"""Курсоры чатов — докуда поллер прочитал каждый чат.

Отдельно от `poller.py` по той же причине, по которой `SqlAlchemyTouchStore`
отделён от воркера касаний: поллер — это правила (кого будить, кого молчать),
хранилище — это SQL. Тест на правила не должен поднимать Postgres, поэтому
рядом с боевой реализацией живёт `InMemoryCursorStore`, и обе обязаны вести
себя одинаково.

Одна сессия на вызов — как и во всём остальном проекте: между шагами
конвейера стоит окно debounce длиной в десятки секунд, держать соединение
открытым всё это время незачем.

КУРСОР ОТВЕЧАЕТ ТОЛЬКО ЗА «ЧТО ЧИТАТЬ». До инцидента 2026-08-28 у него было
второе значение — `cold_start_skipped` решал ещё и «отвечать ли», и именно
это решение (курсор + POLLER_BACKFILL_HOURS + собственная развилка
«свежее/старое») разошлось на практике: 65 клиентов получили ответ в чат,
где последнее слово было от нескольких дней до нескольких месяцев назад.
Разбор — `already_seen` ниже сравнивала `seen_ids`, а холодный старт писал
курсор с реальным `created`, но ПУСТЫМ `seen_ids`; на следующем проходе
`created == self.created`, `message_id in ()` — False, и «уже решили
пропустить» читалось как «новое». Починка не в этом файле: «кому отвечать»
решает `AGENT_MIN_INBOUND_TS` в app/pipeline.py, единой точкой, независимо
от курсора, флагов, номера прохода и канала — так что ошибка курсора (какая
угодно, не только эта) больше не может привести к лишнему исходящему.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol

logger = logging.getLogger("parmangal.poller.cursors")


@dataclass(frozen=True)
class CursorRecord:
    """Позиция в чате.

    `seen_ids` — сообщения, попавшие ровно на `created`. Секундная
    гранулярность времени: поллер забирает `created >= курсор` (иначе второе
    сообщение той же секунды теряется навсегда) и отбрасывает уже виденные
    по этому списку.
    """

    chat_id: str
    created: int = 0
    seen_ids: tuple[str, ...] = ()
    skipped_reason: Optional[str] = None
    item_id: Optional[str] = None

    def already_seen(self, message_id: Optional[str], created: Optional[int]) -> bool:
        if created is None:
            return False
        if created < self.created:
            return True
        if created > self.created:
            return False
        return message_id is not None and message_id in self.seen_ids

    def advanced_by(self, message_id: Optional[str], created: Optional[int]) -> "CursorRecord":
        """Курсор после обработки одного сообщения.

        Двигается ПО КАЖДОМУ сообщению, а не по максимуму батча: упавшее
        сообщение не должно уносить курсор за все следующие, иначе одна
        ошибка молча съедает остаток переписки.
        """
        if created is None or created < self.created:
            return self
        ids = (message_id,) if message_id else ()
        if created == self.created:
            merged = self.seen_ids + tuple(i for i in ids if i not in self.seen_ids)
            # Список не растёт бесконечно: он относится к ОДНОЙ секунде, и
            # как только придёт сообщение следующей секунды, он обнулится.
            return CursorRecord(self.chat_id, created, merged, None, self.item_id)
        return CursorRecord(self.chat_id, created, ids, None, self.item_id)


class CursorStore(Protocol):
    async def load(self, chat_ids: list[str]) -> dict[str, CursorRecord]: ...
    async def save(self, record: CursorRecord) -> None: ...


@dataclass
class InMemoryCursorStore:
    """Для тестов и для `--dry` у пробника."""

    rows: dict[str, CursorRecord] = field(default_factory=dict)

    async def load(self, chat_ids: list[str]) -> dict[str, CursorRecord]:
        return {c: self.rows[c] for c in chat_ids if c in self.rows}

    async def save(self, record: CursorRecord) -> None:
        self.rows[record.chat_id] = record


class SqlAlchemyCursorStore:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    @staticmethod
    def _to_record(row) -> CursorRecord:
        return CursorRecord(
            chat_id=row.chat_id,
            created=int(row.last_message_created or 0),
            seen_ids=tuple(row.last_message_ids or ()),
            skipped_reason=row.skipped_reason,
            item_id=row.item_id,
        )

    async def load(self, chat_ids: list[str]) -> dict[str, CursorRecord]:
        """Все курсоры пачки ОДНИМ запросом.

        Не по одному на чат: проход смотрит до 1100 чатов, и запрос на
        каждый — это 1100 обращений к базе в минуту ради данных, которые
        целиком помещаются в один SELECT ... WHERE chat_id IN (...).
        """
        if not chat_ids:
            return {}

        from sqlalchemy import select

        from app.db.models import ChatCursor

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ChatCursor).where(ChatCursor.chat_id.in_(chat_ids))
                )
            ).scalars().all()
        return {row.chat_id: self._to_record(row) for row in rows}

    async def save(self, record: CursorRecord) -> None:
        from sqlalchemy import select

        from app.db.models import ChatCursor

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ChatCursor).where(ChatCursor.chat_id == record.chat_id)
                )
            ).scalar_one_or_none()

            if row is None:
                row = ChatCursor(chat_id=record.chat_id)
                session.add(row)

            row.last_message_created = record.created
            row.last_message_ids = list(record.seen_ids)
            row.skipped_reason = record.skipped_reason
            if record.item_id is not None:
                row.item_id = record.item_id
            await session.commit()
