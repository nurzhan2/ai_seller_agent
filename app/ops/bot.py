"""Телеграм-бот оператора (aiogram 3).

Каждое действие проходит два фильтра: пользователь должен быть в
TELEGRAM_ALLOWED_USERS, и действие должно быть идемпотентным. Второе не менее
важно первого: Telegram переотправляет callback при плохой связи, и повторное
«Одобрить» не должно отправить клиенту второе сообщение.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from app.config import Settings, get_settings
from app.ops.state import ChatFlags, InMemoryOpsStore, OpsStore, PendingReply, should_auto_return

logger = logging.getLogger("parmangal.ops")


class OpsService:
    """Логика операторских действий, независимая от aiogram.

    Отделена от хендлеров, чтобы тестировать поведение кнопок без Telegram и
    без сети — именно здесь живут правила, а не в декораторах.
    """

    def __init__(
        self,
        store: Optional[OpsStore] = None,
        settings: Optional[Settings] = None,
        send_to_avito: Optional[Callable[[str, str], Awaitable[Any]]] = None,
    ):
        self.store = store or InMemoryOpsStore()
        self.settings = settings or get_settings()
        self.send_to_avito = send_to_avito

    # -- доступ ------------------------------------------------------------

    def is_allowed(self, user_id: int) -> bool:
        allowed = self.settings.telegram_allowed_users
        # Пустой список означает «никому», а не «всем»: пустая настройка не
        # должна открывать управление ботом посторонним.
        return bool(allowed) and user_id in allowed

    # -- перехват ----------------------------------------------------------

    async def takeover(self, chat_id: str, user_id: int) -> dict:
        flags = await self.store.get_flags(chat_id)
        if flags.is_human_takeover:
            return {"changed": False, "message": "Чат уже у оператора"}

        flags.is_human_takeover = True
        flags.takeover_at = datetime.now(timezone.utc)
        await self.store.set_flags(chat_id, flags)
        await self.store.log_action(chat_id, user_id, "takeover", {})
        return {"changed": True, "message": "Чат ваш, агент молчит"}

    async def return_to_ai(self, chat_id: str, user_id: int) -> dict:
        flags = await self.store.get_flags(chat_id)
        if not flags.is_human_takeover:
            return {"changed": False, "message": "Чат и так у агента"}

        flags.is_human_takeover = False
        flags.takeover_at = None
        await self.store.set_flags(chat_id, flags)
        await self.store.log_action(chat_id, user_id, "return_ai", {})
        return {"changed": True, "message": "Агент снова отвечает"}

    async def auto_return_if_stale(self, chat_id: str) -> bool:
        flags = await self.store.get_flags(chat_id)
        if not should_auto_return(flags):
            return False
        flags.is_human_takeover = False
        flags.takeover_at = None
        await self.store.set_flags(chat_id, flags)
        await self.store.log_action(chat_id, 0, "auto_return", {"reason": "24h без активности"})
        return True

    # -- модерация ---------------------------------------------------------

    async def queue_reply(self, chat_id: str, text: str) -> PendingReply:
        reply = PendingReply(chat_id=chat_id, text=text, created_at=datetime.now(timezone.utc))
        await self.store.set_pending(chat_id, reply)
        return reply

    async def approve(self, chat_id: str, user_id: int) -> dict:
        pending = await self.store.get_pending(chat_id)
        if pending is None:
            # Идемпотентность: повторный клик не отправляет второе сообщение.
            return {"sent": False, "message": "Нечего отправлять — уже обработано"}
        if pending.status != "pending":
            return {"sent": False, "message": f"Уже {pending.status}"}

        pending.status = "approved"
        pending.decided_by = user_id
        await self.store.set_pending(chat_id, None)
        await self.store.log_action(chat_id, user_id, "approve", {"text": pending.text})
        self._count("approved")

        if self.send_to_avito is not None:
            await self.send_to_avito(chat_id, pending.text)
        return {"sent": True, "message": "Отправлено клиенту"}

    async def reject(self, chat_id: str, user_id: int) -> dict:
        pending = await self.store.get_pending(chat_id)
        if pending is None:
            return {"sent": False, "message": "Нечего отклонять"}
        pending.status = "rejected"
        pending.decided_by = user_id
        await self.store.set_pending(chat_id, None)
        await self.store.log_action(chat_id, user_id, "reject", {"text": pending.text})
        self._count("rejected")
        return {"sent": False, "message": "Отклонено, клиенту ничего не ушло"}

    async def send_edited(self, chat_id: str, user_id: int, text: str) -> dict:
        """Оператор ответил реплаем — его текст уходит клиенту вместо ответа
        агента, а исходный ответ считается исправленным (это метрика качества,
        а не просто отправка)."""
        pending = await self.store.get_pending(chat_id)
        if pending is not None:
            pending.status = "edited"
            await self.store.set_pending(chat_id, None)
            self._count("edited")
        await self.store.log_action(chat_id, user_id, "send_edited", {"text": text})
        if self.send_to_avito is not None:
            await self.send_to_avito(chat_id, text)
        return {"sent": True, "message": "Ваш текст отправлен"}

    def _count(self, key: str) -> None:
        """Счётчик модерации — по утиному типу, а не по `isinstance`.

        Раньше здесь стояла проверка `isinstance(store, InMemoryOpsStore)`, и
        любая другая реализация стора молча переставала считать одобрения:
        метрика, по которой принимается решение выключить модерацию
        (90% чистых одобрений три дня подряд, docs/GO_LIVE.md), обнулилась бы
        незаметно ровно в тот момент, когда мы перешли на БД.
        """
        bump = getattr(self.store, "bump_moderation", None)
        if bump is not None:
            bump(key)

    # -- рубильник ---------------------------------------------------------

    async def pause_all(self, user_id: int) -> str:
        self.settings.agent_paused = True
        await self.store.log_action("*", user_id, "pause", {})
        return "⏸ Агент на паузе. Все чаты — на операторе."

    async def resume_all(self, user_id: int) -> str:
        self.settings.agent_paused = False
        await self.store.log_action("*", user_id, "resume", {})
        return "▶️ Агент снова в работе."

    async def set_dry_run(self, user_id: int, enabled: bool) -> str:
        self.settings.dry_run = enabled
        await self.store.log_action("*", user_id, "set_dry_run", {"enabled": enabled})
        if enabled:
            return "🔒 DRY_RUN включён: каждый ответ требует одобрения."
        return (
            "🔓 DRY_RUN ВЫКЛЮЧЕН: агент пишет клиентам сам, без модерации.\n"
            "Убедитесь, что метрики позволяют."
        )

    # -- модерация -----------------------------------------------------------

    _MODERATION_MODES = ("all", "concessions_only", "off")

    async def show_moderation_mode(self) -> str:
        mode = self.settings.moderation_mode
        return f"Режим модерации: {mode}.\nИспользование: /moderation all|concessions_only|off"

    async def set_moderation_mode(self, user_id: int, mode: str) -> str:
        if mode not in self._MODERATION_MODES:
            return "Использование: /moderation all|concessions_only|off"
        if mode == self.settings.moderation_mode:
            return f"Уже {mode}."
        previous = self.settings.moderation_mode
        self.settings.moderation_mode = mode
        await self.store.log_action(
            "*", user_id, "set_moderation_mode", {"from": previous, "to": mode}
        )
        explain = {
            "all": "держим на одобрении всё, как раньше",
            "concessions_only": "одобрение только на ценовую уступку",
            "off": "полная автономия, включая ценовые уступки",
        }[mode]
        return f"Режим модерации: {previous} → {mode} ({explain})."

    # -- LLM-провайдер (промт №12) ------------------------------------------

    async def show_provider(self) -> str:
        active = self.settings.llm_provider
        fallback = self.settings.llm_fallback_provider
        line = f"Текущий провайдер: {active}."
        if fallback:
            line += f"\nРезервный: {fallback} (после {self.settings.llm_fallback_after_errors} ошибок подряд)."
        else:
            line += "\nРезервный не настроен."
        return line

    async def set_provider(self, user_id: int, provider: str) -> str:
        if provider not in ("anthropic", "deepseek"):
            return "Использование: /provider [anthropic | deepseek]"
        if provider == self.settings.llm_provider:
            return f"Уже на {provider}."
        previous = self.settings.llm_provider
        self.settings.llm_provider = provider
        await self.store.log_action(
            "*", user_id, "set_provider", {"from": previous, "to": provider}
        )
        return f"Провайдер переключён: {previous} → {provider}."

    async def reset_reply_count(self, chat_id: str, user_id: int) -> dict:
        """Обнулить счётчик ответов агента в одном чате.

        До этого исчерпанный лимит можно было снять только руками в базе:
        счётчик живёт в двух местах сразу (`chats.agent_reply_count` и
        `ChatFlags` в OpsStore — см. app/pipeline.py:_count_agent_reply), и
        обнулить надо оба. `SqlAlchemyOpsStore.set_flags` пишет в ту же
        колонку `chats`, что читает админка, поэтому одного вызова хватает —
        но именно поэтому обнулять надо через стор, а не UPDATE мимо него.

        Сам лимит НЕ меняется: это разовое «дай доработать этот диалог», а
        не «подними планку всем» (для второго — AGENT_MAX_REPLIES_PER_CHAT).
        """
        flags = await self.store.get_flags(chat_id)
        previous = flags.agent_reply_count
        if previous == 0:
            return {"changed": False, "message": f"Счётчик чата {chat_id} и так пуст"}

        flags.agent_reply_count = 0
        await self.store.set_flags(chat_id, flags)
        await self.store.log_action(chat_id, user_id, "reset_reply_count", {"was": previous})
        limit = self.settings.max_agent_replies_per_chat
        return {
            "changed": True,
            "message": (
                f"Счётчик чата {chat_id} сброшен: было {previous}, стало 0. "
                f"Агент снова отвечает (лимит {limit})."
            ),
        }

    async def should_agent_reply(self, chat_id: str) -> tuple[bool, str]:
        """Единая точка решения «отвечает ли агент».

        Собрана в одном месте, чтобы ни один из четырёх стопоров нельзя было
        случайно обойти новым кодом.
        """
        if getattr(self.settings, "agent_paused", False):
            return False, "агент на паузе"

        await self.auto_return_if_stale(chat_id)
        flags = await self.store.get_flags(chat_id)

        if flags.is_human_takeover:
            return False, "чат у оператора"
        if not flags.ai_enabled:
            return False, "ИИ выключен для этого чата"
        if flags.agent_reply_count >= self.settings.max_agent_replies_per_chat:
            return False, "исчерпан лимит ответов агента в чате"
        return True, ""
