"""Склейка сообщений и задержка перед ответом.

Клиенты на Авито пишут очередями: «Здравствуйте», «а сколько стоит», «на
субботу». Без склейки агент ответит трижды, причём на первое сообщение — до
того, как узнает дату из третьего.

Задержка перед отправкой — не косметика: мгновенный ответ на длинный вопрос
читается как автоответчик и снижает доверие ещё до того, как клиент оценит
содержание.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("parmangal.debounce")


@dataclass
class _Pending:
    parts: list[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = None


class Debouncer:
    """Копит сообщения одного чата и отдаёт их одной склейкой.

    Каждое новое сообщение перезапускает окно: клиент, который печатает
    подряд, не получит ответ, пока не закончит мысль.
    """

    def __init__(
        self,
        window_seconds: float,
        handler: Callable[[str, str], Awaitable[None]],
    ):
        self.window_seconds = window_seconds
        self.handler = handler
        self._pending: dict[str, _Pending] = {}

    async def submit(self, chat_id: str, text: str) -> None:
        pending = self._pending.setdefault(chat_id, _Pending())
        pending.parts.append(text)

        if pending.task is not None:
            pending.task.cancel()
        pending.task = asyncio.create_task(self._fire_later(chat_id))

    async def _fire_later(self, chat_id: str) -> None:
        try:
            await asyncio.sleep(self.window_seconds)
        except asyncio.CancelledError:
            return

        pending = self._pending.pop(chat_id, None)
        if pending is None or not pending.parts:
            return

        merged = "\n".join(pending.parts)
        logger.info(
            "debounce flush", extra={"chat_id": chat_id, "parts": len(pending.parts)}
        )
        await self.handler(chat_id, merged)

    async def flush_now(self, chat_id: str) -> Optional[str]:
        """Отдать накопленное немедленно (используется в тестах и при
        аварийном завершении процесса, чтобы не потерять сообщения)."""
        pending = self._pending.pop(chat_id, None)
        if pending is None:
            return None
        if pending.task is not None:
            pending.task.cancel()
        return "\n".join(pending.parts) if pending.parts else None

    def pending_chats(self) -> list[str]:
        return list(self._pending)


async def human_delay(min_seconds: float = 3.0, max_seconds: float = 8.0) -> None:
    """Пауза перед отправкой, чтобы ответ не выглядел автоматическим."""
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))
