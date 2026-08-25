"""Состояние операторского контура.

Абстрагировано за протоколом, потому что в тестах и в проде источники разные
(в проде — Postgres, в тестах — словарь), а логика кнопок должна быть одна.

Идемпотентность кнопок живёт здесь: Telegram доставляет callback повторно при
плохой связи, и «Одобрить» не должно отправить сообщение клиенту дважды.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Protocol

# Через сколько бездействия оператора чат возвращается агенту.
TAKEOVER_TIMEOUT = timedelta(hours=24)


@dataclass
class PendingReply:
    chat_id: str
    text: str
    created_at: datetime
    status: str = "pending"          # pending | approved | rejected | edited
    decided_by: Optional[int] = None
    # --- Модерация ценовых уступок (MODERATION_MODE=concessions_only) ----
    # is_concession=True — этот pending на самом деле запрос на скидку, а
    # не обычный DRY_RUN-холд: у него есть дедлайн, и по истечении фоновый
    # воркер (app.pipeline.MessagePipeline.check_concession_timeouts)
    # отправит fallback_text вместо ожидания оператора вечно.
    is_concession: bool = False
    fallback_text: Optional[str] = None
    due_at: Optional[datetime] = None


@dataclass
class ChatFlags:
    is_human_takeover: bool = False
    ai_enabled: bool = True
    takeover_at: Optional[datetime] = None
    agent_reply_count: int = 0


class OpsStore(Protocol):
    async def get_flags(self, chat_id: str) -> ChatFlags: ...
    async def set_flags(self, chat_id: str, flags: ChatFlags) -> None: ...
    async def get_pending(self, chat_id: str) -> Optional[PendingReply]: ...
    async def set_pending(self, chat_id: str, reply: Optional[PendingReply]) -> None: ...
    async def log_action(self, chat_id: str, user_id: int, action: str, payload: dict) -> None: ...
    async def list_due_concessions(self, now: datetime) -> list[PendingReply]: ...


@dataclass
class InMemoryOpsStore:
    """Реализация для тестов и локального запуска."""

    flags: dict[str, ChatFlags] = field(default_factory=dict)
    pending: dict[str, PendingReply] = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)
    moderation: dict[str, int] = field(
        default_factory=lambda: {"approved": 0, "edited": 0, "rejected": 0}
    )

    async def get_flags(self, chat_id: str) -> ChatFlags:
        return self.flags.setdefault(chat_id, ChatFlags())

    async def set_flags(self, chat_id: str, flags: ChatFlags) -> None:
        self.flags[chat_id] = flags

    async def get_pending(self, chat_id: str) -> Optional[PendingReply]:
        return self.pending.get(chat_id)

    async def set_pending(self, chat_id: str, reply: Optional[PendingReply]) -> None:
        if reply is None:
            self.pending.pop(chat_id, None)
        else:
            self.pending[chat_id] = reply

    async def log_action(self, chat_id: str, user_id: int, action: str, payload: dict) -> None:
        # Каждое действие оператора логируется вместе с его user_id — иначе
        # разбор «кто одобрил неверную цену» упирается в догадки.
        self.actions.append(
            {
                "chat_id": chat_id,
                "user_id": user_id,
                "action": action,
                "payload": payload,
                "at": datetime.now(timezone.utc),
            }
        )

    def bump_moderation(self, key: str) -> None:
        self.moderation[key] = self.moderation.get(key, 0) + 1

    async def list_due_concessions(self, now: datetime) -> list[PendingReply]:
        return [
            reply for reply in self.pending.values()
            if reply.is_concession and reply.due_at is not None and reply.due_at <= now
        ]


def _pending_reply_from_row(row) -> PendingReply:
    return PendingReply(
        chat_id=row.chat_id,
        text=row.text,
        created_at=row.created_at,
        status=row.status,
        decided_by=row.decided_by,
        is_concession=row.is_concession,
        fallback_text=row.fallback_text,
        due_at=row.due_at,
    )


class SqlAlchemyOpsStore:
    """Тот же интерфейс поверх БД — состояние модерации переживает рестарт.

    Что где лежит:
      * `ChatFlags`      → колонки таблицы `chats` (is_human_takeover,
                           ai_enabled, takeover_at, agent_reply_count);
      * `PendingReply`   → таблица `pending_replies`, одна строка на чат;
      * `log_action`     → таблица `operator_actions`.

    Счётчики модерации (approved/edited/rejected) СЧИТАЮТСЯ ИЗ
    `operator_actions`, а не хранятся отдельным полем: эти действия и так
    логируются поимённо, а второй источник тех же чисел рано или поздно
    разойдётся с первым. `bump_moderation` поэтому здесь пустой — считать
    нечего, всё уже записано в журнале.

    Одна сессия на вызов — как у `SqlAlchemyTouchStore` и
    `SqlAlchemyDialogStore`: между нажатиями кнопок оператора проходят
    минуты, держать соединение открытым незачем.
    """

    # Действие в operator_actions -> ключ метрики модерации.
    _MODERATION_ACTIONS = {"approve": "approved", "reject": "rejected", "send_edited": "edited"}

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def get_flags(self, chat_id: str) -> ChatFlags:
        from sqlalchemy import select

        from app.db.models import Chat

        async with self._session_factory() as session:
            chat = (
                await session.execute(select(Chat).where(Chat.chat_id == chat_id))
            ).scalar_one_or_none()

        if chat is None:
            # Чата ещё нет — значения по умолчанию, но БЕЗ создания строки:
            # чтение не должно писать. Строку заведёт конвейер, когда придёт
            # первое сообщение.
            return ChatFlags()

        return ChatFlags(
            is_human_takeover=chat.is_human_takeover,
            ai_enabled=chat.ai_enabled,
            takeover_at=chat.takeover_at,
            agent_reply_count=chat.agent_reply_count or 0,
        )

    async def set_flags(self, chat_id: str, flags: ChatFlags) -> None:
        from sqlalchemy import select

        from app.db.models import Chat

        async with self._session_factory() as session:
            chat = (
                await session.execute(select(Chat).where(Chat.chat_id == chat_id))
            ).scalar_one_or_none()
            if chat is None:
                # Оператор может забрать чат раньше, чем конвейер успел его
                # завести (например, по карточке от воркера касаний).
                chat = Chat(chat_id=chat_id)
                session.add(chat)
            chat.is_human_takeover = flags.is_human_takeover
            chat.ai_enabled = flags.ai_enabled
            chat.takeover_at = flags.takeover_at
            chat.agent_reply_count = flags.agent_reply_count
            await session.commit()

    async def get_pending(self, chat_id: str) -> Optional[PendingReply]:
        from sqlalchemy import select

        from app.db.models import PendingReplyRow

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(PendingReplyRow).where(PendingReplyRow.chat_id == chat_id)
                )
            ).scalar_one_or_none()

        if row is None:
            return None
        return _pending_reply_from_row(row)

    async def set_pending(self, chat_id: str, reply: Optional[PendingReply]) -> None:
        from sqlalchemy import delete, select

        from app.db.models import PendingReplyRow

        async with self._session_factory() as session:
            if reply is None:
                await session.execute(
                    delete(PendingReplyRow).where(PendingReplyRow.chat_id == chat_id)
                )
                await session.commit()
                return

            row = (
                await session.execute(
                    select(PendingReplyRow).where(PendingReplyRow.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = PendingReplyRow(chat_id=chat_id)
                session.add(row)
            row.text = reply.text
            row.status = reply.status
            row.decided_by = reply.decided_by
            row.is_concession = reply.is_concession
            row.fallback_text = reply.fallback_text
            row.due_at = reply.due_at
            await session.commit()

    async def list_due_concessions(self, now: datetime) -> list[PendingReply]:
        from sqlalchemy import select

        from app.db.models import PendingReplyRow

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(PendingReplyRow).where(
                        PendingReplyRow.is_concession.is_(True),
                        PendingReplyRow.due_at.is_not(None),
                        PendingReplyRow.due_at <= now,
                    )
                )
            ).scalars().all()
        return [_pending_reply_from_row(row) for row in rows]

    async def log_action(self, chat_id: str, user_id: int, action: str, payload: dict) -> None:
        from app.db.models import OperatorAction

        async with self._session_factory() as session:
            session.add(
                OperatorAction(
                    chat_id=chat_id, telegram_user_id=user_id, action=action, payload=payload
                )
            )
            await session.commit()

    def bump_moderation(self, key: str) -> None:
        """Ничего не делает намеренно — см. докстринг класса: числа
        восстанавливаются из `operator_actions` в `moderation_stats()`."""
        return None

    async def moderation_stats(self) -> dict[str, int]:
        """{'approved': n, 'edited': n, 'rejected': n} из журнала действий."""
        from sqlalchemy import func as sa_func, select

        from app.db.models import OperatorAction

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(OperatorAction.action, sa_func.count())
                    .where(OperatorAction.action.in_(tuple(self._MODERATION_ACTIONS)))
                    .group_by(OperatorAction.action)
                )
            ).all()

        stats = {"approved": 0, "edited": 0, "rejected": 0}
        for action, count in rows:
            stats[self._MODERATION_ACTIONS[action]] = count
        return stats


def should_auto_return(flags: ChatFlags, now: Optional[datetime] = None) -> bool:
    """Оператор взял чат и забыл — через сутки возвращаем агенту."""
    if not flags.is_human_takeover or flags.takeover_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - flags.takeover_at >= TAKEOVER_TIMEOUT
