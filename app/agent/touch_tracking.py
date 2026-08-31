"""Отложенные касания — переходы состояния таймера напоминаний.

Регламент (Максим): первое касание — называем цену (обычный ход агента,
см. `ToolExecutor._tool_calculate_price`). Если клиент молчит
`touch_reminder_delay_minutes` (по умолчанию 30) — второе касание, мягкое
напоминание. Если снова молчит (или явно возразил по цене — тогда сразу,
без ожидания) — третье, прямой вопрос. Дальше молчим: не более
`touch_max_count` касаний на диалог. Ценовая уступка разрешена не раньше
третьего касания либо сразу при возражении по цене — это отдельное правило
R13 в app.pricing.concessions, не здесь.

Всё здесь — чистые функции без сети, БД и event loop: тестируются без
инфраструктуры. БД-обвязка (кто именно из диалогов созрел, реальная отправка)
— app.ops.touch_scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time as TimeType, timedelta
from typing import Optional

from app.clock import MOSCOW_TZ
from app.kb.loader import WorkingWindow

SOFT_TOUCH_NUMBER = 2
DIRECT_TOUCH_NUMBER = 3

TEMPLATE_SOFT = "soft"
TEMPLATE_DIRECT = "direct"


@dataclass(frozen=True)
class TouchState:
    """Снимок touch_* колонок `DialogState` — не ORM-объект, чтобы логику
    перехода можно было гонять в тестах без базы."""

    touch_count: int = 0
    last_touch_at: Optional[datetime] = None
    next_touch_due_at: Optional[datetime] = None


def record_first_touch(state: TouchState, now: datetime, delay_minutes: int) -> TouchState:
    """Заводит таймер на второе касание. Цена уже названа обычным ходом
    агента (`touch_count` там же выставлен в 1) — здесь только таймер."""
    return replace(
        state,
        touch_count=max(state.touch_count, 1),
        last_touch_at=now,
        next_touch_due_at=now + timedelta(minutes=delay_minutes),
    )


def reset_timer_on_reply(state: TouchState) -> TouchState:
    """Клиент ответил — активное напоминание больше не актуально.

    `touch_count` НЕ обнуляется: уже отправленные касания остаются частью
    истории диалога на регламент считает их на весь диалог, а не на «текущую
    волну молчания». Обнулять его при каждом ответе клиента позволило бы
    бесконечно переоткрывать эскалацию заново.
    """
    if state.next_touch_due_at is None:
        return state
    return replace(state, next_touch_due_at=None)


def is_due(state: TouchState, now: datetime, max_count: int) -> bool:
    """True, если пора слать следующее касание — таймер тикает, лимит не
    исчерпан, время настало. Ничего не знает про рабочее окно — это
    отдельная проверка (`is_within_working_hours`), потому что «пора»
    и «можно сейчас» разные вопросы: диалог остаётся due и в 3 ночи, просто
    отправка откладывается до утра, а не отменяется."""
    if state.next_touch_due_at is None:
        return False
    if state.touch_count >= max_count:
        return False
    return state.next_touch_due_at <= now


def _parse_hhmm(value: str) -> TimeType:
    hour, minute = value.split(":")
    return TimeType(int(hour), int(minute))


def is_within_working_hours(dt: datetime, window: WorkingWindow) -> bool:
    """9:00–23:00 (или что задано в constants.working_window) — за пределами
    окна отложенные сообщения не уходят, регламент прямо это требует.

    ВРЕМЯ ПРИВОДИТСЯ К МОСКВЕ. Окно в базе знаний — часы работы комплекса,
    то есть московские; воркер же передаёт сюда `datetime.now(timezone.utc)`.
    Сравнение UTC-времени с московскими часами превращало окно 9:00–23:00 в
    фактические 12:00–02:00 МСК: касания могли уйти клиенту в час ночи и не
    уходили с 9 до 12 утра.

    Naive datetime считается уже московским — так его передают тесты; у
    aware время переводится в МСК явно.
    """
    start = _parse_hhmm(window.from_)
    end = _parse_hhmm(window.to)
    moment = dt.astimezone(MOSCOW_TZ) if dt.tzinfo is not None else dt
    t = moment.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end   # окно через полночь — на всякий случай, сейчас не наш случай


@dataclass(frozen=True)
class TouchOutcome:
    state: TouchState
    template_key: str          # TEMPLATE_SOFT | TEMPLATE_DIRECT
    touch_number: int


def advance_touch(state: TouchState, now: datetime, delay_minutes: int, max_count: int) -> TouchOutcome:
    """Диалог созрел (`is_due` уже True) — какой шаблон слать и как
    обновить состояние. Ничего не отправляет сама, только считает —
    отправка (и то, что произойдёт при DRY_RUN) остаётся за вызывающим
    кодом (app.ops.touch_scheduler)."""
    next_number = state.touch_count + 1
    template_key = TEMPLATE_SOFT if next_number < DIRECT_TOUCH_NUMBER else TEMPLATE_DIRECT

    new_due = None if next_number >= max_count else now + timedelta(minutes=delay_minutes)

    new_state = replace(
        state,
        touch_count=next_number,
        last_touch_at=now,
        next_touch_due_at=new_due,
    )
    return TouchOutcome(state=new_state, template_key=template_key, touch_number=next_number)


def force_to_max_on_price_objection(state: TouchState, max_count: int) -> TouchState:
    """Клиент прямо возразил по цене («дорого») — регламент разрешает
    скидку сразу же (см. R13 в app.pricing.concessions), а запланированное
    напоминание в этот момент теряет смысл: разговор о цене уже идёт живьём,
    добавлять к нему шаблонное «вы где-то затерялись?» позже незачем.
    touch_count поднимается до максимума, а не обнуляется — сама уступка
    уже не заблокирована этим правилом, дальше эскалировать некуда."""
    if state.touch_count >= max_count:
        return state
    return replace(state, touch_count=max_count, next_touch_due_at=None)
