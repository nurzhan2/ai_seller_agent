"""Хендлеры aiogram: тонкая обёртка над OpsService.

Вся логика — в OpsService, здесь только разбор callback_data, проверка прав и
ответ пользователю. Так поведение кнопок тестируется без Telegram.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.ops.bot import OpsService
from app.ops.notifications import render_stats

logger = logging.getLogger("parmangal.ops.handlers")

CALLBACK_ACTIONS = ("approve", "reject", "takeover", "return_ai")


def parse_callback(data: str) -> tuple[str, str]:
    action, _, chat_id = data.partition(":")
    return action, chat_id


def build_dispatcher(service: OpsService, stats_provider=None) -> Dispatcher:
    dp = Dispatcher()

    async def _guard(event, user_id: int) -> bool:
        if service.is_allowed(user_id):
            return True
        logger.warning("ops access denied", extra={"user_id": user_id})
        if isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        else:
            await event.answer("Нет доступа.")
        return False

    @dp.callback_query(F.data.regexp(r"^(approve|reject|takeover|return_ai):"))
    async def on_action(callback: CallbackQuery) -> None:
        if not await _guard(callback, callback.from_user.id):
            return

        action, chat_id = parse_callback(callback.data or "")
        user_id = callback.from_user.id

        if action == "approve":
            result = await service.approve(chat_id, user_id)
        elif action == "reject":
            result = await service.reject(chat_id, user_id)
        elif action == "takeover":
            result = await service.takeover(chat_id, user_id)
        else:
            result = await service.return_to_ai(chat_id, user_id)

        await callback.answer(result.get("message", "Готово"))

    @dp.message(Command("stats"))
    async def on_stats(message: Message) -> None:
        if not await _guard(message, message.from_user.id):
            return
        if stats_provider is None:
            await message.answer("Статистика пока недоступна.")
            return
        data = await stats_provider()
        await message.answer(render_stats(**data))

    @dp.message(Command("pause"))
    async def on_pause(message: Message) -> None:
        if not await _guard(message, message.from_user.id):
            return
        await message.answer(await service.pause_all(message.from_user.id))

    @dp.message(Command("resume"))
    async def on_resume(message: Message) -> None:
        if not await _guard(message, message.from_user.id):
            return
        await message.answer(await service.resume_all(message.from_user.id))

    @dp.message(Command("dryrun"))
    async def on_dryrun(message: Message) -> None:
        if not await _guard(message, message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or parts[1] not in ("on", "off"):
            await message.answer("Использование: /dryrun on | /dryrun off")
            return
        await message.answer(
            await service.set_dry_run(message.from_user.id, parts[1] == "on")
        )

    @dp.message(Command("provider"))
    async def on_provider(message: Message) -> None:
        if not await _guard(message, message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer(await service.show_provider())
            return
        await message.answer(await service.set_provider(message.from_user.id, parts[1].strip()))

    @dp.message(Command("chat"))
    async def on_chat(message: Message) -> None:
        if not await _guard(message, message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /chat <id>")
            return
        flags = await service.store.get_flags(parts[1].strip())
        await message.answer(
            f"Чат {parts[1].strip()}\n"
            f"У оператора: {'да' if flags.is_human_takeover else 'нет'}\n"
            f"ИИ включён: {'да' if flags.ai_enabled else 'нет'}\n"
            f"Ответов агента: {flags.agent_reply_count}"
        )

    @dp.message(F.reply_to_message)
    async def on_reply(message: Message) -> None:
        """Ответ реплаем на карточку диалога уходит клиенту в Авито."""
        if not await _guard(message, message.from_user.id):
            return
        chat_id = _chat_id_from_card(message.reply_to_message.text or "")
        if not chat_id:
            return
        result = await service.send_edited(chat_id, message.from_user.id, message.text or "")
        await message.answer(result["message"])

    return dp


def _chat_id_from_card(card_text: str) -> str | None:
    """Достаёт id чата из текста карточки, на которую ответили реплаем."""
    for line in card_text.splitlines():
        if "чат " in line:
            tail = line.split("чат ", 1)[1].strip()
            return tail.split()[0] if tail else None
    return None
