"""Тесты чистой логики отложенных касаний (app/agent/touch_tracking.py).

Регламент: первое касание — цена (уже названа обычным ходом агента);
30 минут молчания — второе (мягкое); снова молчит — третье (прямой вопрос);
дальше молчим. Ценовая уступка требует минимум 3 касания либо явное
возражение по цене — это отдельный тест в test_concessions.py (R13), не тут.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.clock import MOSCOW_TZ
from app.agent.touch_tracking import (
    DIRECT_TOUCH_NUMBER,
    SOFT_TOUCH_NUMBER,
    TEMPLATE_DIRECT,
    TEMPLATE_SOFT,
    TouchState,
    advance_touch,
    force_to_max_on_price_objection,
    is_due,
    is_within_working_hours,
    record_first_touch,
    reset_timer_on_reply,
)
from app.kb.loader import WorkingWindow

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
WINDOW = WorkingWindow(**{"from": "09:00", "to": "23:00"})


# --------------------------------------------------------------------------
# «Таймер срабатывает через 30 минут»
# --------------------------------------------------------------------------

def test_first_touch_arms_timer_thirty_minutes_out():
    state = record_first_touch(TouchState(), NOW, delay_minutes=30)
    assert state.touch_count == 1
    assert state.next_touch_due_at == NOW + timedelta(minutes=30)


def test_not_due_before_thirty_minutes_pass():
    state = record_first_touch(TouchState(), NOW, delay_minutes=30)
    almost = NOW + timedelta(minutes=29, seconds=59)
    assert is_due(state, almost, max_count=3) is False


def test_due_exactly_at_thirty_minutes():
    state = record_first_touch(TouchState(), NOW, delay_minutes=30)
    exactly = NOW + timedelta(minutes=30)
    assert is_due(state, exactly, max_count=3) is True


def test_due_after_thirty_minutes():
    state = record_first_touch(TouchState(), NOW, delay_minutes=30)
    later = NOW + timedelta(minutes=45)
    assert is_due(state, later, max_count=3) is True


# --------------------------------------------------------------------------
# «Ответ клиента таймер сбрасывает»
# --------------------------------------------------------------------------

def test_client_reply_clears_pending_timer():
    state = record_first_touch(TouchState(), NOW, delay_minutes=30)
    reset = reset_timer_on_reply(state)
    assert reset.next_touch_due_at is None
    # уже отправленное касание остаётся в истории — не обнуляется ответом клиента
    assert reset.touch_count == 1


def test_reply_with_no_pending_timer_is_a_noop():
    state = TouchState(touch_count=0, next_touch_due_at=None)
    assert reset_timer_on_reply(state) == state


def test_after_reset_dialog_is_no_longer_due():
    state = record_first_touch(TouchState(), NOW, delay_minutes=30)
    reset = reset_timer_on_reply(state)
    much_later = NOW + timedelta(hours=5)
    assert is_due(reset, much_later, max_count=3) is False


# --------------------------------------------------------------------------
# Прогрессия касаний: 2-е (мягкое) -> 3-е (прямое) -> дальше молчим
# --------------------------------------------------------------------------

def test_second_touch_uses_soft_template_and_reschedules():
    state = record_first_touch(TouchState(), NOW, delay_minutes=30)
    due_at = state.next_touch_due_at
    outcome = advance_touch(state, due_at, delay_minutes=30, max_count=3)
    assert outcome.touch_number == SOFT_TOUCH_NUMBER
    assert outcome.template_key == TEMPLATE_SOFT
    assert outcome.state.touch_count == 2
    assert outcome.state.next_touch_due_at == due_at + timedelta(minutes=30)


def test_third_touch_uses_direct_template_and_stops_scheduling():
    state = TouchState(touch_count=2, last_touch_at=NOW, next_touch_due_at=NOW)
    outcome = advance_touch(state, NOW, delay_minutes=30, max_count=3)
    assert outcome.touch_number == DIRECT_TOUCH_NUMBER
    assert outcome.template_key == TEMPLATE_DIRECT
    assert outcome.state.touch_count == 3
    # не более трёх касаний на диалог — дальше молчим, таймер не заводится снова
    assert outcome.state.next_touch_due_at is None


def test_at_max_count_dialog_is_never_due_again():
    state = TouchState(touch_count=3, next_touch_due_at=None)
    assert is_due(state, NOW + timedelta(days=1), max_count=3) is False


# --------------------------------------------------------------------------
# «Отложенное сообщение не уходит в 3 ночи»
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (3, 0, False),      # 3 ночи — точно нет
        (8, 59, False),     # минута до открытия
        (9, 0, True),       # ровно открытие
        (12, 0, True),      # день
        (22, 59, True),     # минута до закрытия
        (23, 0, False),     # ровно закрытие — уже нет
        (23, 30, False),
        (0, 0, False),
    ],
)
def test_working_hours_window(hour, minute, expected):
    """Часы — МОСКОВСКИЕ: окно в базе знаний это часы работы комплекса."""
    dt = datetime(2026, 8, 20, hour, minute, tzinfo=MOSCOW_TZ)
    assert is_within_working_hours(dt, WINDOW) is expected


@pytest.mark.parametrize(
    "utc_hour,expected,why",
    [
        (22, False, "01:00 МСК — ночь, касание уходить не должно"),
        (6, True, "09:00 МСК — ровно открытие"),
        (5, False, "08:00 МСК — ещё закрыто"),
        (19, True, "22:00 МСК — ещё работаем"),
        (20, False, "23:00 МСК — закрытие"),
    ],
)
def test_a_utc_moment_is_converted_to_moscow_before_comparing(utc_hour, expected, why):
    """Воркер передаёт сюда `datetime.now(timezone.utc)`, а окно 9:00–23:00
    — московское. Сравнение UTC-времени с московскими часами превращало окно
    в фактические 12:00–02:00 МСК: касания уходили в час ночи и не уходили
    с 9 до 12 утра."""
    dt = datetime(2026, 8, 20, utc_hour, 0, tzinfo=timezone.utc)

    assert is_within_working_hours(dt, WINDOW) is expected, why


def test_a_naive_moment_is_taken_as_moscow():
    """Naive время приходит только из тестов и считается уже московским —
    иначе те же 9:00 значили бы разное в двух соседних вызовах."""
    assert is_within_working_hours(datetime(2026, 8, 20, 10, 0), WINDOW) is True
    assert is_within_working_hours(datetime(2026, 8, 20, 2, 0), WINDOW) is False


# --------------------------------------------------------------------------
# Жалоба на цену эскалирует сразу до максимума
# --------------------------------------------------------------------------

def test_price_objection_forces_touch_count_to_max_and_clears_timer():
    state = record_first_touch(TouchState(), NOW, delay_minutes=30)
    forced = force_to_max_on_price_objection(state, max_count=3)
    assert forced.touch_count == 3
    assert forced.next_touch_due_at is None


def test_price_objection_is_a_noop_once_already_at_max():
    state = TouchState(touch_count=3, next_touch_due_at=None)
    assert force_to_max_on_price_objection(state, max_count=3) == state
