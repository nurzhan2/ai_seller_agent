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


def should_auto_return(flags: ChatFlags, now: Optional[datetime] = None) -> bool:
    """Оператор взял чат и забыл — через сутки возвращаем агенту."""
    if not flags.is_human_takeover or flags.takeover_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - flags.takeover_at >= TAKEOVER_TIMEOUT
