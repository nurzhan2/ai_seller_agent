"""AGENT_MIN_INBOUND_TS — инвариантный тест на свойство, а не на сценарий.

Инцидент 2026-08-28: холодный старт поллера отвечал в чаты месячной
давности. Причина была устроена так, что сценарные тесты (один-два прохода)
её не поймали — она проявлялась только НА ВТОРОМ проходе, когда курсор с
пустым seen_ids читался как «новое сообщение» (разбор — докстринг
app/avito/cursors.py). Поэтому здесь не сценарий, а свойство: сколько
проходов ни делай, сколько угодно раз подряд, по чату старше метки не
должно уйти ни одного исходящего. Мерило «исходящего» — вызов AgentLoop:
если агент не позван, ответа не существует ни в какой форме (ни в DRY_RUN-
очереди, ни у клиента), это раньше любой развилки про доставку.

Второй тест проверяет то же свойство на пути вебхука напрямую — правило
живёт в конвейере (`app/pipeline.py:_is_too_old_to_answer`), а не в поллере,
именно чтобы вебхук не был лазейкой мимо него.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.agent.loop import TurnResult
from app.avito.cursors import InMemoryCursorStore
from app.avito.poller import AvitoPoller
from app.config import Settings
from app.dialog_store import InMemoryDialogStore
from app.ops.bot import OpsService
from app.ops.state import InMemoryOpsStore
from app.pipeline import MessagePipeline

OUR_USER_ID = "seller-1"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
NOW_TS = int(NOW.timestamp())
# Порог: агент отвечает только на то, что пришло не раньше этого момента.
CUTOFF_TS = NOW_TS - 3600
PASSES = 10


class _FakeAgentLoop:
    """Считает, по каким чатам его вообще позвали — это и есть «было бы
    исходящее», независимо от DRY_RUN/модерации/канала доставки."""

    def __init__(self):
        self.calls: list[str] = []

    async def run_turn(self, dialog_id, history, user_text, state=None, item_id=None, item_lookup=None):
        self.calls.append(dialog_id)
        return TurnResult(text="Добрый день!")


class _FakeAvitoClient:
    """Аккаунт, который НИЧЕГО не меняет между проходами — ни новых
    сообщений, ни новых чатов. Это и есть «десять проходов подряд без
    единого изменения на стороне Авито» из задания."""

    def __init__(self, chats: list[dict], messages: dict[str, list[dict]]):
        self._chats = chats
        self._messages = messages

    async def list_chats(self, *, limit=50, offset=0):
        return {"chats": self._chats[offset:offset + limit]}

    async def get_messages(self, chat_id, *, limit=50, offset=0):
        batch = self._messages.get(chat_id, [])
        return {"messages": batch[offset:offset + limit]}


async def _resolved(value):
    return value


def _settings(**overrides) -> Settings:
    base = dict(
        avito_user_id=OUR_USER_ID,
        dry_run=True,
        debounce_window_seconds=0,
        telegram_ops_chat_id="",
        telegram_allowed_users=[1],
        touch_reminder_delay_minutes=30,
        touch_max_count=3,
        agent_min_inbound_ts=CUTOFF_TS,
        poller_chats_page_size=50,
        poller_max_offset=1000,
        poller_messages_page_size=20,
        poller_max_message_pages=10,
        poller_interval_seconds=60,
    )
    base.update(overrides)
    return Settings(**base)


def _chat(chat_id: str, item_id: str, created: int, message_id: str, author="buyer") -> dict:
    """`last_message.id` совпадает с единственным реальным сообщением этого
    чата — реальный Авито несёт один и тот же id в обоих ответах (список
    чатов и список сообщений), рассинхронизация была бы тестовым
    артефактом, а не свойством прода."""
    return {
        "id": chat_id,
        "last_message": {"id": message_id, "created": created, "author_id": author},
        "context": {"type": "item", "value": {"id": item_id, "title": "Баня"}},
    }


def _message(message_id: str, created: int, author="buyer") -> dict:
    return {
        "id": message_id, "created": created, "author_id": author,
        "type": "text", "content": {"text": "привет"},
    }


def _build_pipeline(settings: Settings):
    store = InMemoryDialogStore()
    agent = _FakeAgentLoop()
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    pipeline = MessagePipeline(
        store=store, agent_loop=agent, ops_service=ops_service, settings=settings,
        debounce_window_seconds=0, now_fn=lambda: NOW,
    )
    return pipeline, agent


async def _settle() -> None:
    """Debounce отдаёт склейку из отдельной задачи — даём ей провернуться
    (см. tests/test_pipeline.py)."""
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# Инвариант: поллер, десять проходов, ничего не меняется на стороне Авито
# --------------------------------------------------------------------------

async def test_ten_poller_passes_never_answer_a_chat_older_than_the_cutoff():
    old_created_1 = CUTOFF_TS - 100 * 3600      # заведомо старше порога
    old_created_2 = CUTOFF_TS - 1                # ровно на границе, тоже старше
    fresh_created = CUTOFF_TS + 100              # заведомо новее порога

    chats = [
        _chat("old-1", "111", old_created_1, "m-old-1"),
        _chat("old-2", "111", old_created_2, "m-old-2"),
        _chat("fresh-1", "111", fresh_created, "m-fresh-1"),
    ]
    messages = {
        "old-1": [_message("m-old-1", old_created_1)],
        "old-2": [_message("m-old-2", old_created_2)],
        "fresh-1": [_message("m-fresh-1", fresh_created)],
    }

    settings = _settings()
    pipeline, agent = _build_pipeline(settings)
    client = _FakeAvitoClient(chats, messages)
    poller = AvitoPoller(
        client=client, pipeline=pipeline, cursors=InMemoryCursorStore(),
        settings=settings, items_provider=lambda: _resolved({"111"}), now_fn=lambda: NOW,
    )

    for i in range(PASSES):
        await poller.run_pass()
        await _settle()
        # Свойство держится НА КАЖДОМ проходе, не только в конце — если оно
        # где-то нарушится посередине, ассерт укажет, на каком именно.
        assert "old-1" not in agent.calls, f"старый чат получил ответ на проходе {i + 1}"
        assert "old-2" not in agent.calls, f"старый чат получил ответ на проходе {i + 1}"

    # Не вырожденный тест: живой чат отвечен (иначе «ноль ответов» ничего не
    # доказывает — агент мог быть просто сломан).
    assert agent.calls == ["fresh-1"]


# --------------------------------------------------------------------------
# То же правило — на пути вебхука, не только поллера
# --------------------------------------------------------------------------

async def test_the_same_rule_fires_on_the_webhook_path_too():
    """Правило живёт в конвейере, а не в поллере, именно ради этого пути:
    вебхук идёт через `handle_message` -> `_accept` напрямую, минуя поллер
    и его курсор целиком."""
    settings = _settings()
    pipeline, agent = _build_pipeline(settings)

    old_event = {"payload": {"value": {
        "id": "m-old", "chat_id": "chat-old", "author_id": "buyer",
        "item_id": "111", "content": {"text": "привет"},
        "created": CUTOFF_TS - 100 * 3600,
    }}}
    fresh_event = {"payload": {"value": {
        "id": "m-fresh", "chat_id": "chat-fresh", "author_id": "buyer",
        "item_id": "111", "content": {"text": "привет"},
        "created": CUTOFF_TS + 100,
    }}}

    assert await pipeline.handle_message(old_event, source="webhook") is True
    await _settle()
    assert agent.calls == []

    assert await pipeline.handle_message(fresh_event, source="webhook") is True
    await _settle()
    assert agent.calls == ["chat-fresh"]


# --------------------------------------------------------------------------
# Граница: created РОВНО НА пороге обязан получить ответ (>=, не >)
# --------------------------------------------------------------------------

async def test_message_created_exactly_at_the_cutoff_still_gets_an_answer():
    """Порог задаёт момент, «раньше которого» не отвечаем — включительно с
    самим порогом. `>` вместо `>=` здесь тихо сдвинул бы границу на секунду
    и не поймался бы ни одним из двух тестов выше (там разрывы намеренно
    большие, ради читаемости, а не ради этой границы)."""
    settings = _settings()
    pipeline, agent = _build_pipeline(settings)

    boundary_event = {"payload": {"value": {
        "id": "m-boundary", "chat_id": "chat-boundary", "author_id": "buyer",
        "item_id": "111", "content": {"text": "привет"},
        "created": CUTOFF_TS,
    }}}

    assert await pipeline.handle_message(boundary_event, source="webhook") is True
    await _settle()
    assert agent.calls == ["chat-boundary"]
