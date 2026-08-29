"""app/channels/outbound_gate.py — одна дверь наружу для всех отправок.

Повод: белый список объявлений стоял только в конвейере, а выходов у
системы четыре. В 09:00 воркер касаний отправил третье касание в чат
u2u-…, в 12:09 конвейер тот же чат заблокировал — то есть касание ушло
клиенту, которому агент писать не должен.
"""

from __future__ import annotations

import inspect

import pytest

from app.channels.avito import AvitoClient
from app.channels.daily_limit import DailyLimitResult
from app.channels.outbound_gate import (
    DailyLimitExceeded,
    DailyLimitUnavailable,
    ListingNotAllowed,
    OutboundGate,
    SendingHalted,
)
from app.config import Settings


class _FakeClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.images: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> dict:
        self.sent.append((chat_id, text))
        return {"ok": True}

    async def send_image(self, chat_id: str, image_id: str) -> dict:
        self.images.append((chat_id, image_id))
        return {"ok": True}

    async def upload_and_send_image(self, chat_id, image_bytes, **kwargs) -> dict:
        self.images.append((chat_id, "uploaded"))
        return {"ok": True}

    async def get_chat(self, chat_id: str) -> dict:
        return {"id": chat_id}


def _gate(
    items: str = "", chats: dict | None = None, blocked: str = "",
) -> tuple[OutboundGate, _FakeClient]:
    chats = chats if chats is not None else {}

    async def lookup(chat_id: str):
        return chats.get(chat_id)

    client = _FakeClient()
    settings = Settings(avito_allowed_items=items, avito_blocked_items=blocked)
    return OutboundGate(client, settings, lookup), client


# --------------------------------------------------------------------------
# Решение
# --------------------------------------------------------------------------

async def test_empty_allowlist_allows_everything():
    """Переменная не задана — гейт прозрачен, как будто его нет."""
    gate, client = _gate(items="")

    await gate.send_message("chat-1", "привет")

    assert client.sent == [("chat-1", "привет")]


async def test_allowed_listing_passes_through():
    gate, client = _gate(items="item-1", chats={"chat-1": "item-1"})

    await gate.send_message("chat-1", "привет")

    assert client.sent == [("chat-1", "привет")]


async def test_listing_outside_the_allowlist_is_blocked():
    gate, client = _gate(items="item-1", chats={"chat-1": "item-vacancy"})

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("chat-1", "привет")

    assert client.sent == []


async def test_chat_without_a_listing_is_allowed_by_default():
    """Чат u2u/a2u — обращение из профиля продавца, объявления у него нет
    по спеку Авито. Это живой клиент: агент отвечает, молчание хуже."""
    gate, client = _gate(items="item-1", chats={})

    await gate.send_message("u2u-2QuAfvI4HoxsE7IKKDN3SA", "Здравствуйте!")

    assert client.sent == [("u2u-2QuAfvI4HoxsE7IKKDN3SA", "Здравствуйте!")]


async def test_chat_without_a_listing_can_be_blocked_by_the_flag():
    """AVITO_ALLOW_CHATS_WITHOUT_ITEM=false возвращает прежнее поведение —
    рубильник на случай, если из профиля польётся мусор."""
    async def lookup(chat_id: str):
        return None

    client = _FakeClient()
    gate = OutboundGate(
        client,
        Settings(avito_blocked_items="item-x", avito_allow_chats_without_item=False),
        lookup,
    )

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("u2u-chat", "Здравствуйте!")

    assert client.sent == []


# --------------------------------------------------------------------------
# Чёрный список — основной режим
# --------------------------------------------------------------------------

async def test_blocked_listing_is_rejected():
    """Пять посторонних объявлений заказчика — вакансия, продажа бизнеса,
    квартира. Ответ про бани такому клиенту хуже молчания."""
    gate, client = _gate(blocked="8204183112", chats={"chat-1": "8204183112"})

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("chat-1", "Баня свободна!")

    assert client.sent == []


async def test_a_listing_outside_the_blocklist_passes():
    """Главная причина замены белого списка чёрным: новое объявление
    комплекса работает сразу, без правки переменной."""
    gate, client = _gate(blocked="8204183112", chats={"chat-1": "9999-новое"})

    await gate.send_message("chat-1", "Здравствуйте!")

    assert client.sent == [("chat-1", "Здравствуйте!")]


async def test_the_default_blocked_listings_are_blocked_without_any_env_var():
    """Переменную могут забыть выставить на новом стенде — тогда посторонние
    объявления снова начнут получать прайс на бани. DEFAULT_BLOCKED_ITEMS
    зашит значением по умолчанию именно поэтому."""
    from app.config import DEFAULT_BLOCKED_ITEMS

    for item_id in DEFAULT_BLOCKED_ITEMS:
        async def lookup(chat_id: str, _item=item_id):
            return _item

        client = _FakeClient()
        gate = OutboundGate(client, Settings(), lookup)   # никаких переменных

        with pytest.raises(ListingNotAllowed):
            await gate.send_message("chat-1", "Баня свободна!")
        assert client.sent == []


async def test_allowlist_wins_over_the_blocklist_when_set():
    """Совместимость: где AVITO_ALLOWED_ITEMS уже выставлен, он и решает."""
    gate, client = _gate(items="item-1", blocked="item-1", chats={"chat-1": "item-1"})

    await gate.send_message("chat-1", "Здравствуйте!")

    assert client.sent == [("chat-1", "Здравствуйте!")]


async def test_lookup_failure_blocks_rather_than_allows():
    """«Не смогли проверить» — это запрет. Список задан явно, значит
    оператор перечислил, кому писать можно; сбой поиска не повод расширять
    этот список до всех."""
    async def broken(chat_id: str):
        raise RuntimeError("БД недоступна")

    client = _FakeClient()
    gate = OutboundGate(client, Settings(avito_allowed_items="item-1"), broken)

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("chat-1", "привет")

    assert client.sent == []


async def test_missing_lookup_blocks_when_the_allowlist_is_set():
    """Гейт собрали без поиска item_id, а список задан — единственный
    безопасный выбор здесь тоже запрет."""
    client = _FakeClient()
    gate = OutboundGate(client, Settings(avito_allowed_items="item-1"), None)

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("chat-1", "привет")

    assert client.sent == []


# --------------------------------------------------------------------------
# Картинки и прочие методы
# --------------------------------------------------------------------------

async def test_images_are_gated_too():
    gate, client = _gate(items="item-1", chats={"chat-1": "item-vacancy"})

    with pytest.raises(ListingNotAllowed):
        await gate.send_image("chat-1", "img-1")
    with pytest.raises(ListingNotAllowed):
        await gate.upload_and_send_image("chat-1", b"bytes")

    assert client.images == []


async def test_reading_methods_are_not_gated():
    """Гейт закрывает запись клиенту, а не чтение: get_chat нужен самому
    конвейеру, чтобы восстановить item_id, — заблокировав его, мы бы
    закрыли и способ узнать, что чат вообще-то разрешён."""
    gate, client = _gate(items="item-1", chats={})

    assert await gate.get_chat("chat-1") == {"id": "chat-1"}


# --------------------------------------------------------------------------
# Аварийный рубильник (/stop, /resume)
# --------------------------------------------------------------------------

async def test_kill_switch_blocks_sending():
    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        kill_switch_lookup=lambda: _async(True),
    )

    with pytest.raises(SendingHalted):
        await gate.send_message("chat-1", "привет")

    assert client.sent == []


async def test_kill_switch_off_lets_messages_through():
    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        kill_switch_lookup=lambda: _async(False),
    )

    await gate.send_message("chat-1", "привет")

    assert client.sent == [("chat-1", "привет")]


async def test_kill_switch_lookup_failure_blocks_rather_than_allows():
    async def broken():
        raise RuntimeError("Redis недоступен")

    client = _FakeClient()
    gate = OutboundGate(client, Settings(avito_blocked_items="none"), kill_switch_lookup=broken)

    with pytest.raises(SendingHalted):
        await gate.send_message("chat-1", "привет")

    assert client.sent == []


async def test_no_kill_switch_lookup_means_transparent():
    """Гейт без колбэка (тесты, где рубильник не собирают) ведёт себя как
    раньше — конструктор его не требует."""
    gate, client = _gate(items="")

    await gate.send_message("chat-1", "привет")

    assert client.sent == [("chat-1", "привет")]


async def test_kill_switch_does_not_affect_is_allowed():
    """`is_allowed` — это ещё и `can_send` воркера отложенных касаний, и
    `False` там ГАСИТ ТАЙМЕР НАВСЕГДА. Рубильник временный (снимается
    /resume), поэтому не должен туда просачиваться — иначе один инцидент
    навсегда стёр бы напоминания, которые были due в эти минуты."""
    client = _FakeClient()
    gate = OutboundGate(client, Settings(avito_blocked_items="none"), kill_switch_lookup=lambda: _async(True))

    assert await gate.is_allowed("chat-1") is True


# --------------------------------------------------------------------------
# Суточный лимит
# --------------------------------------------------------------------------

def _limit_result(
    allowed: bool, count: int, limit: int, just_exceeded: bool = False, redis_unavailable: bool = False,
):
    return DailyLimitResult(
        allowed=allowed, count=count, limit=limit,
        just_exceeded=just_exceeded, redis_unavailable=redis_unavailable,
    )


async def test_daily_limit_blocks_when_exceeded():
    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        daily_limit_check=lambda: _async(_limit_result(False, 11, 10)),
    )

    with pytest.raises(DailyLimitExceeded):
        await gate.send_message("chat-1", "привет")

    assert client.sent == []


async def test_daily_limit_allows_when_under():
    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        daily_limit_check=lambda: _async(_limit_result(True, 3, 10)),
    )

    await gate.send_message("chat-1", "привет")

    assert client.sent == [("chat-1", "привет")]


async def test_daily_limit_alert_fires_on_just_exceeded():
    alerts = []

    async def alert(result):
        alerts.append(result)

    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        daily_limit_check=lambda: _async(_limit_result(False, 11, 10, just_exceeded=True)),
        daily_limit_alert=alert,
    )

    with pytest.raises(DailyLimitExceeded):
        await gate.send_message("chat-1", "привет")

    assert len(alerts) == 1
    assert (alerts[0].count, alerts[0].limit) == (11, 10)


async def test_daily_limit_alert_does_not_fire_when_not_just_exceeded():
    """Сообщение N+2 и далее уже заблокировано, но алерт не должен
    повторяться на каждое из них до конца суток."""
    alerts = []

    async def alert(result):
        alerts.append(result)

    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        daily_limit_check=lambda: _async(_limit_result(False, 15, 10, just_exceeded=False)),
        daily_limit_alert=alert,
    )

    with pytest.raises(DailyLimitExceeded):
        await gate.send_message("chat-1", "привет")

    assert alerts == []


async def test_daily_limit_alert_failure_does_not_block_the_exception():
    """Сбой отправки алерта в Telegram — это отдельная проблема, она не
    должна маскировать сам факт, что лимит исчерпан."""
    async def broken_alert(result):
        raise RuntimeError("Telegram недоступен")

    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        daily_limit_check=lambda: _async(_limit_result(False, 11, 10, just_exceeded=True)),
        daily_limit_alert=broken_alert,
    )

    with pytest.raises(DailyLimitExceeded):
        await gate.send_message("chat-1", "привет")


# --------------------------------------------------------------------------
# Суточный лимит — авария Redis: fail closed, а не «продолжаем как обычно»
# --------------------------------------------------------------------------

async def test_daily_limit_redis_unavailable_blocks_sending():
    """Раньше (fail open) сообщение прошло бы. Redis, которым считается
    лимит, упал — это ровно момент, когда предохранитель нужнее всего, а
    не повод его отключить."""
    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        daily_limit_check=lambda: _async(_limit_result(False, -1, 10, redis_unavailable=True)),
    )

    with pytest.raises(DailyLimitUnavailable):
        await gate.send_message("chat-1", "привет")

    assert client.sent == []


async def test_daily_limit_redis_unavailable_alerts_every_time_not_once():
    """В отличие от `just_exceeded` (алерт ровно один раз), авария Redis
    не даёт способа отличить первую заблокированную попытку от сотой —
    молчать после первого алерта в длящейся аварии хуже, чем повторяться."""
    alerts = []

    async def alert(result):
        alerts.append(result)

    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        daily_limit_check=lambda: _async(_limit_result(False, -1, 10, redis_unavailable=True)),
        daily_limit_alert=alert,
    )

    for _ in range(3):
        with pytest.raises(DailyLimitUnavailable):
            await gate.send_message("chat-1", "привет")

    assert len(alerts) == 3
    assert all(a.redis_unavailable for a in alerts)


async def test_daily_limit_unavailable_is_distinguishable_from_exceeded():
    """Вызывающий код (и алерт) должен уметь различить «лимит исчерпан» и
    «не смогли проверить» — это разные exception и разный текст оператору."""
    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        daily_limit_check=lambda: _async(_limit_result(False, -1, 10, redis_unavailable=True)),
    )

    with pytest.raises(DailyLimitUnavailable) as exc_info:
        await gate.send_message("chat-1", "привет")
    assert not isinstance(exc_info.value, DailyLimitExceeded)


# --------------------------------------------------------------------------
# Порядок проверок
# --------------------------------------------------------------------------

async def test_kill_switch_checked_before_daily_limit_counter():
    """Рубильник должен отсечь сообщение РАНЬШЕ инкремента суточного
    счётчика — иначе сообщение, которое и так не уйдёт, съедает лимит."""
    calls = []

    async def daily_check():
        calls.append("daily")
        return _limit_result(True, 1, 10)

    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_blocked_items="none"),
        kill_switch_lookup=lambda: _async(True),
        daily_limit_check=daily_check,
    )

    with pytest.raises(SendingHalted):
        await gate.send_message("chat-1", "привет")

    assert calls == []


async def test_listing_filter_checked_before_daily_limit_counter():
    calls = []

    async def daily_check():
        calls.append("daily")
        return _limit_result(True, 1, 10)

    async def lookup(chat_id: str):
        return "item-vacancy"

    client = _FakeClient()
    gate = OutboundGate(
        client, Settings(avito_allowed_items="item-1"), lookup,
        daily_limit_check=daily_check,
    )

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("chat-1", "привет")

    assert calls == []


# --------------------------------------------------------------------------
# item_scope — классификация по заголовку вместо зашитого списка
# --------------------------------------------------------------------------

async def test_item_scope_resolver_decides_when_wired():
    from app.channels.item_scope import InMemoryItemScopeStore, ItemScopeResolver

    store = InMemoryItemScopeStore()
    await store.upsert("item-baня", title="Баня", decision="allow", reason="title_matches_allow")
    resolver = ItemScopeResolver(store, Settings())

    async def lookup(chat_id: str):
        return "item-baня"

    client = _FakeClient()
    gate = OutboundGate(client, Settings(), lookup, item_scope_resolver=resolver)

    await gate.send_message("chat-1", "привет")

    assert client.sent == [("chat-1", "привет")]


async def test_item_scope_resolver_can_deny_even_without_a_blocklist():
    """С резолвером фильтр никогда не «выключен полностью» — жёсткий deny
    (item_id == "0", объявление не нашего аккаунта) обязан сработать даже
    когда AVITO_BLOCKED_ITEMS пуст."""
    from app.channels.item_scope import InMemoryItemScopeStore, ItemScopeResolver

    resolver = ItemScopeResolver(InMemoryItemScopeStore(), Settings(avito_blocked_items="none"))

    async def lookup(chat_id: str):
        return "0"   # item_id == 0 — см. app/channels/item_scope.py:ZERO_ITEM_ID

    client = _FakeClient()
    gate = OutboundGate(client, Settings(avito_blocked_items="none"), lookup, item_scope_resolver=resolver)

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("chat-1", "привет")

    assert client.sent == []


async def test_foreign_item_is_denied_through_the_gate():
    """33 чата на 1100: владелец аккаунта сам покупатель. Заголовок не
    матчит ни одно deny-слово — без гуарда «объявление не наше» такой чат
    прошёл бы обычную классификацию (allow)."""
    from app.channels.item_scope import InMemoryItemScopeStore, ItemScopeResolver

    async def own_items():
        return {"111"}   # объявления комплекса — репетитора среди них нет

    resolver = ItemScopeResolver(
        InMemoryItemScopeStore(), Settings(), own_items_provider=own_items
    )

    async def lookup(chat_id: str):
        return "999-repetitor"

    client = _FakeClient()
    gate = OutboundGate(client, Settings(), lookup, item_scope_resolver=resolver)

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("chat-1", "Здравствуйте, физика ЕГЭ?")

    assert client.sent == []


async def test_hard_blocklist_beats_item_scope_allow_classification():
    """Требование 6: даже если item_scope уже закешировал allow (например,
    из позавчерашнего часового прохода до правки списка), жёсткий deny по
    id обязан победить на следующем же вызове."""
    from app.channels.item_scope import InMemoryItemScopeStore, ItemScopeResolver

    store = InMemoryItemScopeStore()
    await store.upsert(
        "7980739861", title="Продажа банного комплекса",
        decision="allow", reason="title_matches_allow",  # устаревшее/ошибочное значение
    )
    settings = Settings(avito_blocked_items="7980739861")
    resolver = ItemScopeResolver(store, settings)

    async def lookup(chat_id: str):
        return "7980739861"

    client = _FakeClient()
    gate = OutboundGate(client, settings, lookup, item_scope_resolver=resolver)

    with pytest.raises(ListingNotAllowed):
        await gate.send_message("chat-1", "Баня свободна?")

    assert client.sent == []


async def _async(value):
    return value


def test_every_sending_method_of_the_client_is_covered_by_the_gate():
    """Страховка от тихой дыры в будущем: `__getattr__` пропускает наружу
    любой метод клиента, которого гейт не знает. Если в AvitoClient
    появится новый способ написать клиенту, а в гейте — нет, этот тест
    падает, а не узнаём мы об этом из логов, как в прошлый раз."""
    sending = {
        name for name, _ in inspect.getmembers(AvitoClient, inspect.isfunction)
        if name.startswith("send") or name.startswith("upload_and_send")
    }
    covered = {
        name for name, _ in inspect.getmembers(OutboundGate, inspect.isfunction)
        if name.startswith("send") or name.startswith("upload_and_send")
    }

    assert sending <= covered, (
        f"в AvitoClient есть методы отправки без проверки в OutboundGate: {sending - covered}"
    )
