"""Курсоры чатов — докуда поллер прочитал каждый чат.

Отдельно от `poller.py` по той же причине, по которой `SqlAlchemyTouchStore`
отделён от воркера касаний: поллер — это правила (кого будить, кого молчать),
хранилище — это SQL. Тест на правила не должен поднимать Postgres, поэтому
рядом с боевой реализацией живёт `InMemoryCursorStore`, и обе обязаны вести
себя одинаково.

Одна сессия на вызов — как и во всём остальном проекте: между шагами
конвейера стоит окно debounce длиной в десятки секунд, держать соединение
открытым всё это время незачем.
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
    cold_start_skipped: bool = False
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
            return CursorRecord(self.chat_id, created, merged, False, None, self.item_id)
        return CursorRecord(self.chat_id, created, ids, False, None, self.item_id)


class CursorStore(Protocol):
    async def load(self, chat_ids: list[str]) -> dict[str, CursorRecord]: ...
    async def save(self, record: CursorRecord) -> None: ...
    async def list_cold_start_skipped(self, limit: int = 200) -> list[CursorRecord]: ...
    async def clear_skip(self, chat_id: str) -> Optional[CursorRecord]: ...


@dataclass
class InMemoryCursorStore:
    """Для тестов и для `--dry` у пробника."""

    rows: dict[str, CursorRecord] = field(default_factory=dict)

    async def load(self, chat_ids: list[str]) -> dict[str, CursorRecord]:
        return {c: self.rows[c] for c in chat_ids if c in self.rows}

    async def save(self, record: CursorRecord) -> None:
        self.rows[record.chat_id] = record

    async def list_cold_start_skipped(self, limit: int = 200) -> list[CursorRecord]:
        return [r for r in self.rows.values() if r.cold_start_skipped][:limit]

    async def clear_skip(self, chat_id: str) -> Optional[CursorRecord]:
        row = self.rows.get(chat_id)
        if row is None:
            return None
        cleared = CursorRecord(
            row.chat_id, row.created, row.seen_ids, False, None, row.item_id
        )
        self.rows[chat_id] = cleared
        return row


class SqlAlchemyCursorStore:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    @staticmethod
    def _to_record(row) -> CursorRecord:
        return CursorRecord(
            chat_id=row.chat_id,
            created=int(row.last_message_created or 0),
            seen_ids=tuple(row.last_message_ids or ()),
            cold_start_skipped=bool(row.cold_start_skipped),
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
            row.cold_start_skipped = record.cold_start_skipped
            row.skipped_reason = record.skipped_reason
            if record.item_id is not None:
                row.item_id = record.item_id
            await session.commit()

    async def list_cold_start_skipped(self, limit: int = 200) -> list[CursorRecord]:
        from sqlalchemy import select

        from app.db.models import ChatCursor

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ChatCursor)
                    .where(ChatCursor.cold_start_skipped.is_(True))
                    .order_by(ChatCursor.last_message_created.desc())
                    .limit(limit)
                )
            ).scalars().all()
        return [self._to_record(row) for row in rows]

    async def clear_skip(self, chat_id: str) -> Optional[CursorRecord]:
        """Снять пометку «пропущен холодным стартом» и вернуть СТАРУЮ строку.

        Старую — потому что вызывающему (кнопке «обработать» в админке) нужен
        курсор ДО снятия: именно с него он будет перечитывать чат. После
        снятия эта информация в базе уже недоступна.
        """
        from sqlalchemy import select

        from app.db.models import ChatCursor

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ChatCursor).where(ChatCursor.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            previous = self._to_record(row)
            row.cold_start_skipped = False
            row.skipped_reason = None
            await session.commit()
        return previous
