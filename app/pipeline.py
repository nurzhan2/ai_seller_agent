"""Конвейер обработки входящих: вебхук → агент → ответ клиенту.

Это тот кусок, которого не хватало, чтобы всё остальное заработало вместе:
до него вебхук принимал и дедуплицировал сообщения, но `handler` был None,
агент вызывался только из тестов и харнесса, а `reset_timer_on_reply` не
вызывал никто.

ПОРЯДОК ШАГОВ ЗДЕСЬ — ЧАСТЬ ПОВЕДЕНИЯ, А НЕ ОФОРМЛЕНИЕ.

`handle_message` (синхронная часть, на каждое входящее):
    0. ПОРОГ ВОЗРАСТА (`_is_too_old_to_answer`, AGENT_MIN_INBOUND_TS) —
       первым делом в `_accept`, сразу после дедупа в `_handle_message`
       (шаг ещё раньше этого списка) и до всего перечисленного ниже.
       Сообщение старше порога дальше по функции размечено, но НЕ
       блокирует запись в историю (шаг 3) — блокирует только шаги, которые
       порождают исходящее (6 и шаблонный ответ на фото). Живёт здесь, а
       не в поллере: вебхук идёт тем же путём `_accept`, и проверка в
       поллере эту дыру не закрыла бы. Разбор инцидента 2026-08-28 (65
       чатов) — в app/config.py:agent_min_inbound_ts;
    1. отбрасываем эхо наших же сообщений — иначе агент отвечает сам себе;
    1a. ОПРЕДЕЛЯЕМ item_id и прогоняем через фильтр объявлений
       (`app/channels/outbound_gate.py:is_listing_allowed` — та же функция,
       что и на границе отправки). Запрещённое объявление — выходим
       НЕМЕДЛЕННО, до создания `Chat`: в аккаунте заказчика есть вакансия
       менеджера, продажа глэмпинга и квартира-студия, и диалога по ним не
       должно остаться ни в базе, ни в карточках оператора. Чаты без
       объявления (обращение из профиля, u2u/a2u) — штатно разрешены;
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

БЕЗ ТЕКСТА (шаг 3 сохранить нечего) — отдельная ветка, не шаги 3-6: лог с
диагностикой сырого payload (тип, ключи, санитизированный content — см.
`_log_textless_payload`, `app/channels/avito_payloads.py:
describe_payload_for_logging`) и, если это фото (`is_image_message`),
шаблонный ответ мимо `AgentLoop` — прямо в `_deliver`, чтобы клиент не
получил тишину (см. `_handle_image_without_text`). Таймер касаний (шаг 4)
сбрасывается всегда, до этой развилки — активность клиента реальна
независимо от того, распознали мы содержимое или нет.

`_on_debounce_flush` (по истечении окна, на склеенный текст):
    7. ПЕРЕПРОВЕРЯЕМ право агента отвечать: окно длится десятки секунд, и
       оператор мог забрать чат ровно в этот промежуток;
    8. поднимаем историю и состояние диалога из БД, зовём `AgentLoop`;
    9. сохраняем состояние — храповик, ступени, счётчик касаний;
    10. отправляем: DRY_RUN → очередь модерации + карточка оператору,
        иначе → Авито.

ГДЕ СООБЩЕНИЕ ИСЧЕЗАЕТ ДО ЗАПИСИ В БД. Полный список — если чата нет в
базе вовсе, причина одна из этих, и каждая пишет строку в лог, чтобы
«не дошло» было отличимо от «дошло и отброшено»:

  * `app/webhooks.py`: неверный секрет в пути (404), тело не JSON,
    дубликат по message_id (Redis), обработчик не подключён;
  * эхо нашего же сообщения — `is_outgoing_echo`, шаг 1 ниже;
  * вебхук без chat_id;
  * объявление под запретом — шаг 1a ниже;
  * дубликат по message_id уже в БД (второй рубеж дедупликации).

Плюс ЖУРНАЛ ПРИЁМА в самом начале `_handle_message`: одна строка на
каждое входящее (chat_id, item_id с его типом, chat_type, message_id) ДО
всех проверок. Без неё «событие не дошло» и «дошло и отброшено»
неразличимы — в базе в обоих случаях пусто.

Отдельно: сообщение БЕЗ ТЕКСТА чат создаёт, но строку в `messages` не
пишет (сохранять нечего) — такой диалог в базе будет с нулём сообщений.

ПРО QUOTE_GATE. `apply_dialog_floor` вызывается внутри
`ToolExecutor._tool_calculate_price` — там, где вообще существует
`PriceQuote`. На уровне конвейера квоты уже нет, есть только текст, поэтому
второй «прогон через гейт» здесь был бы имитацией проверки, а не проверкой.
Роль конвейера в храповике другая и не менее важная: он ЗАГРУЖАЕТ
`floor_reached` из БД перед ходом и СОХРАНЯЕТ после (шаги 8 и 9). Без этого
гейт применяется к состоянию, которое каждый раз начинается с нуля, — и
после перезапуска процесса агент спокойно назовёт цену выше уже обещанной.
Гейт без персистентности — это гейт с одной петлёй, а не с двумя.

ПРО MODERATION_MODE. Раньше в DRY_RUN на одобрение уходило ВСЁ, без
различия. Теперь — три уровня (app.config.Settings.moderation_mode):
`all` держит всё, как раньше; `off` не держит ничего (полная автономия);
`concessions_only` (по умолчанию) — одобрение требуется только когда за ход
`decide()` вернул решение, которое ConcessionEvent.needs_operator_approval
считает значимым (выданная ЦЕНОВАЯ уступка либо «загрузка неизвестна» —
requires_operator_approval). DRY_RUN остаётся отдельным аварийным
рубильником НАД этой настройкой: пока он включён, всё уходит на одобрение
независимо от moderation_mode — см. `_deliver`.

Для запроса на скидку (не для обычного DRY_RUN-холда) заранее, ещё до
постановки в очередь, считается ВТОРОЙ, «чистый» ответ — тем же ходом
агента, но с заблокированными уступками (`concessions_blocked=True` у
`AgentLoop.run_turn`). Если оператор не отреагирует за
`concession_approval_timeout_minutes`, этот запасной текст уходит клиенту
вместо ожидания — диалог не должен стоять в тишине из-за того, что
оператор отошёл. Посчитан заранее, а не в момент таймаута: фоновый воркер
(`check_concession_timeouts`) тогда работает только с уже готовыми
строками из БД, без похода к LLM внутри периодического прохода — тот же
принцип, что у `app.ops.touch_scheduler`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from app.agent.debounce import Debouncer
from app.agent.loop import TurnResult
from app.agent.touch_tracking import TouchState, record_first_touch, reset_timer_on_reply
from app.channels.avito_payloads import (
    describe_payload_for_logging,
    extract_chat_id,
    extract_chat_type,
    extract_created,
    extract_item_id,
    extract_item_id_from_chat,
    extract_item_id_raw,
    extract_message_id,
    extract_text,
    is_image_message,
    is_outgoing_echo,
)
from app.channels import inbound_dedup as dedup
from app.channels.outbound_gate import is_listing_allowed
from app.db.models import SendStatus
from app.dialog_store import HISTORY_LIMIT, DialogStore
from app.metrics import messages_total
from app.ops.notifications import (
    ConcessionRequestCard,
    DialogCard,
    concession_keyboard,
    dialog_keyboard,
    render_concession_request,
    render_daily_limit_notice,
    render_dialog_card,
)
from app.ops.state import PendingReply

logger = logging.getLogger("parmangal.pipeline")

# Не LLM-ответ, а фиксированная строка: клиент прислал фото (известный
# пробел — оно не скачивается и не попадает в Message.image_ids, см.
# README), но молчание в ответ на активность клиента — худший вариант.
# Идёт мимо AgentLoop.run_turn прямо в _deliver, тем же путём модерации/
# dry_run/автономной отправки, что и обычный ход агента.
IMAGE_WITHOUT_TEXT_REPLY = (
    "Вижу фото, но пока не могу его посмотреть — опишите, пожалуйста, "
    "что вас интересует, и я отвечу."
)


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
        kb: Any = None,
        avito_client: Any = None,
        ops_bot: Any = None,
        redis: Any = None,
        debounce_window_seconds: Optional[float] = None,
        delay_fn: Optional[Callable[[], Awaitable[None]]] = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        item_scope_resolver: Any = None,
    ):
        self.store = store
        self.agent_loop = agent_loop
        self.ops_service = ops_service
        self.settings = settings
        # app/channels/item_scope.py:ItemScopeResolver — та же классификация
        # по заголовку, что и на границе отправки (OutboundGate). None —
        # обратная совместимость со старым AVITO_BLOCKED_ITEMS, см.
        # is_listing_allowed в app/channels/outbound_gate.py.
        self.item_scope_resolver = item_scope_resolver
        # Явный параметр, а не chтение agent_loop.kb: конвейеру нужен только
        # max_concessions_per_day для уведомления об исчерпанном лимите
        # (_notify_daily_limit_exhausted), и завязываться на внутреннее
        # устройство AgentLoop ради одного числа — заставлять любой фейк
        # agent_loop в тестах притворяться, что у него есть .kb, хотя это
        # его собственная, а не конвейера, ответственность.
        self.kb = kb
        self.avito_client = avito_client
        self.ops_bot = ops_bot
        # Нужен только дедупликации входящих (app/channels/inbound_dedup.py).
        # None — рабочее состояние для тестов: заявка выдаётся всегда, а от
        # двойного сохранения текста по-прежнему защищает уникальный индекс
        # `uq_messages_avito_message_id`. Чего None НЕ закрывает — повторный
        # шаблонный ответ на фото: строки в `messages` для сообщения без
        # текста нет, и ловить дубль там нечем.
        self._redis = redis
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

    async def handle_message(self, payload: dict, *, source: str = "webhook") -> None:
        """Обработчик для `webhooks.configure(handler=...)` И для поллера.

        Никогда не бросает наружу: это фоновая задача FastAPI, и исключение
        отсюда не долетит ни до Авито (мы уже ответили 200), ни до оператора —
        оно просто утонет. Поэтому логируем и выходим.

        ПОЛЛЕР — ИСКЛЮЧЕНИЕ, ЕМУ ИСХОД НУЖЕН. Он двигает курсор чата по
        каждому обработанному сообщению, и «обработано» ему надо отличать от
        «упало»: иначе одно испорченное сообщение уносит курсор за все
        следующие. Поэтому возвращается bool, а не None, — но вебхук его
        по-прежнему игнорирует.

        True здесь — ОБРАБОТАНО, а не «агент ответил». Эхо, чужое объявление,
        чат у оператора, дубль по уникальному индексу — всё это штатные
        исходы: повторять их бессмысленно, курсор двигать надо. False бывает
        ровно в одном случае — что-то упало, и тогда сообщение обязано
        вернуться следующим проходом.

        `source` идёт в журнал приёма: с двумя каналами «сообщение не дошло»
        и «дошло не тем путём, каким ждали» перестали быть одним и тем же
        вопросом.
        """
        try:
            return await self._handle_message(payload, source=source)
        except Exception:
            logger.exception(
                "pipeline: incoming message failed",
                extra={"chat_id": extract_chat_id(payload), "source": source},
            )
            return False

    async def _handle_message(self, payload: dict, *, source: str = "webhook") -> bool:
        # ЖУРНАЛ ПРИЁМА: одна строка на КАЖДОЕ входящее, до всех проверок.
        # Без неё нельзя отличить «событие не дошло» от «дошло и молча
        # отброшено»: в базе в обоих случаях пусто. Именно этот вопрос —
        # «приходят ли вебхуки по объявлениям комплекса вообще» — иначе
        # не имеет ответа. `raw_item_id` печатается с типом: item_id в API
        # число, у нас строка, и «строка против числа» — первое, что
        # проверяют, когда фильтр «не сработал».
        raw_item_id = extract_item_id_raw(payload)
        message_id = extract_message_id(payload)
        logger.info(
            "pipeline: входящее source=%s chat_id=%s item_id=%r(%s) chat_type=%s msg_id=%s",
            source, extract_chat_id(payload), raw_item_id, type(raw_item_id).__name__,
            extract_chat_type(payload), message_id,
        )

        # ДЕДУПЛИКАЦИЯ — ЗДЕСЬ, А НЕ В ВЕБХУКЕ. Единственная точка, через
        # которую проходят оба канала приёма; подробности и почему заявка
        # ставится на минуту, а не на сутки, — в app/channels/inbound_dedup.py.
        if not await dedup.claim(message_id, self._redis):
            logger.info(
                "pipeline: отброшен дубликат по message_id (уже в обработке или обработан)",
                extra={"chat_id": extract_chat_id(payload), "message_id": message_id,
                       "source": source},
            )
            return False

        handled = False
        try:
            await self._accept(payload, source=source)
            handled = True
            return True
        finally:
            if handled:
                await dedup.confirm(
                    message_id, self._redis,
                    self.settings.webhook_idempotency_ttl_seconds,
                )
            else:
                # Упало — заявку снимаем, чтобы сообщение вернулось следующим
                # проходом поллера, а не исчезло навсегда под видом дубля.
                await dedup.release(message_id, self._redis)

    async def _accept(self, payload: dict, *, source: str = "webhook") -> None:
        """Разбор одного входящего. Штатные отказы — просто `return`.

        Любой НЕштатный сбой обязан улететь исключением наверх: там по нему
        снимается заявка дедупа и не двигается курсор поллера. Глушить
        исключения здесь значит терять сообщения молча.
        """
        # ПОРОГ ВОЗРАСТА — сразу после дедупа (он уже отработал в
        # `_handle_message`), до всякой бизнес-логики ниже. Инцидент
        # 2026-08-28: курсор поллера решал «отвечать ли» и ошибся на 65
        # чатах. Теперь «отвечать ли» решается ЗДЕСЬ, одним местом для
        # обоих каналов (source="poller"/"webhook" — оба идут через
        # `handle_message` -> `_accept`), независимо от курсора, флагов и
        # номера прохода. `too_old` НЕ прерывает разбор — сообщение всё
        # равно можно записать в историю диалога (см. использование ниже
        # и app/config.py:agent_min_inbound_ts), просто ни один путь,
        # порождающий исходящее, дальше по функции его не увидит.
        too_old = self._is_too_old_to_answer(payload, source=source)

        our_user_id = getattr(self.settings, "avito_user_id", "") or ""
        if is_outgoing_echo(payload, our_user_id):
            # Авито шлёт вебхук и на наши собственные сообщения. Без этой
            # проверки агент отвечает сам себе по кругу.
            #
            # Лог обязателен, хотя отбрасывание здесь штатное и частое.
            # Это ПЕРВОЕ место, где сообщение исчезает до всякой записи в
            # БД: при неверном AVITO_USER_ID (или если Авито однажды
            # положит наш id в author_id входящего) сюда уходили бы ВСЕ
            # сообщения подряд, чат не появился бы в базе вовсе, и снаружи
            # это неотличимо от «вебхуки не приходили».
            logger.info(
                "pipeline: отброшено эхо нашего же сообщения (author_id == AVITO_USER_ID)",
                extra={"chat_id": extract_chat_id(payload)},
            )
            return

        chat_id = extract_chat_id(payload)
        if not chat_id:
            logger.warning("pipeline: webhook without a chat_id, dropped")
            return

        item_id = await self._resolve_item_id(payload, chat_id)
        if not await self._listing_is_allowed(item_id, chat_id):
            # Ничего не сохраняем и не создаём: диалога по чужому объявлению
            # у нас быть не должно вообще — ни в базе, ни в карточках
            # оператора. Возврат ДО get_or_create_chat именно поэтому.
            return

        text = (extract_text(payload) or "").strip()
        message_id = extract_message_id(payload)

        chat = await self.store.get_or_create_chat(chat_id, item_id=item_id)

        if not text:
            # Картинка/системное событие без текста: сохранять нечего, но
            # клиент проявил активность — таймер напоминаний всё равно сбросить.
            await self._reset_touch_timer(chat_id)
            self._log_textless_payload(payload, chat_id)

            if chat.is_human_takeover:
                logger.info("pipeline: chat is with a human, agent skipped", extra={"chat_id": chat_id})
                return
            if is_image_message(payload) and not too_old:
                await self._handle_image_without_text(chat)
            return

        saved = await self.store.save_incoming(chat_id, text, avito_message_id=message_id)
        if not saved:
            # Второй рубеж дедупликации; первый — заявка в Redis несколькими
            # строками выше (`dedup.claim`). Он остаётся нужен: заявка живёт
            # сутки, а уникальный индекс — вечно, и после истечения ключа
            # (или при пустом Redis) только он не даёт сохранить текст дважды.
            # Уже обработанное сообщение не должно ни сбрасывать таймер, ни
            # доходить до агента второй раз.
            #
            # Лог обязателен: это последнее место, где сообщение исчезало
            # беззвучно. Если Авито когда-нибудь переиспользует message_id
            # (или Redis отдаст ложный промах), сюда уйдут живые сообщения,
            # и без строки в логе это выглядит как «клиент написал, а агент
            # молчит» без единого следа.
            logger.info(
                "pipeline: отброшен дубликат по message_id (уже есть в БД)",
                extra={"chat_id": chat_id, "message_id": message_id},
            )
            return
        messages_total.labels(direction="incoming", status="received").inc()

        # Шаг 4 — до всех проверок ниже. См. докстринг модуля.
        await self._reset_touch_timer(chat_id)

        if chat.is_human_takeover:
            logger.info("pipeline: chat is with a human, agent skipped", extra={"chat_id": chat_id})
            return

        if too_old:
            # Записано в историю (save_incoming выше) — просто не отвечаем.
            return

        await self.debouncer.submit(chat_id, text)

    def _is_too_old_to_answer(self, payload: dict, *, source: str) -> bool:
        """AGENT_MIN_INBOUND_TS: единственная защита от ответа в чат, где
        клиент писал в последний раз давно.

        ДЕФОЛТ БЕЗОПАСНЫЙ. 0/незадано означает «агент не отвечает ни на
        что» — НЕ «проверка выключена». Обратный дефолт (0 = защита не
        активна) означал бы, что забытая на деплое переменная тихо снимает
        защиту, — ровно тот класс ошибки, что уже стоил 65 сообщений (см.
        app/config.py:agent_min_inbound_ts). app/main.py дублирует это
        WARNING'ом в лог при старте, если POLLER_ENABLED=true, а порог
        не поднят.

        Пока порог включён (> 0), сообщение с НЕИЗВЕСТНЫМ `created` тоже
        считается СТАРЫМ, а не свежим (fail closed — тот же приём, что у
        `OutboundGate.is_allowed` и `OwnItemIds.__call__`): доверять
        свежести, о которой нечего сказать, нельзя ровно там, где мы уже
        один раз ошиблись в другую сторону.
        """
        threshold = getattr(self.settings, "agent_min_inbound_ts", 0) or 0
        if threshold <= 0:
            logger.warning(
                "pipeline: AGENT_MIN_INBOUND_TS не задан (<= 0) — агент не "
                "отвечает ни на что, пока порог не поднят осознанно",
                extra={"chat_id": extract_chat_id(payload), "source": source},
            )
            return True

        created = extract_created(payload)
        if created is not None and created >= threshold:
            return False

        logger.warning(
            "pipeline: входящее старше AGENT_MIN_INBOUND_TS — исходящее не порождается",
            extra={
                "chat_id": extract_chat_id(payload),
                # НЕ "created": это имя уже занято самим LogRecord (момент
                # записи лога) — logging бросает KeyError на попытку его
                # перезаписать через extra.
                "inbound_created": created,
                "agent_min_inbound_ts": threshold,
                "source": source,
            },
        )
        return True

    async def _resolve_item_id(self, payload: dict, chat_id: str) -> Optional[str]:
        """item_id из вебхука, а если его там нет — из чата отдельным запросом.

        Почему его иногда нет: по спеку (WebhookMessage) item_id «актуально
        только для чатов с типом u2i» и объявлен nullable. То есть у чатов
        u2u/a2u — начатых с профиля продавца, а не с объявления — объявления
        нет в принципе, и дозапрашивать его бессмысленно: get_chat вернёт
        контекст не типа "item". Поэтому запрос делается ТОЛЬКО когда чат
        по объявлению (или когда тип чата не удалось прочитать вовсе —
        тогда одна попытка лучше, чем молча потерянный item_id).
        """
        item_id = extract_item_id(payload)
        if item_id is not None:
            return item_id

        chat_type = extract_chat_type(payload)
        if chat_type in ("u2u", "a2u"):
            logger.info(
                "pipeline: чат не по объявлению (chat_type=%s) — item_id не существует, "
                "get_chat не запрашиваем",
                chat_type, extra={"chat_id": chat_id},
            )
            return None
        if self.avito_client is None:
            logger.info(
                "pipeline: item_id нет в вебхуке (chat_type=%s), запросить неоткуда — "
                "нет avito_client",
                chat_type, extra={"chat_id": chat_id},
            )
            return None

        try:
            chat = await self.avito_client.get_chat(chat_id)
        except Exception:
            # Не роняем ход из-за одного недостающего поля: дальше сработает
            # либо фильтр по списку (если он задан), либо обычная работа без
            # подсказки о зоне — ровно как было до фолбэка.
            logger.exception(
                "pipeline: get_chat не ответил, item_id остался неизвестным",
                extra={"chat_id": chat_id},
            )
            return None

        recovered = extract_item_id_from_chat(chat)
        logger.info(
            "pipeline: item_id не пришёл в вебхуке (chat_type=%s), из get_chat получено: %s",
            chat_type, recovered or "тоже ничего",
            extra={"chat_id": chat_id},
        )
        return recovered

    async def _listing_is_allowed(self, item_id: Optional[str], chat_id: str) -> bool:
        """То же правило, что и на границе отправки — буквально та же
        функция (`is_listing_allowed`), а не её копия.

        Проверка стоит ЗДЕСЬ дополнительно к гейту не ради дублирования, а
        ради другого эффекта: не завести диалог по чужому объявлению вообще
        — ни строки в базе, ни карточки оператору. Гарантию «клиент не
        получит сообщение» даёт гейт; эта проверка экономит мусор.
        """
        if await is_listing_allowed(
            item_id, self.settings, scope_resolver=self.item_scope_resolver
        ):
            return True

        logger.info(
            "pipeline: пропущено — объявление %s под запретом",
            item_id if item_id is not None else "(чат без объявления)",
            extra={"chat_id": chat_id, "item_id": item_id},
        )
        return False

    async def _reset_touch_timer(self, chat_id: str) -> None:
        concession, touch = await self.store.load_dialog_state(chat_id)
        updated = reset_timer_on_reply(touch)
        if updated == touch:
            return   # таймер и так не тикал — лишняя запись в БД не нужна
        await self.store.save_dialog_state(chat_id, concession, updated)
        logger.info("pipeline: touch timer reset on client reply", extra={"chat_id": chat_id})

    def _log_textless_payload(self, payload: dict, chat_id: str) -> None:
        """Раньше лог "message without text" не говорил, ЧТО пришло —
        фото (известный пробел, см. README) или структура, отличная от
        ожидаемой (системное событие, другой тип). Диагностика — прямо в
        тексте сообщения лога, а не только в extra: extra-поля не попадают
        в обычный текстовый вывод logging.basicConfig, только в structured-
        коллекторы, если они когда-нибудь появятся. describe_payload_for_
        logging маскирует имена/телефоны сама (см. app/channels/
        avito_payloads.py) — здесь их только сериализуют."""
        description = describe_payload_for_logging(payload)
        dumped = json.dumps(description["sanitized"], ensure_ascii=False)
        logger.info(
            "pipeline: message without text — type=%s top_level_keys=%s payload=%s",
            description["message_type"], description["top_level_keys"], dumped[:2000],
            extra={"chat_id": chat_id},
        )

    async def _handle_image_without_text(self, chat: Any) -> None:
        """Клиент прислал фото — молчание хуже шаблонного ответа. Не ход
        агента (нет текста, чтобы дать модели), поэтому TurnResult собран
        вручную и отправлен через тот же _deliver, что и обычный ход: та
        же модерация/dry_run/автономная отправка, тот же учёт лимита
        ответов (_count_agent_reply — внутри _deliver и _queue_for_
        moderation)."""
        allowed, reason = await self.ops_service.should_agent_reply(chat.chat_id)
        if not allowed:
            logger.info(
                "pipeline: agent stays silent on image",
                extra={"chat_id": chat.chat_id, "reason": reason},
            )
            return
        result = TurnResult(text=IMAGE_WITHOUT_TEXT_REPLY)
        await self._deliver(chat, result, client_text="[фото]", gate=None, fallback_text=None)

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

        gate = self._concession_gate(result)

        # Дневной лимит уступок (R10) — не гейт (нечего одобрять, движок
        # уже отказал сам), а информирование: оператор обязан узнать, что с
        # этого момента клиенты дня идут без скидок, чтобы вмешаться вручную,
        # если сочтёт нужным. Не привязано к gate/dry_run — это факт про
        # весь бизнес-день, а не про решение по конкретному ответу.
        for event in result.concession_events:
            if event.decision.daily_limit_exhausted:
                await self._notify_daily_limit_exhausted(chat_id)
                break

        # «Чистый» запасной ответ считается заранее, ещё до постановки в
        # очередь — не в момент таймаута (см. докстринг модуля). Только для
        # живого режима: в DRY_RUN due_at никогда не выставляется, значит
        # авто-отправка по таймауту всё равно не сработает, а лишний вызов
        # модели того не стоит.
        fallback_text: Optional[str] = None
        if gate is not None and not self.settings.dry_run:
            fallback = await self.agent_loop.run_turn(
                dialog_id=chat_id,
                history=history,
                user_text=merged_text,
                state=concession,
                item_id=chat.item_id,
                item_lookup=self.store,
                concessions_blocked=True,
            )
            fallback_text = fallback.text

        await self._deliver(chat, result, client_text=merged_text, gate=gate, fallback_text=fallback_text)

    def _concession_gate(self, result: Any) -> Optional[Any]:
        """Первое решение за ход, которое требует одобрения — или None.

        `moderation_mode="off"` отключает гейт целиком (полная автономия,
        включая ценовые уступки). При любом другом значении — первое
        совпадение по `ConcessionEvent.needs_operator_approval`; за ход
        обычно один вызов `request_concession`, но если их несколько,
        достаточно одного значимого решения, чтобы придержать весь ответ
        целиком — частичная отправка (часть текста без одобрения, часть с)
        технически невозможна: ответ — один текст на весь ход.
        """
        if self.settings.moderation_mode == "off":
            return None
        for event in result.concession_events:
            if event.needs_operator_approval:
                return event
        return None

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

    async def _deliver(
        self, chat: Any, result: Any, client_text: str,
        gate: Optional[Any] = None, fallback_text: Optional[str] = None,
    ) -> None:
        chat_id = chat.chat_id

        if self.settings.dry_run:
            # Мастер-рубильник: пока он включён, ВСЁ уходит на одобрение,
            # независимо от moderation_mode — карточка только богаче для
            # запроса на скидку, а решение о самом холде дальше не идёт.
            if gate is not None:
                await self._queue_concession_approval(
                    chat, result, client_text, gate, fallback_text=None, due_at=None,
                )
            else:
                await self._queue_for_moderation(chat, result, client_text)
            return

        if gate is not None:
            due_at = self.now_fn() + timedelta(
                minutes=self.settings.concession_approval_timeout_minutes
            )
            await self._queue_concession_approval(
                chat, result, client_text, gate, fallback_text=fallback_text, due_at=due_at,
            )
            return

        if self.settings.moderation_mode == "all":
            await self._queue_for_moderation(chat, result, client_text)
            return

        # Автономная отправка: не concessions_only-триггер, mode != all,
        # DRY_RUN выключен — агенту не нужен человек, чтобы ответить.
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
        # Оператор видит переписку ВСЕГДА, не только при эскалации — без
        # кнопок одобрения (нечего одобрять, сообщение уже ушло), но с
        # «Взять на себя»: перехватить диалог оператор должен мочь всегда.
        # render_dialog_card/dialog_keyboard уже дают ровно это при
        # dry_run=False — отдельная FYI-вёрстка не нужна.
        await self._notify_operator(chat, result, client_text)

    async def _queue_for_moderation(self, chat: Any, result: Any, client_text: str) -> None:
        """Обычный холд — DRY_RUN без запроса на скидку, либо
        moderation_mode=all на не-ценовом ходе. Без дедлайна: авто-отправки
        по таймауту здесь нет, это не запрос на скидку."""
        chat_id = chat.chat_id
        await self.ops_service.queue_reply(chat_id, result.text)
        await self.store.save_outgoing(
            chat_id, result.text, SendStatus.dry_run, llm_meta=result.llm_meta
        )
        messages_total.labels(direction="outgoing", status="dry_run").inc()
        await self._notify_operator(chat, result, client_text)
        await self._count_agent_reply(chat_id)

    async def _queue_concession_approval(
        self, chat: Any, result: Any, client_text: str, gate: Any,
        *, fallback_text: Optional[str], due_at: Optional[datetime],
    ) -> None:
        """Запрос на ценовую уступку — богатая карточка, отдельные кнопки,
        и (в живом режиме) дедлайн с заранее посчитанным запасным ответом.
        """
        chat_id = chat.chat_id
        reply = PendingReply(
            chat_id=chat_id,
            text=result.text,
            created_at=self.now_fn(),
            is_concession=True,
            fallback_text=fallback_text,
            due_at=due_at,
        )
        await self.ops_service.store.set_pending(chat_id, reply)
        await self.store.save_outgoing(
            chat_id, result.text, SendStatus.dry_run, llm_meta=result.llm_meta
        )
        messages_total.labels(direction="outgoing", status="dry_run").inc()

        # «Сколько уже выдано сегодня» — ДО записи текущего решения в лог,
        # иначе карточка сосчитала бы сама себя.
        concessions_today = await self.store.count_concessions_today()
        await self.store.log_concession(chat_id, gate)

        decision = gate.decision
        card = ConcessionRequestCard(
            chat_id=chat_id,
            client_text=client_text,
            agent_text=result.text,
            tier=decision.tier,
            trigger=gate.trigger,
            reason=decision.denial_reason or "",
            base_price=gate.base_price,
            final_price=decision.new_quote.total if decision.new_quote else None,
            revenue_delta=decision.revenue_delta if decision.new_quote else None,
            concessions_today=concessions_today,
            provisional=decision.provisional_policy,
        )
        if self.ops_bot is not None and getattr(self.settings, "telegram_ops_chat_id", ""):
            try:
                await self.ops_bot.send_message(
                    chat_id=self.settings.telegram_ops_chat_id,
                    text=render_concession_request(card),
                    reply_markup=concession_keyboard(chat_id),
                )
            except Exception:
                logger.exception(
                    "pipeline: concession card send failed", extra={"chat_id": chat_id}
                )
        await self._count_agent_reply(chat_id)

    # -- таймаут запроса на скидку ------------------------------------------

    async def check_concession_timeouts(self) -> list[str]:
        """Один проход фонового воркера. Просроченный запрос на скидку —
        клиенту уходит заранее посчитанный fallback_text, диалог
        продолжается, оператор в логе видит явную пометку. Возвращает
        chat_id всех обработанных диалогов (для теста и для лога воркера).
        """
        if self.settings.dry_run:
            # Мастер-рубильник проверяется и здесь, не только при постановке
            # в очередь — если DRY_RUN включили обратно, пока запрос ждал
            # оператора, авто-отправка не должна проскочить между проверками.
            return []

        handled: list[str] = []
        for pending in await self.ops_service.store.list_due_concessions(self.now_fn()):
            if not pending.fallback_text:
                # Не должно происходить (см. _run_turn — fallback считается
                # заранее для каждого due_at != None), но пустая строка не
                # повод молча зависнуть — чат остаётся в очереди до
                # следующего прохода вместо того, чтобы тихо потерять его.
                logger.error(
                    "pipeline: concession timeout without a fallback_text",
                    extra={"chat_id": pending.chat_id},
                )
                continue
            try:
                await self.avito_client.send_message(pending.chat_id, pending.fallback_text)
            except Exception:
                logger.exception(
                    "pipeline: concession timeout fallback send failed",
                    extra={"chat_id": pending.chat_id},
                )
                continue

            await self.store.save_outgoing(
                pending.chat_id, pending.fallback_text, SendStatus.sent,
            )
            messages_total.labels(direction="outgoing", status="sent").inc()
            await self._count_agent_reply(pending.chat_id)
            await self.ops_service.store.set_pending(pending.chat_id, None)
            await self.ops_service.store.log_action(
                pending.chat_id, 0, "concession_timeout", {"text": pending.fallback_text}
            )
            logger.info(
                "уступка не подтверждена по таймауту",
                extra={"chat_id": pending.chat_id},
            )
            handled.append(pending.chat_id)
        return handled

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

    async def _notify_daily_limit_exhausted(self, chat_id: str) -> None:
        """R10: дневной лимит уступок исчерпан. Всегда логируется (ниже) —
        Telegram-уведомление лучшая попытка, а не гарантия: без бота или без
        TELEGRAM_OPS_CHAT_ID лог остаётся единственным способом узнать."""
        if self.kb is None:
            logger.warning("concession daily limit exhausted", extra={"chat_id": chat_id})
            return
        limit = self.kb.concessions.policy.max_concessions_per_day
        logger.warning(
            "concession daily limit exhausted", extra={"chat_id": chat_id, "limit": limit}
        )
        if self.ops_bot is None or not getattr(self.settings, "telegram_ops_chat_id", ""):
            return
        try:
            await self.ops_bot.send_message(
                chat_id=self.settings.telegram_ops_chat_id,
                text=render_daily_limit_notice(chat_id, limit),
            )
        except Exception:
            logger.exception(
                "pipeline: daily limit notification failed", extra={"chat_id": chat_id}
            )
