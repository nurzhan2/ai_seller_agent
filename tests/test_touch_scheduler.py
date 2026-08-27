"""Тесты воркера отложенных касаний (app/ops/touch_scheduler.py) — один
проход через InMemoryTouchStore, без реальной БД (тот же приём, что и
InMemoryOpsStore для операторского контура)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agent.touch_tracking import TEMPLATE_DIRECT, TEMPLATE_SOFT, TouchState
from app.kb.loader import WorkingWindow
from app.ops.touch_scheduler import InMemoryTouchStore, TouchDialog, run_scheduler_pass

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
WINDOW = WorkingWindow(**{"from": "09:00", "to": "23:00"})
TEMPLATES = {TEMPLATE_SOFT: "Вы где-то затерялись?", TEMPLATE_DIRECT: "Будете бронировать или нет?"}


def _sender():
    sent: list[tuple[str, str]] = []

    async def send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    return send, sent


async def test_due_dialog_gets_the_soft_template_and_advances_to_touch_two():
    store = InMemoryTouchStore(dialogs={
        "chat-1": TouchState(touch_count=1, last_touch_at=NOW - timedelta(minutes=30), next_touch_due_at=NOW),
    })
    send, sent = _sender()

    touched = await run_scheduler_pass(
        store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3,
    )

    assert touched == ["chat-1"]
    assert sent == [("chat-1", TEMPLATES[TEMPLATE_SOFT])]
    assert store.dialogs["chat-1"].touch_count == 2
    assert store.dialogs["chat-1"].next_touch_due_at == NOW + timedelta(minutes=30)


async def test_second_touch_uses_direct_template_and_stops():
    store = InMemoryTouchStore(dialogs={
        "chat-1": TouchState(touch_count=2, last_touch_at=NOW - timedelta(minutes=30), next_touch_due_at=NOW),
    })
    send, sent = _sender()

    await run_scheduler_pass(store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3)

    assert sent == [("chat-1", TEMPLATES[TEMPLATE_DIRECT])]
    assert store.dialogs["chat-1"].touch_count == 3
    assert store.dialogs["chat-1"].next_touch_due_at is None


async def test_not_yet_due_dialog_is_left_untouched():
    store = InMemoryTouchStore(dialogs={
        "chat-1": TouchState(touch_count=1, next_touch_due_at=NOW + timedelta(minutes=5)),
    })
    send, sent = _sender()

    touched = await run_scheduler_pass(store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3)

    assert touched == []
    assert sent == []
    assert store.dialogs["chat-1"].touch_count == 1


async def test_dialog_at_max_count_is_never_picked_up():
    store = InMemoryTouchStore(dialogs={
        "chat-1": TouchState(touch_count=3, next_touch_due_at=NOW - timedelta(minutes=1)),
    })
    send, sent = _sender()

    touched = await run_scheduler_pass(store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3)

    assert touched == []
    assert sent == []


# --------------------------------------------------------------------------
# «Отложенное сообщение не уходит в 3 ночи»
# --------------------------------------------------------------------------

async def test_whole_pass_is_skipped_outside_working_hours():
    night = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
    store = InMemoryTouchStore(dialogs={
        "chat-1": TouchState(touch_count=1, next_touch_due_at=night - timedelta(minutes=1)),
    })
    send, sent = _sender()

    touched = await run_scheduler_pass(store, TEMPLATES, WINDOW, send, night, delay_minutes=30, max_count=3)

    assert touched == []
    assert sent == []
    # состояние не тронуто — тот же диалог due и останется due до утра
    assert store.dialogs["chat-1"].touch_count == 1
    assert store.dialogs["chat-1"].next_touch_due_at == night - timedelta(minutes=1)


async def test_overdue_dialog_fires_as_soon_as_window_opens():
    """Просроченный ночью диалог не потерян — следующий проход воркера
    (уже утром) подхватывает его как есть."""
    night = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
    morning = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    store = InMemoryTouchStore(dialogs={
        "chat-1": TouchState(touch_count=1, next_touch_due_at=night),
    })
    send, sent = _sender()

    await run_scheduler_pass(store, TEMPLATES, WINDOW, send, night, delay_minutes=30, max_count=3)
    assert sent == []

    await run_scheduler_pass(store, TEMPLATES, WINDOW, send, morning, delay_minutes=30, max_count=3)
    assert sent == [("chat-1", TEMPLATES[TEMPLATE_SOFT])]


# --------------------------------------------------------------------------
# Устойчивость к сбою отправки
# --------------------------------------------------------------------------

async def test_send_failure_does_not_advance_state_or_stop_the_pass():
    async def failing_send(chat_id, text):
        raise RuntimeError("Avito недоступен")

    store = InMemoryTouchStore(dialogs={
        "chat-1": TouchState(touch_count=1, next_touch_due_at=NOW),
    })

    touched = await run_scheduler_pass(store, TEMPLATES, WINDOW, failing_send, NOW, delay_minutes=30, max_count=3)

    assert touched == []
    # состояние не продвинулось — при следующем проходе попробуем снова
    assert store.dialogs["chat-1"].touch_count == 1
    assert store.dialogs["chat-1"].next_touch_due_at == NOW


async def test_one_dialog_failing_does_not_block_the_rest():
    async def flaky_send(chat_id, text):
        if chat_id == "bad":
            raise RuntimeError("boom")

    store = InMemoryTouchStore(dialogs={
        "bad": TouchState(touch_count=1, next_touch_due_at=NOW),
        "good": TouchState(touch_count=1, next_touch_due_at=NOW),
    })

    touched = await run_scheduler_pass(store, TEMPLATES, WINDOW, flaky_send, NOW, delay_minutes=30, max_count=3)

    assert touched == ["good"]
    assert store.dialogs["bad"].touch_count == 1
    assert store.dialogs["good"].touch_count == 2


# --------------------------------------------------------------------------
# «Рестарт процесса не теряет запланированные касания»
# --------------------------------------------------------------------------

async def test_state_reloaded_after_restart_is_picked_up_by_a_fresh_store():
    """Сохранённое состояние — не то же самое, что живой процесс: новый
    InMemoryTouchStore, «загруженный» из тех же данных (как если бы это был
    новый процесс, читающий БД), находит диалог due как ни в чём не бывало."""
    persisted_snapshot = {
        "chat-1": TouchState(touch_count=1, last_touch_at=NOW - timedelta(hours=1), next_touch_due_at=NOW),
    }

    # «Рестарт»: новый объект стора, те же данные — как будто только что
    # прочитаны из DialogState.
    fresh_store = InMemoryTouchStore(dialogs=dict(persisted_snapshot))
    send, sent = _sender()

    touched = await run_scheduler_pass(fresh_store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3)

    assert touched == ["chat-1"]
    assert sent == [("chat-1", TEMPLATES[TEMPLATE_SOFT])]


# --------------------------------------------------------------------------
# Белый список объявлений (инцидент: касание ушло в чат u2u-…)
# --------------------------------------------------------------------------

def _blocker(*blocked: str):
    """can_send, запрещающий перечисленные чаты."""
    async def can_send(chat_id: str) -> bool:
        return chat_id not in blocked

    return can_send


async def test_touch_is_not_sent_to_a_blocked_chat():
    """Главный сценарий инцидента: в 09:00 третье касание ушло в чат
    u2u-2QuAfvI4HoxsE7IKKDN3SA, которому агент писать не должен."""
    chat = "u2u-2QuAfvI4HoxsE7IKKDN3SA"
    store = InMemoryTouchStore(dialogs={
        chat: TouchState(touch_count=2, last_touch_at=NOW - timedelta(minutes=30), next_touch_due_at=NOW),
    })
    send, sent = _sender()

    touched = await run_scheduler_pass(
        store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3,
        can_send=_blocker(chat),
    )

    assert sent == []
    assert touched == []


async def test_blocked_chat_gets_its_timer_disarmed_not_just_skipped():
    """Пропустить мало: диалог остался бы due и всплывал в каждом проходе
    воркера до конца времён. Гасим срок — и это же чистит чаты, попавшие в
    таблицу касаний до появления фильтра."""
    chat = "u2u-old"
    store = InMemoryTouchStore(dialogs={
        chat: TouchState(touch_count=2, last_touch_at=NOW - timedelta(minutes=30), next_touch_due_at=NOW),
    })
    send, sent = _sender()

    await run_scheduler_pass(
        store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3,
        can_send=_blocker(chat),
    )

    assert store.dialogs[chat].next_touch_due_at is None
    # Счётчик — история, а не следствие фильтра: его не переписываем.
    assert store.dialogs[chat].touch_count == 2

    # Второй проход по тому же стору диалог уже не находит.
    touched_again = await run_scheduler_pass(
        store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3,
        can_send=_blocker(chat),
    )
    assert touched_again == []


async def test_allowed_chats_are_untouched_by_the_filter():
    """Фильтр не должен задевать соседей: в одном проходе заблокированный
    и разрешённый чат обрабатываются независимо."""
    store = InMemoryTouchStore(dialogs={
        "blocked": TouchState(touch_count=1, last_touch_at=NOW - timedelta(minutes=30), next_touch_due_at=NOW),
        "allowed": TouchState(touch_count=1, last_touch_at=NOW - timedelta(minutes=30), next_touch_due_at=NOW),
    })
    send, sent = _sender()

    touched = await run_scheduler_pass(
        store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3,
        can_send=_blocker("blocked"),
    )

    assert touched == ["allowed"]
    assert sent == [("allowed", TEMPLATES[TEMPLATE_SOFT])]
    assert store.dialogs["allowed"].touch_count == 2
    assert store.dialogs["blocked"].touch_count == 1


async def test_without_can_send_everything_works_as_before():
    """Обратная совместимость: параметр не передан — поведение прежнее."""
    store = InMemoryTouchStore(dialogs={
        "chat-1": TouchState(touch_count=1, last_touch_at=NOW - timedelta(minutes=30), next_touch_due_at=NOW),
    })
    send, sent = _sender()

    touched = await run_scheduler_pass(store, TEMPLATES, WINDOW, send, NOW, delay_minutes=30, max_count=3)

    assert touched == ["chat-1"]
    assert sent == [("chat-1", TEMPLATES[TEMPLATE_SOFT])]
