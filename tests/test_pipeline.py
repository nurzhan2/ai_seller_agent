"""Сквозной путь: входящее → дедуп → debounce → агент → ответ.

Через `InMemoryDialogStore` и фейковый `AgentLoop` — без Postgres, Redis,
Авито и настоящей модели. Проверяется именно СКЛЕЙКА кусков (кто кого зовёт
и в каком порядке), а не их внутренности: движок цен, уступки и переходы
таймера покрыты своими тестами.

Окно debounce везде выставлено в 0 — тесту не нужно ждать реальные 10 секунд,
чтобы проверить, что накопленное склеилось и ушло агенту одним ходом.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.agent.loop import TurnResult
from app.agent.touch_tracking import TouchState, is_due
from app.config import Settings
from app.db.models import Author, Direction, SendStatus
from app.dialog_store import InMemoryDialogStore
from app.kb.loader import load_catalog
from app.ops.bot import OpsService
from app.ops.state import InMemoryOpsStore, PendingReply
from app.pipeline import MessagePipeline
from app.pricing.concessions import ConcessionDecision, ConcessionEvent, DialogConcessionState
from app.pricing.engine import PriceQuote

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

OUR_USER_ID = "seller-1"


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


def _payload(
    chat_id: str = "chat-1",
    text: str = "Здравствуйте, сколько стоит баня?",
    message_id: str = "msg-1",
    author_id: str = "buyer-9",
    item_id: str | None = "item-1",
) -> dict:
    value: dict = {
        "id": message_id,
        "chat_id": chat_id,
        "author_id": author_id,
        "content": {"text": text},
    }
    if item_id is not None:
        value["item_id"] = item_id
    return {"payload": {"value": value}}


def _image_payload(
    chat_id: str = "chat-1",
    message_id: str = "msg-img-1",
    author_id: str = "buyer-9",
    item_id: str | None = "item-1",
) -> dict:
    """Фото без подписи — по общеизвестной (не подтверждённой спеком) форме
    мессенджер-вебхука Авито: type="image", content.image вместо content.text."""
    value: dict = {
        "id": message_id,
        "chat_id": chat_id,
        "author_id": author_id,
        "type": "image",
        "content": {"image": {"sizes": {"140x105": "https://example.com/photo.jpg"}}},
    }
    if item_id is not None:
        value["item_id"] = item_id
    return {"payload": {"value": value}}


def _unrecognized_typeless_payload(chat_id: str = "chat-1", message_id: str = "msg-sys-1") -> dict:
    """Ни текста, ни распознанного типа — системное событие или структура,
    отличная от ожидаемой. Не должно ни звать агента, ни отвечать шаблоном:
    только лог с диагностикой (см. test_textless_payload_of_unknown_type_*)."""
    return {
        "payload": {
            "value": {
                "id": message_id,
                "chat_id": chat_id,
                "author_id": "buyer-9",
                "type": "call_missed",
            }
        }
    }


class _FakeAgentLoop:
    """Записывает, с чем его позвали, и отдаёт заранее заданный результат."""

    def __init__(self, result: TurnResult | None = None):
        self.calls: list[dict] = []
        self.result = result or TurnResult(text="Добрый день! Уточните дату, пожалуйста.")

    async def run_turn(self, dialog_id, history, user_text, state=None, item_id=None, item_lookup=None):
        self.calls.append(
            {
                "dialog_id": dialog_id,
                "history": history,
                "user_text": user_text,
                "state": state,
                "item_id": item_id,
            }
        )
        return self.result


class _FakeAvito:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: str, text: str) -> dict:
        if self.fail:
            raise RuntimeError("Avito 503")
        self.sent.append((chat_id, text))
        return {"ok": True}


class _FakeOpsBot:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append({"chat_id": chat_id, "text": text})


def _settings(**overrides) -> Settings:
    base = dict(
        dry_run=True,
        avito_user_id=OUR_USER_ID,
        debounce_window_seconds=0,
        telegram_ops_chat_id="",
        telegram_allowed_users=[1],
        touch_reminder_delay_minutes=30,
        touch_max_count=3,
    )
    base.update(overrides)
    return Settings(**base)


def _build(
    *,
    settings: Settings | None = None,
    store: InMemoryDialogStore | None = None,
    agent: _FakeAgentLoop | None = None,
    avito: _FakeAvito | None = None,
    ops_bot: _FakeOpsBot | None = None,
):
    settings = settings or _settings()
    store = store or InMemoryDialogStore()
    agent = agent or _FakeAgentLoop()
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    pipeline = MessagePipeline(
        store=store,
        agent_loop=agent,
        ops_service=ops_service,
        settings=settings,
        avito_client=avito,
        ops_bot=ops_bot,
        debounce_window_seconds=0,
        now_fn=lambda: NOW,
    )
    return pipeline, store, agent, ops_service


async def _settle():
    """Debounce отдаёт склейку из отдельной задачи — даём ей провернуться."""
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# Сквозной путь
# --------------------------------------------------------------------------

async def test_incoming_message_reaches_the_agent_and_answer_goes_to_moderation():
    pipeline, store, agent, ops_service = _build()

    await pipeline.handle_message(_payload())
    await _settle()

    assert len(agent.calls) == 1
    assert agent.calls[0]["user_text"] == "Здравствуйте, сколько стоит баня?"
    assert agent.calls[0]["dialog_id"] == "chat-1"

    # DRY_RUN: ответ в очереди модерации, а не у клиента.
    pending = await ops_service.store.get_pending("chat-1")
    assert pending is not None
    assert pending.text == "Добрый день! Уточните дату, пожалуйста."


async def test_incoming_is_saved_before_the_agent_runs():
    pipeline, store, agent, _ = _build()

    await pipeline.handle_message(_payload(text="Привет"))
    await _settle()

    saved = store.messages["chat-1"]
    assert saved[0]["direction"] == Direction.incoming
    assert saved[0]["author"] == Author.client
    assert saved[0]["text"] == "Привет"


async def test_chat_is_created_with_item_id_and_zone_from_the_mapping():
    from app.agent.listing_context import ItemZoneRow

    store = InMemoryDialogStore(item_zones={"item-1": ItemZoneRow(zone_id="bath_russian")})
    pipeline, store, agent, _ = _build(store=store)

    await pipeline.handle_message(_payload())
    await _settle()

    chat = store.chats["chat-1"]
    assert chat.item_id == "item-1"
    assert chat.zone_id == "bath_russian"
    # item_id доезжает до агента — иначе подсказка о зоне не построится.
    assert agent.calls[0]["item_id"] == "item-1"


async def test_several_messages_in_the_window_reach_the_agent_as_one_turn():
    """Ровно то, ради чего debounce существует: клиент пишет очередью, а
    агент отвечает один раз и уже зная всё, что тот дописал."""
    pipeline, store, agent, _ = _build()

    await pipeline.handle_message(_payload(text="Здравствуйте", message_id="m1"))
    await pipeline.handle_message(_payload(text="а сколько стоит", message_id="m2"))
    await pipeline.handle_message(_payload(text="на субботу", message_id="m3"))
    await _settle()

    assert len(agent.calls) == 1
    assert agent.calls[0]["user_text"] == "Здравствуйте\nа сколько стоит\nна субботу"


# --------------------------------------------------------------------------
# Дедупликация
# --------------------------------------------------------------------------

async def test_repeated_webhook_with_the_same_message_id_does_nothing():
    pipeline, store, agent, _ = _build()

    await pipeline.handle_message(_payload(message_id="msg-42"))
    await _settle()
    await pipeline.handle_message(_payload(message_id="msg-42"))
    await _settle()

    assert len(agent.calls) == 1
    assert len([m for m in store.messages["chat-1"] if m["direction"] == Direction.incoming]) == 1


async def test_duplicate_does_not_reset_the_touch_timer_again():
    """Дубль не должен выглядеть как новая активность клиента — иначе ретрай
    вебхука бесконечно отодвигал бы напоминание."""
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)

    await pipeline.handle_message(_payload(message_id="msg-7"))
    await _settle()

    # Ставим таймер вручную, как будто цена уже названа и касание запланировано.
    concession, _touch = await store.load_dialog_state("chat-1")
    await store.save_dialog_state(
        "chat-1", concession, TouchState(touch_count=1, next_touch_due_at=NOW + timedelta(minutes=30))
    )

    await pipeline.handle_message(_payload(message_id="msg-7"))   # тот же id
    await _settle()

    _c, touch = await store.load_dialog_state("chat-1")
    assert touch.next_touch_due_at == NOW + timedelta(minutes=30)   # не сброшен


async def test_our_own_message_echo_is_ignored():
    """Авито шлёт вебхук и на наши исходящие — без этой проверки агент
    отвечает сам себе по кругу."""
    pipeline, store, agent, _ = _build()

    await pipeline.handle_message(_payload(author_id=OUR_USER_ID))
    await _settle()

    assert agent.calls == []
    assert store.messages == {}


# --------------------------------------------------------------------------
# Перехват оператором
# --------------------------------------------------------------------------

async def test_agent_is_not_called_when_the_chat_is_taken_over():
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    await store.get_or_create_chat("chat-1")
    store.chats["chat-1"] = store.chats["chat-1"].__class__(
        chat_id="chat-1", is_human_takeover=True
    )

    await pipeline.handle_message(_payload())
    await _settle()

    assert agent.calls == []


async def test_incoming_is_still_saved_when_the_chat_is_taken_over():
    """Оператор ведёт диалог сам, но переписка обязана остаться в БД —
    иначе история для агента порвётся на месте перехвата."""
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    await store.get_or_create_chat("chat-1")
    store.chats["chat-1"] = store.chats["chat-1"].__class__(
        chat_id="chat-1", is_human_takeover=True
    )

    await pipeline.handle_message(_payload(text="Я подумаю"))
    await _settle()

    assert store.messages["chat-1"][0]["text"] == "Я подумаю"


async def test_takeover_during_the_debounce_window_stops_the_turn():
    """Окно длится десятки секунд — оператор успевает нажать кнопку ровно
    посередине, и ход агента после этого запускаться не должен."""
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)

    await pipeline.handle_message(_payload())
    # Перехват уже после submit, но до срабатывания окна.
    store.chats["chat-1"] = store.chats["chat-1"].__class__(
        chat_id="chat-1", is_human_takeover=True
    )
    await _settle()

    assert agent.calls == []


async def test_reply_limit_actually_stops_the_agent():
    """Предохранитель от зацикливания обязан срабатывать по-настоящему.

    `should_agent_reply` сверяет лимит по `ChatFlags.agent_reply_count` в
    `OpsStore`, а конвейер пишет ещё и в `Chat.agent_reply_count` в БД. Если
    обновлять только БД, лимит не сработает никогда — проверяющая сторона о
    нём просто не узнает. Тест ловит именно этот разрыв.
    """
    settings = _settings(max_agent_replies_per_chat=2)
    pipeline, store, agent, ops_service = _build(settings=settings)

    for i in range(4):
        await pipeline.handle_message(_payload(message_id=f"m-{i}", text=f"вопрос {i}"))
        await _settle()

    assert len(agent.calls) == 2
    allowed, reason = await ops_service.should_agent_reply("chat-1")
    assert allowed is False
    assert "лимит" in reason


async def test_reply_count_is_written_to_the_database_too():
    """Вторая половина той же пары: счётчик в БД — то, что переживает
    рестарт и видно в админке."""
    pipeline, store, agent, _ = _build()

    await pipeline.handle_message(_payload(message_id="m-1"))
    await _settle()

    assert store.chats["chat-1"].agent_reply_count == 1


async def test_paused_agent_does_not_reply():
    settings = _settings(agent_paused=True)
    pipeline, store, agent, _ = _build(settings=settings)

    await pipeline.handle_message(_payload())
    await _settle()

    assert agent.calls == []


# --------------------------------------------------------------------------
# Сброс таймера касаний ответом клиента
# --------------------------------------------------------------------------

async def test_client_reply_resets_the_touch_timer():
    """Главный баг, ради которого всё это собиралось: без сброса второе
    касание уходит человеку, который только что ответил."""
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    await store.save_dialog_state(
        "chat-1",
        DialogConcessionState(base_price_quoted=True, touch_count=1),
        TouchState(touch_count=1, last_touch_at=NOW, next_touch_due_at=NOW + timedelta(minutes=30)),
    )

    await pipeline.handle_message(_payload(text="Да, интересно"))

    _concession, touch = await store.load_dialog_state("chat-1")
    assert touch.next_touch_due_at is None
    assert touch.touch_count == 1   # уже отправленные касания не «отменяются»


async def test_after_a_client_reply_the_scheduler_no_longer_considers_the_dialog_due():
    """Тот же сброс, но проверенный так, как его увидит воркер: диалог
    перестаёт быть «созревшим» для касания."""
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    overdue = TouchState(touch_count=1, last_touch_at=NOW - timedelta(hours=1), next_touch_due_at=NOW - timedelta(minutes=1))
    await store.save_dialog_state("chat-1", DialogConcessionState(base_price_quoted=True), overdue)
    assert is_due(overdue, NOW, max_count=3)      # до ответа — созрел

    await pipeline.handle_message(_payload(text="Ой, я тут"))

    _c, touch = await store.load_dialog_state("chat-1")
    assert not is_due(touch, NOW, max_count=3)    # после ответа — нет


async def test_reply_without_text_still_resets_the_timer():
    """Клиент прислал фото без подписи — текста нет, но человек явно на
    связи, и напоминание ему уже не нужно."""
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    await store.save_dialog_state(
        "chat-1",
        DialogConcessionState(base_price_quoted=True),
        TouchState(touch_count=1, next_touch_due_at=NOW + timedelta(minutes=30)),
    )

    await pipeline.handle_message(_payload(text="   "))
    await _settle()

    _c, touch = await store.load_dialog_state("chat-1")
    assert touch.next_touch_due_at is None
    assert agent.calls == []      # но и агента дёргать не на что


# --------------------------------------------------------------------------
# Сообщение без текста: диагностика в логе, фото — не молчание
# --------------------------------------------------------------------------

async def test_image_without_text_gets_a_canned_reply_not_silence():
    """Худший вариант — молчание в ответ на активность клиента. Не ход
    агента (LLM не звали, текста для него нет) — фиксированная строка,
    но доставленная тем же путём, что и обычный ответ (DRY_RUN → очередь
    модерации)."""
    pipeline, store, agent, ops_service = _build()

    await pipeline.handle_message(_image_payload())
    await _settle()

    assert agent.calls == []      # не ход LLM — шаблон
    pending = await ops_service.store.get_pending("chat-1")
    assert pending is not None
    assert "Вижу фото" in pending.text


async def test_image_without_text_still_resets_the_touch_timer():
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    await store.save_dialog_state(
        "chat-1",
        DialogConcessionState(base_price_quoted=True),
        TouchState(touch_count=1, next_touch_due_at=NOW + timedelta(minutes=30)),
    )

    await pipeline.handle_message(_image_payload())
    await _settle()

    _c, touch = await store.load_dialog_state("chat-1")
    assert touch.next_touch_due_at is None


async def test_image_message_respects_human_takeover():
    """Оператор ведёт чат — автоматический «вижу фото» не должен встревать
    поверх живого человека, ровно как и обычный ход агента."""
    store = InMemoryDialogStore()
    pipeline, store, agent, ops_service = _build(store=store)
    await store.get_or_create_chat("chat-1")
    store.chats["chat-1"] = store.chats["chat-1"].__class__(
        chat_id="chat-1", is_human_takeover=True
    )

    await pipeline.handle_message(_image_payload())
    await _settle()

    assert agent.calls == []
    assert await ops_service.store.get_pending("chat-1") is None


async def test_image_message_respects_the_reply_limit():
    """Тот же предохранитель от зацикливания, что и у обычных ходов —
    шаблонный ответ на фото тоже считается ответом агента."""
    settings = _settings(max_agent_replies_per_chat=1)
    pipeline, store, agent, ops_service = _build(settings=settings)

    await pipeline.handle_message(_payload(message_id="m-0", text="вопрос"))
    await _settle()
    assert len(agent.calls) == 1

    await pipeline.handle_message(_image_payload(message_id="m-img"))
    await _settle()

    # Лимит уже исчерпан обычным ходом — шаблонный ответ на фото поверх
    # него не проскакивает мимо той же проверки.
    pending = await ops_service.store.get_pending("chat-1")
    assert pending is None or "Вижу фото" not in pending.text


async def test_textless_payload_logs_type_and_top_level_keys(caplog):
    """Ради этого лог и появился: раньше "message without text" не говорил,
    ЧТО пришло. Теперь тип и структура видны прямо в тексте сообщения — не
    только в extra, который обычный текстовый вывод logging не показывает."""
    pipeline, store, agent, _ = _build()

    with caplog.at_level("INFO", logger="parmangal.pipeline"):
        await pipeline.handle_message(_image_payload())
    await _settle()

    assert "message without text" in caplog.text
    assert "type=image" in caplog.text
    assert "chat_id" in caplog.text


async def test_textless_payload_masks_a_phone_number_in_the_log(caplog):
    """Без персональных данных — телефон не должен доехать до лога, даже
    если окажется под неожиданным ключом (структура не text — гарантий
    формы у нас нет, см. app/channels/avito_payloads.py)."""
    payload = _image_payload()
    payload["payload"]["value"]["author_phone"] = "+7 999 123-45-67"
    pipeline, store, agent, _ = _build()

    with caplog.at_level("INFO", logger="parmangal.pipeline"):
        await pipeline.handle_message(payload)
    await _settle()

    assert "999" not in caplog.text
    assert "123-45-67" not in caplog.text


async def test_textless_payload_of_unknown_type_is_logged_but_not_answered():
    """Ни текста, ни распознанного «это фото» — не ход агента, не шаблон,
    только диагностика в логе (уже проверена отдельным тестом выше)."""
    pipeline, store, agent, ops_service = _build()

    await pipeline.handle_message(_unrecognized_typeless_payload())
    await _settle()

    assert agent.calls == []
    assert await ops_service.store.get_pending("chat-1") is None


# --------------------------------------------------------------------------
# Фильтр по объявлениям (AVITO_ALLOWED_ITEMS)
#
# В аккаунте заказчика 22 объявления, 5 из них не про комплекс: вакансия
# менеджера, продажа глэмпинга за 39 млн, арендный бизнес, продажа банного
# комплекса, квартира-студия. Человек, спросивший про вакансию, получал
# прайс на бани.
# --------------------------------------------------------------------------

class _FakeAvitoWithChat(_FakeAvito):
    """`get_chat` для фолбэка item_id. `chat_calls` — чтобы проверить, что
    лишнего запроса к Авито не случилось."""

    def __init__(self, chat_response: dict | None = None, fail: bool = False):
        super().__init__()
        self.chat_response = chat_response or {}
        self.chat_calls: list[str] = []
        self.chat_fails = fail

    async def get_chat(self, chat_id: str) -> dict:
        self.chat_calls.append(chat_id)
        if self.chat_fails:
            raise RuntimeError("Avito 503")
        return self.chat_response


def _payload_without_item(chat_id: str = "chat-1", chat_type: str | None = None) -> dict:
    payload = _payload(chat_id=chat_id, item_id=None)
    if chat_type is not None:
        payload["payload"]["value"]["chat_type"] = chat_type
    return payload


async def test_message_from_a_listing_outside_the_allowlist_never_reaches_the_agent():
    """Главный сценарий: клиент пишет по вакансии — агент молчит и диалога
    не остаётся вовсе, ни в базе, ни у оператора."""
    settings = _settings(avito_allowed_items="item-1,item-2")
    pipeline, store, agent, ops_service = _build(settings=settings)

    await pipeline.handle_message(_payload(item_id="item-vacancy", text="Вакансия ещё актуальна?"))
    await _settle()

    assert agent.calls == []
    assert store.chats == {}                      # диалог не создан
    assert store.messages == {}                   # сообщение не сохранено
    assert await ops_service.store.get_pending("chat-1") is None


async def test_message_from_an_allowed_listing_goes_through():
    settings = _settings(avito_allowed_items="item-1,item-2")
    pipeline, store, agent, _ = _build(settings=settings)

    await pipeline.handle_message(_payload(item_id="item-2"))
    await _settle()

    assert len(agent.calls) == 1


async def test_empty_allowlist_lets_everything_through():
    """Переменная не задана — поведение ровно прежнее. Пустой список НЕ
    означает «запретить всё»: забытая переменная не должна превращать
    агента в молчуна."""
    pipeline, store, agent, _ = _build(settings=_settings())   # список пуст

    await pipeline.handle_message(_payload(item_id="item-какой-угодно"))
    await _settle()

    assert len(agent.calls) == 1


async def test_missing_item_id_is_blocked_when_the_allowlist_is_set():
    """«Объявление не определено» + заданный список = молчим. Ответить про
    бани человеку, спросившему про квартиру, хуже, чем не ответить."""
    settings = _settings(avito_allowed_items="item-1")
    pipeline, store, agent, _ = _build(settings=settings)

    await pipeline.handle_message(_payload_without_item())
    await _settle()

    assert agent.calls == []
    assert store.chats == {}


async def test_missing_item_id_passes_when_the_allowlist_is_empty():
    """Обратная сторона: без списка неизвестный item_id — не повод молчать,
    так работало всё это время."""
    pipeline, store, agent, _ = _build(settings=_settings())

    await pipeline.handle_message(_payload_without_item())
    await _settle()

    assert len(agent.calls) == 1


async def test_blocked_listing_is_logged_with_the_item_id(caplog):
    settings = _settings(avito_allowed_items="item-1")
    pipeline, store, agent, _ = _build(settings=settings)

    with caplog.at_level("INFO", logger="parmangal.pipeline"):
        await pipeline.handle_message(_payload(item_id="item-vacancy"))
    await _settle()

    assert "item-vacancy" in caplog.text
    assert "не в списке разрешённых" in caplog.text


async def test_missing_item_id_is_logged_separately_from_a_wrong_listing(caplog):
    """Отдельное сообщение — чтобы по логам было видно, НАСКОЛЬКО часто
    item_id теряется, а не путать это с чужими объявлениями."""
    settings = _settings(avito_allowed_items="item-1")
    pipeline, store, agent, _ = _build(settings=settings)

    with caplog.at_level("INFO", logger="parmangal.pipeline"):
        await pipeline.handle_message(_payload_without_item())
    await _settle()

    assert "item_id неизвестен" in caplog.text


# --------------------------------------------------------------------------
# Фолбэк item_id через get_chat
# --------------------------------------------------------------------------

async def test_item_id_is_recovered_from_get_chat_when_the_webhook_lacks_it():
    """Спек (schemas/Chat): context.type == "item", context.value.id — ID
    объявления. Восстановленный item_id должен и разблокировать фильтр, и
    доехать до Chat."""
    avito = _FakeAvitoWithChat(
        {"id": "chat-1", "context": {"type": "item", "value": {"id": 777, "title": "Баня"}}}
    )
    settings = _settings(avito_allowed_items="777")
    pipeline, store, agent, _ = _build(settings=settings, avito=avito)

    await pipeline.handle_message(_payload_without_item())
    await _settle()

    assert avito.chat_calls == ["chat-1"]
    assert len(agent.calls) == 1
    assert store.chats["chat-1"].item_id == "777"


async def test_profile_chats_do_not_trigger_a_get_chat_request():
    """chat_type u2u/a2u — чат по профилю продавца, объявления там нет в
    принципе (спек: item_id «актуально только для чатов с типом u2i»).
    Тратить запрос к Авито на заведомо пустой ответ незачем."""
    avito = _FakeAvitoWithChat()
    settings = _settings(avito_allowed_items="item-1")
    pipeline, store, agent, _ = _build(settings=settings, avito=avito)

    await pipeline.handle_message(_payload_without_item(chat_type="u2u"))
    await _settle()

    assert avito.chat_calls == []      # запроса не было
    assert agent.calls == []           # и ответа тоже


async def test_get_chat_failure_does_not_crash_the_turn():
    """Одно недостающее поле не должно ронять обработку: без списка
    разрешённых объявлений диалог продолжается как раньше."""
    avito = _FakeAvitoWithChat(fail=True)
    pipeline, store, agent, _ = _build(settings=_settings(), avito=avito)

    await pipeline.handle_message(_payload_without_item())
    await _settle()

    assert avito.chat_calls == ["chat-1"]
    assert len(agent.calls) == 1       # ход состоялся, просто без item_id


async def test_get_chat_is_not_called_when_the_webhook_already_has_item_id():
    """Лишний сетевой запрос на каждое сообщение — не мелочь: item_id есть
    в вебхуке почти всегда."""
    avito = _FakeAvitoWithChat()
    pipeline, store, agent, _ = _build(settings=_settings(), avito=avito)

    await pipeline.handle_message(_payload(item_id="item-1"))
    await _settle()

    assert avito.chat_calls == []


async def test_quoting_a_price_schedules_the_next_touch():
    agent = _FakeAgentLoop(
        TurnResult(
            text="Баня в субботу — 7 000 ₽ за 2 часа.",
            quote_statuses=["ok"],
            concession_state=DialogConcessionState(base_price_quoted=True, touch_count=1),
        )
    )
    pipeline, store, agent, _ = _build(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    _c, touch = await store.load_dialog_state("chat-1")
    assert touch.touch_count == 1
    assert touch.next_touch_due_at == NOW + timedelta(minutes=30)


async def test_no_price_yet_means_no_touch_is_scheduled():
    """Напоминание «вы где-то затерялись?» бессмысленно для клиента, который
    ещё не услышал ни одной цифры."""
    pipeline, store, agent, _ = _build()   # ответ без цены

    await pipeline.handle_message(_payload())
    await _settle()

    _c, touch = await store.load_dialog_state("chat-1")
    assert touch.next_touch_due_at is None
    assert touch.touch_count == 0


# --------------------------------------------------------------------------
# История диалога
# --------------------------------------------------------------------------

async def test_agent_receives_previous_messages_as_history():
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    await store.save_incoming("chat-1", "Здравствуйте", avito_message_id="old-1")
    await store.save_outgoing("chat-1", "Добрый день!", SendStatus.sent)

    await pipeline.handle_message(_payload(text="Сколько стоит?", message_id="new-1"))
    await _settle()

    history = agent.calls[0]["history"]
    assert {"role": "user", "content": "Здравствуйте"} in history
    assert {"role": "assistant", "content": "Добрый день!"} in history


async def test_history_is_capped_to_the_window():
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    for i in range(50):
        await store.save_incoming("chat-1", f"сообщение {i}", avito_message_id=f"old-{i}")

    await pipeline.handle_message(_payload(text="и ещё вопрос", message_id="new-1"))
    await _settle()

    assert len(agent.calls[0]["history"]) == 30


async def test_rejected_replies_never_enter_the_history():
    """Отклонённый оператором текст клиент не видел — показать его модели
    значит убедить её, что она уже это сказала."""
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    await store.save_outgoing("chat-1", "Неудачный ответ", SendStatus.rejected)

    await pipeline.handle_message(_payload())
    await _settle()

    contents = [m["content"] for m in agent.calls[0]["history"]]
    assert "Неудачный ответ" not in contents


async def test_dialog_state_is_loaded_into_the_turn():
    """Храповик работает только если пол цены доезжает до движка уступок —
    иначе после рестарта агент назовёт цену выше уже обещанной."""
    store = InMemoryDialogStore()
    pipeline, store, agent, _ = _build(store=store)
    await store.save_dialog_state(
        "chat-1",
        DialogConcessionState(base_price_quoted=True, floor_reached=Decimal("5000")),
        TouchState(),
    )

    await pipeline.handle_message(_payload())
    await _settle()

    assert agent.calls[0]["state"].floor_reached == Decimal("5000")


async def test_dialog_state_after_the_turn_is_persisted():
    agent = _FakeAgentLoop(
        TurnResult(
            text="Хорошо, 5 000 ₽.",
            concession_state=DialogConcessionState(
                base_price_quoted=True, used_tiers=frozenset({1}),
                floor_reached=Decimal("5000"), touch_count=1,
            ),
        )
    )
    pipeline, store, agent, _ = _build(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    concession, _touch = await store.load_dialog_state("chat-1")
    assert concession.floor_reached == Decimal("5000")
    assert concession.used_tiers == frozenset({1})


# --------------------------------------------------------------------------
# Сохранение исходящих и llm_meta
# --------------------------------------------------------------------------

async def test_outgoing_is_saved_with_llm_meta():
    meta = {"provider": "anthropic", "model": "claude-sonnet-5",
            "input_tokens": 1200, "output_tokens": 80, "cost_rub": "1.23"}
    agent = _FakeAgentLoop(TurnResult(text="Ответ агента", llm_meta=meta))
    pipeline, store, agent, _ = _build(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    outgoing = [m for m in store.messages["chat-1"] if m["direction"] == Direction.outgoing]
    assert len(outgoing) == 1
    assert outgoing[0]["llm_meta"] == meta
    assert outgoing[0]["author"] == Author.agent
    assert outgoing[0]["status"] == SendStatus.dry_run


async def test_live_send_marks_the_message_sent():
    avito = _FakeAvito()
    pipeline, store, agent, _ = _build(settings=_settings(dry_run=False), avito=avito)

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == [("chat-1", "Добрый день! Уточните дату, пожалуйста.")]
    outgoing = [m for m in store.messages["chat-1"] if m["direction"] == Direction.outgoing]
    assert outgoing[0]["status"] == SendStatus.sent


async def test_failed_send_is_recorded_as_failed_not_sent():
    avito = _FakeAvito(fail=True)
    pipeline, store, agent, _ = _build(settings=_settings(dry_run=False), avito=avito)

    await pipeline.handle_message(_payload())
    await _settle()

    outgoing = [m for m in store.messages["chat-1"] if m["direction"] == Direction.outgoing]
    assert outgoing[0]["status"] == SendStatus.failed


async def test_silent_turn_saves_no_outgoing_message():
    """Классификатор счёл входящее спамом — агент молчит, и в переписке
    не должно появиться пустой строки «от нас»."""
    agent = _FakeAgentLoop(TurnResult(text="", classification="spam"))
    pipeline, store, agent, _ = _build(agent=agent)

    await pipeline.handle_message(_payload(text="куплю дёшево гараж"))
    await _settle()

    outgoing = [m for m in store.messages["chat-1"] if m["direction"] == Direction.outgoing]
    assert outgoing == []


# --------------------------------------------------------------------------
# Уведомление оператора
# --------------------------------------------------------------------------

async def test_operator_gets_a_card_in_dry_run():
    bot = _FakeOpsBot()
    pipeline, store, agent, _ = _build(
        settings=_settings(telegram_ops_chat_id="-100500"), ops_bot=bot
    )

    await pipeline.handle_message(_payload())
    await _settle()

    assert len(bot.messages) == 1
    assert "НЕ ОТПРАВЛЕНО" in bot.messages[0]["text"]


async def test_missing_bot_does_not_break_the_turn():
    """Бот не настроен — ответ всё равно обязан попасть в очередь модерации."""
    pipeline, store, agent, ops_service = _build(ops_bot=None)

    await pipeline.handle_message(_payload())
    await _settle()

    assert await ops_service.store.get_pending("chat-1") is not None


async def test_escalation_notifies_the_operator_even_outside_dry_run():
    bot = _FakeOpsBot()
    agent = _FakeAgentLoop(
        TurnResult(text="Уточню у менеджера.", escalated=True, escalation_reason="клиент просит человека")
    )
    pipeline, store, agent, _ = _build(
        settings=_settings(dry_run=False, telegram_ops_chat_id="-100500"),
        agent=agent, avito=_FakeAvito(), ops_bot=bot,
    )

    await pipeline.handle_message(_payload())
    await _settle()

    assert len(bot.messages) == 1
    assert "Эскалация" in bot.messages[0]["text"]


# --------------------------------------------------------------------------
# Устойчивость
# --------------------------------------------------------------------------

async def test_agent_failure_does_not_escape_the_background_task():
    """Исключение отсюда всё равно некому поймать — Авито уже получил 200.
    Значит оно обязано быть залогировано, а не утонуть молча."""
    class _Boom:
        async def run_turn(self, **kwargs):
            raise RuntimeError("провайдер недоступен")

    pipeline, store, _agent, _ = _build()
    pipeline.agent_loop = _Boom()

    await pipeline.handle_message(_payload())
    await _settle()   # не должно бросить


async def test_webhook_without_chat_id_is_dropped_quietly():
    pipeline, store, agent, _ = _build()

    await pipeline.handle_message({"payload": {"value": {"id": "x", "content": {"text": "привет"}}}})
    await _settle()

    assert agent.calls == []


# --------------------------------------------------------------------------
# Модерация: одобрение только на ценовую уступку (moderation_mode)
# --------------------------------------------------------------------------
#
# Все тесты этого раздела — в живом режиме (dry_run=False): DRY_RUN сам по
# себе держит всё на одобрении независимо от moderation_mode (см. докстринг
# app/pipeline.py), поэтому только тут вообще видно, что отличает
# moderation_mode друг от друга.

def _quote(total=Decimal("7000")):
    return PriceQuote(status="ok", total=total, zone_id="bath_russian", day_type="weekend")


def _price_concession_event(base=Decimal("7000"), final=Decimal("6000")):
    """Как если бы decide() выдал ценовую уступку (ступень 5)."""
    decision = ConcessionDecision(
        allowed=True, tier=5, kind="price", new_quote=_quote(final),
        revenue_delta=final - base, revenue_delta_basis="base_rate",
        offer_template="Идёт навстречу — 6 000 ₽ вместо 7 000 ₽.",
    )
    return ConcessionEvent(decision=decision, base_price=base, zone_id="bath_russian", trigger="price_objection")


def _non_price_concession_event():
    """Как если бы decide() выдал неценовую ступень (перенос на будни)."""
    decision = ConcessionDecision(
        allowed=True, tier=1, kind="non_price",
        offer_template="Могу предложить будний день — там дешевле.",
    )
    return ConcessionEvent(decision=decision, base_price=Decimal("7000"), zone_id="bath_russian", trigger="price_objection")


def _requires_operator_approval_event():
    """Как если бы decide() не смог посчитать загрузку (R7)."""
    decision = ConcessionDecision(
        allowed=False, tier=5, kind="price", requires_operator_approval=True,
        denial_reason="Загрузка на эту дату неизвестна — нужно ваше решение по скидке",
    )
    return ConcessionEvent(decision=decision, base_price=Decimal("7000"), zone_id="bath_russian", trigger="price_objection")


class _FakeConcessionAgentLoop:
    """Отличает обычный ход от «чистого» повторного — конвейер вызывает
    run_turn дважды для запроса на скидку в живом режиме: один раз как
    обычно, второй раз с concessions_blocked=True за запасным ответом."""

    def __init__(self, result: TurnResult, fallback_text: str = "Отвечу без скидки — актуальны ли даты?"):
        self.result = result
        self.fallback_text = fallback_text
        self.calls: list[dict] = []

    async def run_turn(self, dialog_id, history, user_text, state=None, item_id=None,
                        item_lookup=None, concessions_blocked=False):
        self.calls.append({"user_text": user_text, "concessions_blocked": concessions_blocked})
        if concessions_blocked:
            return TurnResult(text=self.fallback_text)
        return self.result


def _live_settings(**overrides) -> Settings:
    base = dict(
        dry_run=False,
        avito_user_id=OUR_USER_ID,
        debounce_window_seconds=0,
        telegram_ops_chat_id="",
        telegram_allowed_users=[1],
        touch_reminder_delay_minutes=30,
        touch_max_count=3,
        concession_approval_timeout_minutes=15,
    )
    base.update(overrides)
    return Settings(**base)


def _build_live(*, agent, moderation_mode="concessions_only", avito=None, store=None,
                 kb=None, ops_bot=None):
    settings = _live_settings(moderation_mode=moderation_mode)
    store = store or InMemoryDialogStore()
    avito = avito if avito is not None else _FakeAvito()
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    pipeline = MessagePipeline(
        store=store, agent_loop=agent, ops_service=ops_service, settings=settings,
        kb=kb, avito_client=avito, ops_bot=ops_bot, debounce_window_seconds=0, now_fn=lambda: NOW,
    )
    return pipeline, store, avito, ops_service, settings


async def test_plain_price_quote_goes_out_without_approval():
    """Ответ по прайсу — calculate_price без единого request_concession за
    ход — уходит клиенту сразу, concession_events пуст."""
    agent = _FakeConcessionAgentLoop(TurnResult(text="Баня в субботу — 7 000 ₽ за 2 часа."))
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == [("chat-1", "Баня в субботу — 7 000 ₽ за 2 часа.")]
    assert await ops_service.store.get_pending("chat-1") is None
    assert len(agent.calls) == 1   # ни одного повторного «чистого» хода — approval не требовался


async def test_price_concession_requires_approval():
    """Ценовая уступка (allowed=True, kind=price) не уходит клиенту
    напрямую — держится на одобрении с богатой карточкой."""
    result = TurnResult(
        text="Идёт навстречу — 6 000 ₽ вместо 7 000 ₽.",
        concession_events=[_price_concession_event()],
    )
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == []
    pending = await ops_service.store.get_pending("chat-1")
    assert pending is not None
    assert pending.is_concession is True
    assert pending.text == "Идёт навстречу — 6 000 ₽ вместо 7 000 ₽."
    assert pending.due_at == NOW + timedelta(minutes=15)


async def test_non_price_concession_does_not_require_approval():
    """Неценовая ступень (перенос на будни и т.п.) не расходует деньги —
    уходит клиенту сразу, как обычный ответ."""
    result = TurnResult(
        text="Могу предложить будний день — там дешевле.",
        concession_events=[_non_price_concession_event()],
    )
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == [("chat-1", "Могу предложить будний день — там дешевле.")]
    assert await ops_service.store.get_pending("chat-1") is None


async def test_requires_operator_approval_requires_approval():
    """occupancy_ratio неизвестен (R7, каталог YCLIENTS пуст) — decide()
    вернул requires_operator_approval=True, а не обычный отказ. Это ЗНАЧИМОЕ
    решение и держится на одобрении, хотя allowed=False."""
    result = TurnResult(
        text="Уточню детали и вернусь с ответом.",
        concession_events=[_requires_operator_approval_event()],
    )
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == []
    pending = await ops_service.store.get_pending("chat-1")
    assert pending is not None and pending.is_concession is True


async def test_routine_denial_does_not_require_approval():
    """Обычный отказ (R1/R2/R6 — например, триггер не сработал) не должен
    держать сообщение: needs_operator_approval=False у таких решений."""
    decision = ConcessionDecision(allowed=False, denial_reason="R1: ни один триггер не сработал")
    event = ConcessionEvent(decision=decision, base_price=Decimal("7000"), zone_id="bath_russian", trigger=None)
    result = TurnResult(text="Баня — 7 000 ₽.", concession_events=[event])
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == [("chat-1", "Баня — 7 000 ₽.")]


async def test_moderation_mode_off_sends_even_price_concessions_without_approval():
    result = TurnResult(text="6 000 ₽ вместо 7 000 ₽.", concession_events=[_price_concession_event()])
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent, moderation_mode="off")

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == [("chat-1", "6 000 ₽ вместо 7 000 ₽.")]


async def test_moderation_mode_all_holds_even_a_plain_price_quote():
    agent = _FakeConcessionAgentLoop(TurnResult(text="Баня — 7 000 ₽."))
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent, moderation_mode="all")

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == []
    pending = await ops_service.store.get_pending("chat-1")
    assert pending is not None
    assert pending.is_concession is False   # обычный холд, без дедлайна и карточки скидки
    assert pending.due_at is None


async def test_price_concession_computes_the_fallback_turn_eagerly():
    """Запасной ответ считается заранее, вторым ходом с
    concessions_blocked=True — не в момент таймаута."""
    result = TurnResult(text="6 000 ₽ вместо 7 000 ₽.", concession_events=[_price_concession_event()])
    agent = _FakeConcessionAgentLoop(result, fallback_text="Уточните дату, посчитаю точнее.")
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    assert len(agent.calls) == 2
    assert agent.calls[0]["concessions_blocked"] is False
    assert agent.calls[1]["concessions_blocked"] is True
    pending = await ops_service.store.get_pending("chat-1")
    assert pending.fallback_text == "Уточните дату, посчитаю точнее."


async def test_dry_run_holds_concessions_without_computing_a_fallback():
    """DRY_RUN — мастер-рубильник: держит на одобрении и без дедлайна,
    вторая (LLM-затратная) попытка ради fallback_text ни к чему, раз
    авто-отправка по таймауту тут в принципе не сработает."""
    result = TurnResult(text="6 000 ₽ вместо 7 000 ₽.", concession_events=[_price_concession_event()])
    agent = _FakeConcessionAgentLoop(result)
    settings = _live_settings(dry_run=True)
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    pipeline = MessagePipeline(
        store=InMemoryDialogStore(), agent_loop=agent, ops_service=ops_service, settings=settings,
        debounce_window_seconds=0, now_fn=lambda: NOW,
    )

    await pipeline.handle_message(_payload())
    await _settle()

    assert len(agent.calls) == 1   # ни одного вызова с concessions_blocked=True
    pending = await ops_service.store.get_pending("chat-1")
    assert pending.is_concession is True
    assert pending.due_at is None
    assert pending.fallback_text is None


async def test_concessions_today_counts_only_allowed_grants():
    result = TurnResult(text="6 000 ₽.", concession_events=[_price_concession_event()])
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent)

    assert await store.count_concessions_today() == 0
    await pipeline.handle_message(_payload(message_id="m1"))
    await _settle()
    assert await store.count_concessions_today() == 1

    await pipeline.handle_message(_payload(chat_id="chat-2", message_id="m2"))
    await _settle()
    assert await store.count_concessions_today() == 2


# --------------------------------------------------------------------------
# R10 — дневной лимит уступок исчерпан: уведомление, не молчание
# --------------------------------------------------------------------------

def _daily_limit_event():
    decision = ConcessionDecision(
        allowed=False, tier=5, kind="price", daily_limit_exhausted=True,
        denial_reason="R10: исчерпан дневной лимит уступок (5)",
    )
    return ConcessionEvent(decision=decision, base_price=Decimal("7000"), zone_id="bath_russian", trigger="price_objection")


async def test_daily_limit_exhausted_notifies_the_operator(kb):
    """Оператор получает ОБА сообщения: обычную FYI-карточку с перепиской
    (сообщение всё равно ушло клиенту автономно — держать на одобрении
    нечего) и отдельное предупреждение про лимит."""
    bot = _FakeOpsBot()
    result = TurnResult(text="Уточню детали и вернусь с ответом.", concession_events=[_daily_limit_event()])
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, settings = _build_live(
        agent=agent, kb=kb, ops_bot=bot,
    )
    settings.telegram_ops_chat_id = "-100500"

    await pipeline.handle_message(_payload())
    await _settle()

    limit_notices = [m for m in bot.messages if "ЛИМИТ" in m["text"]]
    assert len(limit_notices) == 1
    assert "chat-1" in limit_notices[0]["text"]
    assert "(5)" in limit_notices[0]["text"]


async def test_daily_limit_exhausted_does_not_block_the_message():
    """Отказ уже принят движком — держать ответ на одобрении нечего, это
    не gate. Сообщение (без скидки) уходит клиенту как обычно."""
    result = TurnResult(text="Уточню детали и вернусь с ответом.", concession_events=[_daily_limit_event()])
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent)

    await pipeline.handle_message(_payload())
    await _settle()

    assert avito.sent == [("chat-1", "Уточню детали и вернусь с ответом.")]


async def test_daily_limit_exhausted_logs_even_without_a_bot(caplog):
    """Бот не настроен — уведомление лучшая попытка, а не гарантия, но лог
    обязан остаться единственным надёжным способом узнать."""
    result = TurnResult(text="Уточню детали.", concession_events=[_daily_limit_event()])
    agent = _FakeConcessionAgentLoop(result)
    pipeline, store, avito, ops_service, _ = _build_live(agent=agent, ops_bot=None)

    with caplog.at_level("WARNING", logger="parmangal"):
        await pipeline.handle_message(_payload())
        await _settle()

    assert "daily limit exhausted" in caplog.text


async def test_no_daily_limit_event_means_no_notification():
    """Обычная автономная отправка шлёт свою FYI-карточку (см. раздел
    «Автономная отправка + FYI» выше) — но не карточку про дневной лимит."""
    bot = _FakeOpsBot()
    agent = _FakeConcessionAgentLoop(TurnResult(text="Баня — 7 000 ₽."))
    pipeline, store, avito, ops_service, settings = _build_live(agent=agent, ops_bot=bot)
    settings.telegram_ops_chat_id = "-100500"

    await pipeline.handle_message(_payload())
    await _settle()

    assert not any("ЛИМИТ" in m["text"] for m in bot.messages)


# --------------------------------------------------------------------------
# Таймаут запроса на скидку
# --------------------------------------------------------------------------

async def test_timeout_sends_the_precomputed_fallback_and_clears_pending():
    """Просроченный запрос на скидку — клиенту уходит fallback_text, а не
    тишина, диалог продолжается."""
    settings = _live_settings()
    store = InMemoryDialogStore()
    avito = _FakeAvito()
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    await ops_service.store.set_pending(
        "chat-1",
        PendingReply(
            chat_id="chat-1", text="6 000 ₽ вместо 7 000 ₽.", created_at=NOW - timedelta(minutes=20),
            is_concession=True, fallback_text="Уточню детали и вернусь с ответом.",
            due_at=NOW - timedelta(minutes=5),   # уже просрочен
        ),
    )
    pipeline = MessagePipeline(
        store=store, agent_loop=_FakeAgentLoop(), ops_service=ops_service, settings=settings,
        avito_client=avito, debounce_window_seconds=0, now_fn=lambda: NOW,
    )

    handled = await pipeline.check_concession_timeouts()

    assert handled == ["chat-1"]
    assert avito.sent == [("chat-1", "Уточню детали и вернусь с ответом.")]
    assert await ops_service.store.get_pending("chat-1") is None


async def test_timeout_logs_the_reason():
    settings = _live_settings()
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    await ops_service.store.set_pending(
        "chat-1",
        PendingReply(
            chat_id="chat-1", text="скидка", created_at=NOW - timedelta(minutes=20),
            is_concession=True, fallback_text="без скидки", due_at=NOW - timedelta(minutes=1),
        ),
    )
    pipeline = MessagePipeline(
        store=InMemoryDialogStore(), agent_loop=_FakeAgentLoop(), ops_service=ops_service,
        settings=settings, avito_client=_FakeAvito(), debounce_window_seconds=0, now_fn=lambda: NOW,
    )

    await pipeline.check_concession_timeouts()

    actions = [a for a in ops_service.store.actions if a["action"] == "concession_timeout"]
    assert actions and actions[0]["chat_id"] == "chat-1"


async def test_timeout_ignores_requests_not_yet_due():
    settings = _live_settings()
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    await ops_service.store.set_pending(
        "chat-1",
        PendingReply(
            chat_id="chat-1", text="скидка", created_at=NOW, is_concession=True,
            fallback_text="без скидки", due_at=NOW + timedelta(minutes=10),   # ещё не подошёл срок
        ),
    )
    avito = _FakeAvito()
    pipeline = MessagePipeline(
        store=InMemoryDialogStore(), agent_loop=_FakeAgentLoop(), ops_service=ops_service,
        settings=settings, avito_client=avito, debounce_window_seconds=0, now_fn=lambda: NOW,
    )

    handled = await pipeline.check_concession_timeouts()

    assert handled == []
    assert avito.sent == []
    assert await ops_service.store.get_pending("chat-1") is not None


async def test_timeout_never_fires_under_dry_run_even_with_a_stale_due_at():
    """Мастер-рубильник проверяется и в самом воркере — на случай, если
    DRY_RUN включили обратно, пока запрос ждал оператора."""
    settings = _live_settings(dry_run=True)
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    await ops_service.store.set_pending(
        "chat-1",
        PendingReply(
            chat_id="chat-1", text="скидка", created_at=NOW - timedelta(hours=1), is_concession=True,
            fallback_text="без скидки", due_at=NOW - timedelta(minutes=1),
        ),
    )
    avito = _FakeAvito()
    pipeline = MessagePipeline(
        store=InMemoryDialogStore(), agent_loop=_FakeAgentLoop(), ops_service=ops_service,
        settings=settings, avito_client=avito, debounce_window_seconds=0, now_fn=lambda: NOW,
    )

    handled = await pipeline.check_concession_timeouts()

    assert handled == []
    assert avito.sent == []
