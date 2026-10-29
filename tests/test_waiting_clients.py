"""Аудит 30.10: клиент в чате «у менеджера» не остаётся без ответа.

За 10 дней 22 клиента так и не дождались ответа: менеджер написал раз-два
в Авито, чат ушёл в перехват (TAKEOVER_MODE=permanent, агент молчит до 72 ч),
а дальше никто не ответил («Вы можете ответить?»).
"""

from __future__ import annotations

from datetime import timedelta

from app.agent.loop import TurnResult
from app.db.models import Author
from app.dialog_store import InMemoryDialogStore
from app.ops.bot import OpsService
from app.ops.state import InMemoryOpsStore
from app.pipeline import MessagePipeline, _is_just_acknowledgement
from tests.test_pipeline import (
    NOW,
    OUR_USER_ID,
    _FakeAgentLoop,
    _FakeAvito,
    _FakeOpsBot,
    _live_settings,
    _payload,
    _settle,
    _take_over,
    _build,
)


class _AvitoWithHistory(_FakeAvito):
    def __init__(self, history=None):
        super().__init__()
        self.history = history or []

    async def get_messages(self, chat_id, limit=50, offset=0):
        return {"messages": list(self.history)}


async def _stranded(client_text="Как закрепить бронь?", minutes_ago=40, history=None):
    settings = _live_settings(telegram_ops_chat_id="ops")
    store = InMemoryDialogStore()
    agent = _FakeAgentLoop(TurnResult(text="Для брони напишите имя и телефон."))
    avito = _AvitoWithHistory(history)
    ops_bot = _FakeOpsBot()
    ops = OpsService(store=InMemoryOpsStore(), settings=settings)
    pipeline = MessagePipeline(
        store=store, agent_loop=agent, ops_service=ops, settings=settings,
        avito_client=avito, ops_bot=ops_bot, debounce_window_seconds=0, now_fn=lambda: NOW,
    )
    await store.get_or_create_chat("chat-1")
    await _take_over(ops, store, at=NOW - timedelta(hours=2))
    await store.save_incoming("chat-1", client_text, "in-1")
    store.messages["chat-1"][-1]["created_at"] = NOW - timedelta(minutes=minutes_ago)
    return pipeline, store, agent, avito, ops, ops_bot


async def test_a_client_left_waiting_by_the_manager_gets_an_answer():
    manager = {"id": "m-1", "author_id": OUR_USER_ID, "created": int((NOW - timedelta(hours=2)).timestamp()),
               "content": {"text": "Баня Гараж свободна с 17:00"}, "type": "text"}
    pipeline, store, agent, avito, ops, ops_bot = await _stranded(history=[manager])

    rescued = await pipeline.rescue_waiting_clients()

    assert rescued == ["chat-1"]
    assert avito.sent == [("chat-1", "Для брони напишите имя и телефон.")]
    assert (await ops.store.get_flags("chat-1")).is_human_takeover is False
    assert any("ждал ответа" in m["text"] for m in ops_bot.messages)
    # бот видит, что писал менеджер
    history = agent.calls[0]["history"]
    assert any(h["role"] == "assistant" and "Гараж свободна" in h["content"] for h in history)


async def test_the_manager_still_has_time_to_answer():
    pipeline, store, agent, avito, ops, _ = await _stranded(minutes_ago=10)
    assert await pipeline.rescue_waiting_clients() == []
    assert avito.sent == []
    assert (await ops.store.get_flags("chat-1")).is_human_takeover is True


async def test_a_thank_you_is_not_answered_half_an_hour_later():
    pipeline, store, agent, avito, ops, _ = await _stranded(client_text="Спасибо, подумаю")
    assert await pipeline.rescue_waiting_clients() == []
    assert avito.sent == []


async def test_an_old_abandoned_chat_is_not_revived():
    pipeline, store, agent, avito, ops, _ = await _stranded(minutes_ago=60 * 30)
    assert await pipeline.rescue_waiting_clients() == []


async def test_a_manager_reply_found_in_avito_cancels_the_rescue():
    """В базе ответа нет (до 30.10 тексты менеджера не сохранялись), а в
    Авито менеджер уже ответил - не вмешиваемся."""
    late = {"id": "m-2", "author_id": OUR_USER_ID, "created": int((NOW - timedelta(minutes=5)).timestamp()),
            "content": {"text": "Бронь закрепим по предоплате, сейчас пришлю ссылку"}, "type": "text"}
    pipeline, store, agent, avito, ops, _ = await _stranded(history=[late])

    assert await pipeline.rescue_waiting_clients() == []
    assert avito.sent == []


async def test_rescue_is_off_when_the_setting_is_zero():
    pipeline, store, agent, avito, ops, _ = await _stranded()
    pipeline.settings.takeover_client_wait_minutes = 0
    assert await pipeline.rescue_waiting_clients() == []


async def test_the_managers_text_goes_into_the_dialog_history():
    pipeline, store, agent, ops = _build()
    await store.get_or_create_chat("chat-1")

    await pipeline.handle_message(_payload(author_id=OUR_USER_ID, text="Баня свободна с 17:00", message_id="m-9"))
    await _settle()

    rows = store.messages["chat-1"]
    assert rows[-1]["author"] == Author.operator
    assert rows[-1]["text"] == "Баня свободна с 17:00"
    history = await store.load_history("chat-1")
    assert history[-1] == {"role": "assistant", "content": "Баня свободна с 17:00"}
    # эхо НАШЕГО сообщения в будущем не спутается с менеджерским
    assert await store.was_sent_by_us("chat-1", "Баня свободна с 17:00") is False


def test_acknowledgements():
    for text in ("Ок", "Спасибо!", "спасибо большое", "Хорошо 👍", "Я напишу вам позже", "Пока стоп"):
        assert _is_just_acknowledgement(text), text
    for text in ("Да", "С 14 до 17 бронь", "Как закрепить бронь ?", "Сегодня"):
        assert not _is_just_acknowledgement(text), text
