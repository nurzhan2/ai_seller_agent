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
from app.channels.outbound_gate import ListingNotAllowed, OutboundGate
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


async def test_the_five_default_blocked_listings_are_blocked_without_any_env_var():
    """Переменную могут забыть выставить на новом стенде — тогда посторонние
    объявления снова начнут получать прайс на бани. Пять id зашиты
    значением по умолчанию именно поэтому."""
    for item_id in ("8204183112", "8076244626", "8076019723", "7980739861", "8172444564"):
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
