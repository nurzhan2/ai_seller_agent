"""Единственная граница, через которую уходит сообщение живому клиенту.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ СЛОЙ, А НЕ ПРОВЕРКА В КАЖДОЙ ВЕТКЕ. Белый список
объявлений (`AVITO_ALLOWED_ITEMS`) сначала жил только в конвейере — и
этого оказалось недостаточно: в 09:00 воркер отложенных касаний отправил
третье касание в чат `u2u-…`, а в 12:09 конвейер тот же чат заблокировал.
Проверка стояла на одном входе, а выходов у системы четыре:

  * автономный ответ агента            (app/pipeline.py:_deliver)
  * запасной ответ по таймауту уступки (app/pipeline.py:check_concession_timeouts)
  * отложенное касание                 (app/main.py:build_touch_sender)
  * ответ, одобренный оператором       (app/ops/bot.py:OpsService.approve/edit)

Тот же приём, что и `app/pricing/quote_gate.py` для цен: инвариант,
который обязан выполняться ВЕЗДЕ, проверяется на границе, а не
переписывается в каждом вызывающем месте — иначе следующий новый путь
отправки снова про него забудет, и узнаем мы об этом опять из логов.

Гейт СОЗНАТЕЛЬНО обёртка над `AvitoClient`, а не правка внутри него:
`AvitoClient` — транспорт (токены, ретраи, лимиты), а «кому мы вообще
имеем право писать» — правило бизнеса. Снаружи гейт неотличим от клиента
(те же имена методов), поэтому вызывающий код не знает о его
существовании и не может его обойти по невнимательности — ему просто
передают гейт вместо клиента.

По той же причине здесь же, а не в конвейере, живут аварийный рубильник
(app/channels/kill_switch.py, команды /stop и /resume в Telegram-боте) и
суточный лимит исходящих (app/channels/daily_limit.py) — оба должны
работать одинаково для всех четырёх выходов, а не только для того, где их
догадались проверить первым.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from app.channels.daily_limit import DailyLimitResult

logger = logging.getLogger("parmangal.outbound")

# chat_id -> item_id объявления (или None, если чат не по объявлению).
ItemIdLookup = Callable[[str], Awaitable[Optional[str]]]
# chat_id -> стоит ли на чате ручной hold (app/db/models.py:Chat.manual_hold).
ManualHoldLookup = Callable[[str], Awaitable[bool]]
# chat_id -> состояние перехвата чата человеком (см. TakeoverState ниже).
TakeoverLookup = Callable[[str], Awaitable["TakeoverState"]]
# () -> стоит ли глобальный аварийный рубильник (app/channels/kill_switch.py).
KillSwitchLookup = Callable[[], Awaitable[bool]]
# () -> результат проверки суточного лимита (app/channels/daily_limit.py).
DailyLimitCheck = Callable[[], Awaitable[DailyLimitResult]]
# результат -> уведомление в Telegram: либо лимит только что исчерпан
# (just_exceeded), либо не удалось его проверить (redis_unavailable) —
# разбирать, какой из двух случаев, дело колбэка, а не гейта.
DailyLimitAlert = Callable[[DailyLimitResult], Awaitable[None]]


@dataclass(frozen=True)
class TakeoverState:
    """Ровно те два поля `ChatFlags`, которые нужны правилу ниже.

    Свой тип, а не `ChatFlags` из app/ops/state.py, чтобы граница исходящих
    не зависела от операторского контура: гейт обязан работать и там, где
    операторского стора нет вовсе (харнесс, тесты), а направление зависимостей
    «канал → операторский модуль» рано или поздно превратится в кольцо.
    """

    is_human_takeover: bool = False
    takeover_at: Optional[datetime] = None


def takeover_blocks(
    state: TakeoverState, settings: Any, now: Optional[datetime] = None
) -> bool:
    """ЕДИНСТВЕННОЕ определение правила «в чате человек, агент молчит».

    Зовётся из двух мест и обязано оставаться одной функцией: гейт применяет
    его на границе (инвариант, который нельзя обойти новым путём отправки),
    а `should_agent_reply` — заранее, чтобы не платить за ход модели, который
    всё равно не уйдёт. Две похожие проверки в двух файлах разъехались бы на
    первой же правке режима.

    Режимы — app/config.py:takeover_mode. `cooldown` считает окно от
    ПОСЛЕДНЕГО сообщения менеджера: каждое новое его сообщение сдвигает
    `takeover_at` и продлевает тишину.

    `takeover_at is None` при поднятом флаге — это «человек в чате, а когда
    он писал, мы не знаем». Молчим (fail closed): ошибиться в сторону
    молчания здесь дешевле, чем заговорить поверх живого менеджера — ровно
    это и было инцидентом.
    """
    if not state.is_human_takeover:
        return False

    mode = getattr(settings, "takeover_mode", "cooldown")
    if mode == "off":
        return False
    if mode == "permanent":
        return True

    if state.takeover_at is None:
        return True

    minutes = int(getattr(settings, "takeover_cooldown_minutes", 15) or 0)
    if minutes <= 0:
        # Нулевое окно — это «кулдаун выключен», а не «молчать вечно».
        return False

    taken_at = state.takeover_at
    if taken_at.tzinfo is None:
        # SQLite в тестах отдаёт naive datetime; в Postgres колонка aware.
        # Считать naive за локальное время — способ получить окно длиной в
        # часовой пояс.
        taken_at = taken_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - taken_at < timedelta(minutes=minutes)


async def is_listing_allowed(
    item_id: Optional[str], settings: Any, *, scope_resolver: Any = None
) -> bool:
    """Единственное определение правила «по этому объявлению можно писать».

    Одна функция на все места, где правило применяется: на входе в конвейер
    (чтобы не заводить диалог по чужому объявлению), на границе отправки
    (`OutboundGate`, чтобы ни один путь не написал клиенту мимо проверки) и
    в диагностике (/admin/dialogs, scripts/poll_once.py). Раньше это была
    скопированная логика в нескольких местах — так и разъезжаются инварианты.

    Порядок разбора:

    1. Нет item_id (обращение из профиля, chat_type u2u/a2u — объявления у
       такого чата нет по спеку Авито). Решает
       `AVITO_ALLOW_CHATS_WITHOUT_ITEM`; по умолчанию отвечаем — это живой
       клиент, и молчание хуже ответа.
    2. Задан белый список `AVITO_ALLOWED_ITEMS` — он В ПРИОРИТЕТЕ и работает
       как раньше: разрешено только перечисленное. Оставлен ради стендов,
       где он уже выставлен, и как аварийный режим «пускать только вот эти».
    3. Иначе — `scope_resolver` (см. `app/channels/item_scope.py`):
       классификация по заголовку с жёстким deny поверх нее (пять зашитых
       id, item_id == "0", объявления не нашего аккаунта). Резолвер async,
       поэтому и эта функция асинхронная — единственная точка фильтрации не
       может остаться синхронной, если решение иногда требует догрузить
       карточку объявления.
    4. `scope_resolver` не передан (юнит-тесты, стенды без БД/Авито) —
       ОБРАТНАЯ СОВМЕСТИМОСТЬ со старым чёрным списком `AVITO_BLOCKED_ITEMS`:
       запрещено только перечисленное, без классификации по заголовку.
    """
    if item_id is None:
        return bool(getattr(settings, "avito_allow_chats_without_item", True))

    # item_id приводится к строке ЗДЕСЬ, а не только у вызывающих. В API
    # Авито это число (спек: "item_id": {"type": "integer"}), у нас везде
    # строка — и сравнение строки с числом не совпадает молча, то есть
    # заблокированное объявление тихо становится разрешённым. Сейчас все
    # вызывающие передают строку (`extract_item_id` -> `_first_scalar` и
    # колонка `Chat.item_id`), но полагаться на это по всей цепочке —
    # ровно тот вид допущения, который однажды перестаёт выполняться.
    item_id = str(item_id)
    allowed = [str(i) for i in (getattr(settings, "avito_allowed_items", None) or [])]
    if allowed:
        return item_id in allowed

    if scope_resolver is None:
        blocked = [str(i) for i in (getattr(settings, "avito_blocked_items", None) or [])]
        return item_id not in blocked

    row = await scope_resolver.resolve(item_id)
    return row.decision == "allow"


class OutboundBlocked(RuntimeError):
    """Общий предок всех отказов гейта.

    Исключение, а не тихий `{"blocked": True}` в ответе, СОЗНАТЕЛЬНО — как
    и у каждого из его наследников ниже. Вызывающий код (например,
    app/pipeline.py:_deliver) после успешной отправки пишет
    `SendStatus.sent` — то есть на молчаливый отказ он записал бы в базу
    «отправлено» для сообщения, которого клиент никогда не получал, и
    заметить это стало бы нечем. Исключение попадает в уже существующие
    `except` вокруг отправки: сообщение честно помечается недоставленным, а
    в логе (и, для рубильника и суточного лимита, в Telegram) видно, почему.

    Общий базовый класс — чтобы вызывающему коду, которому не важна причина
    отказа, было достаточно одного `except OutboundBlocked`, а не
    перечисления всех подклассов.
    """


class ListingNotAllowed(OutboundBlocked):
    """Попытка написать в чат по объявлению вне белого списка.

    Штатные пути (конвейер на входе, воркер касаний через `can_send`)
    отсекают такие чаты РАНЬШЕ, поэтому сюда долетает только то, что
    просочилось мимо них — то есть ошибка в коде, и она должна быть
    шумной, а не тихой.
    """


class SendingHalted(OutboundBlocked):
    """Глобальный аварийный рубильник (app/channels/kill_switch.py) активен.

    В отличие от `ListingNotAllowed`, это решение ВРЕМЕННОЕ и относится не
    к конкретному чату, а ко всей отправке сразу — снимается командой
    /resume в Telegram-боте оператора.
    """


class DailyLimitExceeded(OutboundBlocked):
    """Суточный лимит исходящих (app/channels/daily_limit.py) исчерпан.

    Снимается сам в полночь по Москве — вмешательство оператора не
    требуется, только уведомление в Telegram, отправленное ровно один раз
    в момент, когда лимит был исчерпан.
    """


class DailyLimitUnavailable(OutboundBlocked):
    """Не удалось проверить суточный лимит — Redis настроен, но упал.

    Fail closed, тем же принципом, что и `SendingHalted`: предохранитель
    существует ровно на случай, когда что-то уже пошло не так, и должен
    сработать и тогда, когда инфраструктура, которой он посчитан, тоже
    отказала — см. докстринг app/channels/daily_limit.py.
    """


class OutboundGate:
    """Пропускает наружу только сообщения по разрешённым объявлениям."""

    def __init__(
        self,
        client: Any,
        settings: Any,
        item_id_lookup: Optional[ItemIdLookup] = None,
        manual_hold_lookup: Optional[ManualHoldLookup] = None,
        takeover_lookup: Optional[TakeoverLookup] = None,
        kill_switch_lookup: Optional[KillSwitchLookup] = None,
        daily_limit_check: Optional[DailyLimitCheck] = None,
        daily_limit_alert: Optional[DailyLimitAlert] = None,
        item_scope_resolver: Any = None,
    ):
        self._client = client
        self._settings = settings
        self._item_id_lookup = item_id_lookup
        self._manual_hold_lookup = manual_hold_lookup
        self._takeover_lookup = takeover_lookup
        self._kill_switch_lookup = kill_switch_lookup
        self._daily_limit_check = daily_limit_check
        self._daily_limit_alert = daily_limit_alert
        # app/channels/item_scope.py:ItemScopeResolver — классификация по
        # заголовку вместо зашитого в код списка. None — старое поведение
        # чистого AVITO_BLOCKED_ITEMS (см. is_listing_allowed).
        self._item_scope_resolver = item_scope_resolver

    # -- решение -----------------------------------------------------------

    def _filter_is_off(self) -> bool:
        """Ни чёрного списка, ни белого, ни резолвера item_scope, и чаты без
        объявления разрешены — фильтровать нечего, и ходить в базу за
        item_id незачем.

        С собранным `item_scope_resolver` этот короткий путь ВСЕГДА False:
        резолвер обязан отработать хотя бы жёсткий deny (item_id == "0",
        объявление не нашего аккаунта) даже когда AVITO_BLOCKED_ITEMS пуст.
        """
        if self._item_scope_resolver is not None:
            return False
        return (
            not (getattr(self._settings, "avito_allowed_items", None) or [])
            and not (getattr(self._settings, "avito_blocked_items", None) or [])
            and bool(getattr(self._settings, "avito_allow_chats_without_item", True))
        )

    async def is_allowed(self, chat_id: str) -> bool:
        """Можно ли писать в этот чат — по правилу `is_listing_allowed`.

        Ошибка поиска item_id — это ЗАПРЕТ, а не разрешение. Чат без
        объявления и чат, про который мы НЕ СМОГЛИ УЗНАТЬ, есть ли у него
        объявление, — разные вещи: первое штатно разрешено
        (`AVITO_ALLOW_CHATS_WITHOUT_ITEM`), второе означает, что проверка не
        отработала, и подменять её результат догадкой нельзя.
        """
        if self._manual_hold_lookup is not None:
            try:
                if await self._manual_hold_lookup(chat_id):
                    logger.info(
                        "outbound: заблокировано — ручной hold",
                        extra={"chat_id": chat_id},
                    )
                    return False
            except Exception:
                # Как и с item_id ниже: сбой проверки — это запрет, а не
                # разрешение. Chat.manual_hold ставят именно на инцидент —
                # молча пропустить отправку из-за упавшего SELECT было бы
                # ровно той ошибкой, ради которой hold и заводили.
                logger.exception(
                    "outbound: не удалось проверить manual_hold — отправка заблокирована",
                    extra={"chat_id": chat_id},
                )
                return False

        # Перехват чата человеком — сразу после ручного hold и по тем же
        # правилам обработки сбоя. Порядок с hold именно такой: hold строже
        # (снимается только руками), и если стоят оба, в логе должен быть
        # виден он, а не кулдаун, который истечёт сам.
        if self._takeover_lookup is not None:
            try:
                state = await self._takeover_lookup(chat_id)
            except Exception:
                logger.exception(
                    "outbound: не удалось проверить перехват чата — отправка заблокирована",
                    extra={"chat_id": chat_id},
                )
                return False
            if takeover_blocks(state, self._settings):
                logger.info(
                    "outbound: заблокировано — в чате живой менеджер (режим %s)",
                    getattr(self._settings, "takeover_mode", "cooldown"),
                    extra={"chat_id": chat_id},
                )
                return False

        if self._filter_is_off():
            return True

        if self._item_id_lookup is None:
            logger.warning(
                "outbound: фильтр объявлений включён, но искать item_id нечем — "
                "отправка заблокирована",
                extra={"chat_id": chat_id},
            )
            return False

        try:
            item_id = await self._item_id_lookup(chat_id)
        except Exception:
            logger.exception(
                "outbound: не удалось определить item_id чата — отправка заблокирована",
                extra={"chat_id": chat_id},
            )
            return False

        if await is_listing_allowed(
            item_id, self._settings, scope_resolver=self._item_scope_resolver
        ):
            return True

        logger.info(
            "outbound: заблокировано — объявление %s под запретом",
            item_id if item_id is not None else "(чат без объявления)",
            extra={"chat_id": chat_id, "item_id": item_id},
        )
        return False

    # -- отправка ----------------------------------------------------------

    async def _require_allowed(self, chat_id: str) -> None:
        """Полная проверка перед РЕАЛЬНОЙ отправкой — в отличие от
        `is_allowed`, включает и рубильник, и суточный лимит.

        Оба НАМЕРЕННО не внутри `is_allowed`: он же служит `can_send`
        воркеру отложенных касаний (app/ops/touch_scheduler.py), и `False`
        там ГАСИТ ТАЙМЕР НАВСЕГДА — решение, уместное для постоянного
        «чат вне белого списка объявлений», но не для временного рубильника
        или суточного лимита, которые снимаются сами (рубильник — командой
        /resume, лимит — в полночь по Москве). Поставь их в `is_allowed`, и
        один инцидент навсегда стёр бы все напоминания, которые оказались
        due в эти минуты — тот же класс бага, ради которого сам `can_send`
        и появился (см. докстринг модуля).

        Порядок проверок важен: рубильник и белый список должны сработать
        РАНЬШЕ инкремента суточного счётчика — иначе сообщение, которое и
        так не ушло бы, всё равно съело бы место в лимите.
        """
        if self._kill_switch_lookup is not None:
            try:
                stopped = await self._kill_switch_lookup()
            except Exception:
                logger.exception(
                    "outbound: не удалось проверить kill switch — отправка заблокирована",
                    extra={"chat_id": chat_id},
                )
                stopped = True
            if stopped:
                logger.info(
                    "outbound: заблокировано — аварийный рубильник активен",
                    extra={"chat_id": chat_id},
                )
                raise SendingHalted(
                    "отправка остановлена аварийным рубильником — /resume в Telegram-боте"
                )

        if not await self.is_allowed(chat_id):
            raise ListingNotAllowed(
                f"чат {chat_id} не проходит белый список объявлений — отправка отменена"
            )

        if self._daily_limit_check is not None:
            result = await self._daily_limit_check()
            if (result.just_exceeded or result.redis_unavailable) and self._daily_limit_alert is not None:
                try:
                    await self._daily_limit_alert(result)
                except Exception:
                    logger.exception(
                        "outbound: не удалось отправить алерт о суточном лимите"
                    )
            if result.redis_unavailable:
                logger.error(
                    "outbound: заблокировано — суточный лимит не проверяется, Redis недоступен",
                    extra={"chat_id": chat_id},
                )
                raise DailyLimitUnavailable(
                    "суточный лимит исходящих недоступен для проверки — Redis не отвечает"
                )
            if not result.allowed:
                logger.warning(
                    "outbound: заблокировано — суточный лимит исчерпан (%d/%d)",
                    result.count, result.limit,
                    extra={"chat_id": chat_id},
                )
                raise DailyLimitExceeded(
                    f"суточный лимит исходящих ({result.limit}) исчерпан"
                )

    async def send_message(self, chat_id: str, text: str) -> dict:
        await self._require_allowed(chat_id)
        return await self._client.send_message(chat_id, text)

    async def send_image(self, chat_id: str, image_id: str) -> dict:
        await self._require_allowed(chat_id)
        return await self._client.send_image(chat_id, image_id)

    async def upload_and_send_image(self, chat_id: str, image_bytes: bytes, **kwargs) -> dict:
        # Проверка ДО загрузки картинки: не только не отправить, но и не
        # тратить трафик и лимиты Авито на чат, куда всё равно нельзя.
        await self._require_allowed(chat_id)
        return await self._client.upload_and_send_image(chat_id, image_bytes, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Всё остальное (чтение чатов, токены, aclose) — как у клиента.

        Намеренно только для того, что НЕ пишет клиенту: методы отправки
        перечислены выше явно и сюда не попадают. Если в `AvitoClient`
        когда-нибудь появится новый метод отправки, он проскочит через
        `__getattr__` без проверки — поэтому в tests/test_outbound_gate.py
        есть тест, который следит за списком методов клиента и падает,
        когда появляется незакрытый.
        """
        return getattr(self._client, name)
