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
from app.ops.bot import OpsService
from app.ops.state import InMemoryOpsStore
from app.pipeline import MessagePipeline
from app.pricing.concessions import DialogConcessionState

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

OUR_USER_ID = "seller-1"


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
