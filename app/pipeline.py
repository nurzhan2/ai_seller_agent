"""Конвейер обработки входящих: вебхук → агент → ответ клиенту.

Это тот кусок, которого не хватало, чтобы всё остальное заработало вместе:
до него вебхук принимал и дедуплицировал сообщения, но `handler` был None,
агент вызывался только из тестов и харнесса, а `reset_timer_on_reply` не
вызывал никто.

ПОРЯДОК ШАГОВ ЗДЕСЬ — ЧАСТЬ ПОВЕДЕНИЯ, А НЕ ОФОРМЛЕНИЕ.

`handle_message` (синхронная часть, на каждое входящее):
    1. отбрасываем эхо наших же сообщений — иначе агент отвечает сам себе;
    2. находим/заводим `Chat`, подтягиваем item_id и zone_id;
    3. сохраняем входящее в `Message`;
    4. СБРАСЫВАЕМ таймер касаний — до всех проверок «отвечает ли агент».
       Клиент ответил: даже если агент сейчас молчит (оператор забрал чат,
       агент на паузе, лимит ответов исчерпан), запланированное «Вы где-то
       затерялись?» уже неуместно. Сброс, привязанный к ответу агента, а не
       к сообщению клиента, — это ровно тот баг, который описан в README как
       известный пробел;
    5. если чат у оператора — на этом всё, агента не трогаем вообще;
    6. кладём сообщение в окно debounce.

`_on_debounce_flush` (по истечении окна, на склеенный текст):
    7. ПЕРЕПРОВЕРЯЕМ право агента отвечать: окно длится десятки секунд, и
       оператор мог забрать чат ровно в этот промежуток;
    8. поднимаем историю и состояние диалога из БД, зовём `AgentLoop`;
    9. сохраняем состояние — храповик, ступени, счётчик касаний;
    10. отправляем: DRY_RUN → очередь модерации + карточка оператору,
        иначе → Авито.

ПРО QUOTE_GATE. `apply_dialog_floor` вызывается внутри
`ToolExecutor._tool_calculate_price` — там, где вообще существует
`PriceQuote`. На уровне конвейера квоты уже нет, есть только текст, поэтому
второй «прогон через гейт» здесь был бы имитацией проверки, а не проверкой.
Роль конвейера в храповике другая и не менее важная: он ЗАГРУЖАЕТ
`floor_reached` из БД перед ходом и СОХРАНЯЕТ после (шаги 8 и 9). Без этого
гейт применяется к состоянию, которое каждый раз начинается с нуля, — и
после перезапуска процесса агент спокойно назовёт цену выше уже обещанной.
Гейт без персистентности — это гейт с одной петлёй, а не с двумя.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.agent.debounce import Debouncer
from app.agent.touch_tracking import TouchState, record_first_touch, reset_timer_on_reply
from app.channels.avito_payloads import (
    extract_chat_id,
    extract_item_id,
    extract_message_id,
    extract_text,
    is_outgoing_echo,
)
from app.db.models import SendStatus
from app.dialog_store import HISTORY_LIMIT, DialogStore
from app.metrics import messages_total
from app.ops.notifications import DialogCard, dialog_keyboard, render_dialog_card

logger = logging.getLogger("parmangal.pipeline")


async def _no_delay() -> None:
    """Заглушка для тестов и DRY_RUN — см. `delay_fn` в конструкторе."""
    return None


class MessagePipeline:
    def __init__(
        self,
        *,
        store: DialogStore,
        agent_loop: Any,
        ops_service: Any,
        settings: Any,
        avito_client: Any = None,
        ops_bot: Any = None,
        debounce_window_seconds: Optional[float] = None,
        delay_fn: Optional[Callable[[], Awaitable[None]]] = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.store = store
        self.agent_loop = agent_loop
        self.ops_service = ops_service
        self.settings = settings
        self.avito_client = avito_client
        self.ops_bot = ops_bot
        self.now_fn = now_fn
        # Пауза «как живой человек» — только перед реальной отправкой в Авито.
        # В DRY_RUN ответ уходит в очередь модерации, и задерживать там нечего:
        # клиент всё равно не увидит его раньше, чем оператор нажмёт кнопку.
        self.delay_fn = delay_fn or _no_delay
        window = (
            debounce_window_seconds
            if debounce_window_seconds is not None
            else settings.debounce_window_seconds
        )
        self.debouncer = Debouncer(window_seconds=window, handler=self._on_debounce_flush)

    # -- шаг 1-6: приём входящего ------------------------------------------

    async def handle_message(self, payload: dict) -> None:
        """Обработчик для `webhooks.configure(handler=...)`.

        Никогда не бросает наружу: это фоновая задача FastAPI, и исключение
        отсюда не долетит ни до Авито (мы уже ответили 200), ни до оператора —
        оно просто утонет. Поэтому логируем и выходим.
        """
        try:
            await self._handle_message(payload)
        except Exception:
            logger.exception(
                "pipeline: incoming message failed",
                extra={"chat_id": extract_chat_id(payload)},
            )

    async def _handle_message(self, payload: dict) -> None:
        our_user_id = getattr(self.settings, "avito_user_id", "") or ""
        if is_outgoing_echo(payload, our_user_id):
            # Авито шлёт вебхук и на наши собственные сообщения. Без этой
            # проверки агент отвечает сам себе по кругу.
            return

        chat_id = extract_chat_id(payload)
        if not chat_id:
            logger.warning("pipeline: webhook without a chat_id, dropped")
            return

        text = (extract_text(payload) or "").strip()
        message_id = extract_message_id(payload)

        chat = await self.store.get_or_create_chat(chat_id, item_id=extract_item_id(payload))

        if not text:
            # Картинка/системное событие без текста: сохранять нечего, но
            # клиент проявил активность — таймер напоминаний всё равно сбросить.
            await self._reset_touch_timer(chat_id)
            logger.info("pipeline: message without text", extra={"chat_id": chat_id})
            return

        saved = await self.store.save_incoming(chat_id, text, avito_message_id=message_id)
        if not saved:
            # Второй рубеж дедупликации (первый — Redis в app/webhooks.py).
            # Уже обработанное сообщение не должно ни сбрасывать таймер, ни
            # доходить до агента второй раз.
            return
        messages_total.labels(direction="incoming", status="received").inc()

        # Шаг 4 — до всех проверок ниже. См. докстринг модуля.
        await self._reset_touch_timer(chat_id)

        if chat.is_human_takeover:
            logger.info("pipeline: chat is with a human, agent skipped", extra={"chat_id": chat_id})
            return

        await self.debouncer.submit(chat_id, text)

    async def _reset_touch_timer(self, chat_id: str) -> None:
        concession, touch = await self.store.load_dialog_state(chat_id)
        updated = reset_timer_on_reply(touch)
        if updated == touch:
            return   # таймер и так не тикал — лишняя запись в БД не нужна
        await self.store.save_dialog_state(chat_id, concession, updated)
        logger.info("pipeline: touch timer reset on client reply", extra={"chat_id": chat_id})

    # -- шаг 7-10: ход агента ----------------------------------------------

    async def _on_debounce_flush(self, chat_id: str, merged_text: str) -> None:
        try:
            await self._run_turn(chat_id, merged_text)
        except Exception:
            logger.exception("pipeline: agent turn failed", extra={"chat_id": chat_id})

    async def _run_turn(self, chat_id: str, merged_text: str) -> None:
        chat = await self.store.get_or_create_chat(chat_id)

        # Перепроверка после окна debounce — оператор мог забрать чат, пока
        # клиент дописывал. Проверяем оба источника: `Chat.is_human_takeover`
        # в БД (переживает рестарт) и `OpsService` (кнопки в Telegram, где
        # состояние живёт в InMemoryOpsStore). Они пока не синхронизированы
        # между собой — известный пробел, см. README; поэтому уважаем ЛЮБОЙ
        # из двух, а не выбираем «более правильный».
        if chat.is_human_takeover:
            logger.info("pipeline: takeover during debounce window", extra={"chat_id": chat_id})
            return

        allowed, reason = await self.ops_service.should_agent_reply(chat_id)
        if not allowed:
            logger.info("pipeline: agent stays silent", extra={"chat_id": chat_id, "reason": reason})
            return

        history = await self.store.load_history(chat_id, limit=HISTORY_LIMIT)
        concession, touch = await self.store.load_dialog_state(chat_id)

        result = await self.agent_loop.run_turn(
            dialog_id=chat_id,
            history=history,
            user_text=merged_text,
            state=concession,
            item_id=chat.item_id,
            item_lookup=self.store,
        )

        # Состояние сохраняем ДО отправки: если отправка упадёт, уже выданная
        # уступка и достигнутый пол цены обязаны остаться в базе. Потерянный
        # храповик дороже неотправленного сообщения — клиент переспросит,
        # а вот назвать цену выше обещанной нельзя.
        new_concession = result.concession_state or concession
        new_touch = self._advance_touch_after_reply(new_concession, touch, bool(result.text))
        await self.store.save_dialog_state(chat_id, new_concession, new_touch)

        if not result.text:
            # Спам или классификатор велел молчать — ответа нет, и это норма.
            logger.info(
                "pipeline: agent produced no reply",
                extra={"chat_id": chat_id, "classification": result.classification},
            )
            return

        await self._deliver(chat, result, client_text=merged_text)

    def _advance_touch_after_reply(
        self, concession: Any, touch: TouchState, replied: bool
    ) -> TouchState:
        """Таймер следующего касания заводится только когда цена уже названа.

        Регламент считает «касанием №1» именно названную цену — напоминание
        «Вы где-то затерялись?» бессмысленно для клиента, который ещё не
        услышал ни одной цифры. `record_first_touch` при этом идемпотентен по
        счётчику (`max(touch_count, 1)`), поэтому повторные ходы диалога
        просто сдвигают срок, а не накручивают касания.
        """
        if not replied or not getattr(concession, "base_price_quoted", False):
            return touch
        return record_first_touch(
            touch, self.now_fn(), self.settings.touch_reminder_delay_minutes
        )

    # -- доставка ----------------------------------------------------------

    async def _deliver(self, chat: Any, result: Any, client_text: str) -> None:
        chat_id = chat.chat_id

        if self.settings.dry_run:
            await self.ops_service.queue_reply(chat_id, result.text)
            await self.store.save_outgoing(
                chat_id, result.text, SendStatus.dry_run, llm_meta=result.llm_meta
            )
            messages_total.labels(direction="outgoing", status="dry_run").inc()
            await self._notify_operator(chat, result, client_text)
            await self._count_agent_reply(chat_id)
            return

        await self.delay_fn()
        try:
            await self.avito_client.send_message(chat_id, result.text)
        except Exception:
            # Сообщение не ушло — записываем это честно, а не как отправленное.
            logger.exception("pipeline: sending to Avito failed", extra={"chat_id": chat_id})
            await self.store.save_outgoing(
                chat_id, result.text, SendStatus.failed, llm_meta=result.llm_meta
            )
            messages_total.labels(direction="outgoing", status="failed").inc()
            return

        await self.store.save_outgoing(
            chat_id, result.text, SendStatus.sent, llm_meta=result.llm_meta
        )
        messages_total.labels(direction="outgoing", status="sent").inc()
        await self._count_agent_reply(chat_id)
        if result.escalated:
            await self._notify_operator(chat, result, client_text)

    async def _count_agent_reply(self, chat_id: str) -> None:
        """Счётчик ответов агента растёт В ДВУХ местах, и это не дублирование.

        `Chat.agent_reply_count` в БД — то, что переживает рестарт и видно в
        админке. `ChatFlags.agent_reply_count` в `OpsStore` — то, по чему
        `OpsService.should_agent_reply` сверяет `max_agent_replies_per_chat`.
        Пока `OpsStore` живёт в памяти процесса, это два разных хранилища
        (известный пробел, см. README), и обновить нужно оба: обновишь только
        БД — предохранитель от зацикливания не сработает НИКОГДА, потому что
        проверяющая сторона о нём не узнает.
        """
        await self.store.bump_agent_reply_count(chat_id)
        try:
            flags = await self.ops_service.store.get_flags(chat_id)
            flags.agent_reply_count += 1
            await self.ops_service.store.set_flags(chat_id, flags)
        except Exception:
            # Счётчик — предохранитель, а не часть ответа клиенту: его сбой
            # не должен отменять уже отправленное сообщение.
            logger.exception("pipeline: failed to count the agent reply", extra={"chat_id": chat_id})

    async def _notify_operator(self, chat: Any, result: Any, client_text: str) -> None:
        """Карточка в Telegram. Отсутствие бота — не ошибка: очередь
        модерации всё равно заполнена, и оператор увидит её в /admin/dialogs."""
        if self.ops_bot is None or not getattr(self.settings, "telegram_ops_chat_id", ""):
            return
        card = DialogCard(
            chat_id=chat.chat_id,
            item_title=chat.item_id,
            buyer_name=chat.buyer_name,
            client_text=client_text,
            agent_text=result.text,
            dry_run=bool(self.settings.dry_run),
            escalated=bool(result.escalated),
            escalation_reason=result.escalation_reason,
        )
        try:
            await self.ops_bot.send_message(
                chat_id=self.settings.telegram_ops_chat_id,
                text=render_dialog_card(card),
                reply_markup=dialog_keyboard(
                    chat.chat_id,
                    dry_run=bool(self.settings.dry_run),
                    taken_over=bool(chat.is_human_takeover),
                ),
            )
        except Exception:
            # Телеграм недоступен — ответ уже в очереди, диалог не теряется.
            logger.exception("pipeline: operator notification failed", extra={"chat_id": chat.chat_id})
