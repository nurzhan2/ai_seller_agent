"""Фоновый воркер отложенных касаний.

Один проход (`run_scheduler_pass`) находит диалоги с истёкшим таймером,
шлёт им следующее касание (шаблон из concessions.yaml) и обновляет
состояние. Ничего не решает про ЦЕНУ — это app.pricing.concessions (R13);
здесь только тайминг и доставка сообщения.

`TouchStore` — тот же приём, что и `OpsStore` в app/ops/state.py: протокол
плюс `InMemoryTouchStore` для тестов и `SqlAlchemyTouchStore` для прода,
чтобы логику прохода можно было проверять без реальной базы. «Рестарт
процесса не теряет запланированные касания» доказывается архитектурой —
состояние живёт в сторе, который переживает процесс, а не в памяти цикла.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional, Protocol

from app.agent.touch_tracking import TouchState, advance_touch, is_due, is_within_working_hours
from app.kb.loader import WorkingWindow

logger = logging.getLogger("parmangal.touch_scheduler")


@dataclass(frozen=True)
class TouchDialog:
    chat_id: str
    state: TouchState


class TouchStore(Protocol):
    async def list_due(self, now: datetime, max_count: int) -> list[TouchDialog]: ...
    async def save(self, chat_id: str, state: TouchState) -> None: ...


@dataclass
class InMemoryTouchStore:
    """Для тестов и локального прогона — тот же снимок, что держит БД,
    просто в словаре процесса."""

    dialogs: dict[str, TouchState] = field(default_factory=dict)

    async def list_due(self, now: datetime, max_count: int) -> list[TouchDialog]:
        return [
            TouchDialog(chat_id=chat_id, state=state)
            for chat_id, state in self.dialogs.items()
            if is_due(state, now, max_count)
        ]

    async def save(self, chat_id: str, state: TouchState) -> None:
        self.dialogs[chat_id] = state


class SqlAlchemyTouchStore:
    """Прод: читает/пишет touch_* колонки `DialogState` напрямую.

    Один `AsyncSession` на проход — соответствует тому, как остальной код
    в проекте открывает и закрывает сессии за один вызов, не держит их
    между проходами воркера.
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def list_due(self, now: datetime, max_count: int) -> list[TouchDialog]:
        from sqlalchemy import select

        from app.db.models import DialogState

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(DialogState).where(
                        DialogState.next_touch_due_at.is_not(None),
                        DialogState.next_touch_due_at <= now,
                        DialogState.touch_count < max_count,
                    )
                )
            ).scalars().all()
            return [
                TouchDialog(
                    chat_id=row.chat_id,
                    state=TouchState(
                        touch_count=row.touch_count,
                        last_touch_at=row.last_touch_at,
                        next_touch_due_at=row.next_touch_due_at,
                    ),
                )
                for row in rows
            ]

    async def save(self, chat_id: str, state: TouchState) -> None:
        from sqlalchemy import select

        from app.db.models import DialogState

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(DialogState).where(DialogState.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            if row is None:
                logger.warning("touch scheduler: no DialogState row for chat", extra={"chat_id": chat_id})
                return
            row.touch_count = state.touch_count
            row.last_touch_at = state.last_touch_at
            row.next_touch_due_at = state.next_touch_due_at
            await session.commit()


Sender = Callable[[str, str], Awaitable[None]]
# chat_id -> можно ли вообще писать в этот чат (белый список объявлений).
CanSend = Callable[[str], Awaitable[bool]]


def _disarmed(state: TouchState) -> TouchState:
    """Тот же счётчик касаний, но без срока следующего.

    Счётчик не обнуляем: он — история («сколько раз мы этого клиента уже
    трогали»), и переписывать её из-за фильтра неправильно. Гасим ровно
    то, из-за чего диалог продолжает всплывать в `list_due`.
    """
    return TouchState(
        touch_count=state.touch_count,
        last_touch_at=state.last_touch_at,
        next_touch_due_at=None,
    )


async def run_scheduler_pass(
    store: TouchStore,
    templates: dict[str, str],
    working_window: WorkingWindow,
    send: Sender,
    now: datetime,
    *,
    delay_minutes: int,
    max_count: int,
    can_send: Optional[CanSend] = None,
) -> list[str]:
    """Один проход. Возвращает chat_id всех диалогов, которым реально
    отправили касание за этот проход.

    Ночная проверка — на уровне всего прохода, а не решение "не отправлять
    молча": диалоги остаются due (next_touch_due_at не трогается), поэтому
    следующий проход воркера (после открытия окна) их подхватит сам —
    никакого отдельного пересчёта "когда открыть окно" не нужно.

    `can_send` — белый список объявлений (`OutboundGate.is_allowed`).
    Проверяется ЗДЕСЬ, а не только внутри `send`, по двум причинам: гейт
    молча вернул бы «заблокировано», а воркер всё равно записал бы касание
    как отправленное и сдвинул счётчик; и таймер такого чата надо не
    пропустить, а ПОГАСИТЬ — иначе он остаётся due навсегда и воркер
    спотыкается о него каждую минуту до конца времён. Гашение здесь же
    работает и как разовая уборка: чаты, попавшие в таблицу касаний до
    появления фильтра (именно так туда попал u2u-чат из инцидента),
    вычищаются сами при первой же попытке их коснуться.
    """
    if not is_within_working_hours(now, working_window):
        return []

    touched: list[str] = []
    for dialog in await store.list_due(now, max_count):
        if can_send is not None and not await can_send(dialog.chat_id):
            await store.save(dialog.chat_id, _disarmed(dialog.state))
            logger.info(
                "touch scheduler: касание отменено — чат вне белого списка объявлений, "
                "таймер погашен",
                extra={"chat_id": dialog.chat_id},
            )
            continue
        outcome = advance_touch(dialog.state, now, delay_minutes, max_count)
        text = templates[outcome.template_key]
        try:
            await send(dialog.chat_id, text)
        except Exception:
            logger.exception(
                "touch scheduler: send failed, state not advanced",
                extra={"chat_id": dialog.chat_id, "touch_number": outcome.touch_number},
            )
            continue
        await store.save(dialog.chat_id, outcome.state)
        touched.append(dialog.chat_id)
        logger.info(
            "touch sent",
            extra={"chat_id": dialog.chat_id, "touch_number": outcome.touch_number},
        )
    return touched
