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


def build_dispatcher(service: OpsService, stats_provider=None, menu_service=None) -> Dispatcher:
    dp = Dispatcher()

    async def _send(event, reply) -> None:
        """Показать `Reply` из MenuService.

        `edit=True` — правим сообщение с меню на месте, чтобы чат не
        зарастал копиями одного и того же меню. Правка чужого/устаревшего
        сообщения — штатная ошибка Telegram («message is not modified»,
        «message can't be edited»), и она не должна выглядеть как сломанная
        кнопка: падаем обратно на новое сообщение.
        """
        if reply.alert:
            await event.answer(reply.alert, show_alert=True)
            return
        if isinstance(event, CallbackQuery):
            if reply.edit and event.message is not None:
                try:
                    await event.message.edit_text(reply.text, reply_markup=reply.markup)
                    return
                except Exception:
                    logger.debug("edit_text failed, sending a new message", exc_info=True)
            if event.message is not None:
                await event.message.answer(reply.text, reply_markup=reply.markup)
            return
        await event.answer(reply.text, reply_markup=reply.markup)

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

    @dp.message(Command("moderation"))
    async def on_moderation(message: Message) -> None:
        if not await _guard(message, message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer(await service.show_moderation_mode())
            return
        await message.answer(
            await service.set_moderation_mode(message.from_user.id, parts[1].strip())
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

    @dp.message(Command("reset"))
    async def on_reset(message: Message) -> None:
        """Сбросить счётчик ответов агента в одном чате.

        До этой команды исчерпанный лимит снимался только правкой в базе.
        Лимит на все чаты она НЕ трогает — для этого
        AGENT_MAX_REPLIES_PER_CHAT.
        """
        if not await _guard(message, message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer(
                "Использование: /reset <chat_id>\n"
                "Сбрасывает счётчик ответов агента в этом чате. "
                "Узнать счётчик: /chat <chat_id>"
            )
            return
        result = await service.reset_reply_count(parts[1].strip(), message.from_user.id)
        await message.answer(result["message"])

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

    # ---- инлайн-меню управления ассистентом -------------------------------
    #
    # Регистрируется только если меню собрано (menu_service передан). Без
    # него бот работает ровно как раньше — старые кнопки модерации и
    # команды никуда не делись.
    if menu_service is not None:

        @dp.message(Command("menu", "start"))
        async def on_menu(message: Message) -> None:
            if not await _guard(message, message.from_user.id):
                return
            await _send(message, await menu_service.root(message.from_user.id))

        @dp.callback_query(F.data.startswith("m:"))
        async def on_section(callback: CallbackQuery) -> None:
            if not await _guard(callback, callback.from_user.id):
                return
            await callback.answer()
            _, _, section = (callback.data or "").partition(":")
            await _send(callback, await menu_service.open_section(callback.from_user.id, section))

        @dp.callback_query(F.data.startswith("z:"))
        async def on_zone(callback: CallbackQuery) -> None:
            if not await _guard(callback, callback.from_user.id):
                return
            await callback.answer()
            _, _, zone_id = (callback.data or "").partition(":")
            await _send(callback, await menu_service.open_zone(callback.from_user.id, zone_id))

        @dp.callback_query(F.data.startswith("e:"))
        async def on_edit(callback: CallbackQuery) -> None:
            if not await _guard(callback, callback.from_user.id):
                return
            await callback.answer()
            parts = (callback.data or "").split(":")
            field_key = parts[1] if len(parts) > 1 else ""
            zone_id = parts[2] if len(parts) > 2 else None
            await _send(callback, await menu_service.start_edit(
                callback.from_user.id, field_key, zone_id))

        @dp.callback_query(F.data.startswith("ok:"))
        async def on_confirm(callback: CallbackQuery) -> None:
            if not await _guard(callback, callback.from_user.id):
                return
            await callback.answer()
            _, _, token = (callback.data or "").partition(":")
            await _send(callback, await menu_service.confirm(callback.from_user.id, token))

        @dp.callback_query(F.data.startswith("no:"))
        async def on_cancel(callback: CallbackQuery) -> None:
            if not await _guard(callback, callback.from_user.id):
                return
            await callback.answer()
            _, _, token = (callback.data or "").partition(":")
            await _send(callback, await menu_service.cancel(callback.from_user.id, token))

        @dp.callback_query(F.data.startswith("md:"))
        async def on_mode(callback: CallbackQuery) -> None:
            if not await _guard(callback, callback.from_user.id):
                return
            await callback.answer()
            _, _, mode = (callback.data or "").partition(":")
            await _send(callback, await menu_service.set_moderation(callback.from_user.id, mode))

        @dp.callback_query(F.data.startswith("tg:"))
        async def on_toggle(callback: CallbackQuery) -> None:
            if not await _guard(callback, callback.from_user.id):
                return
            await callback.answer()
            _, _, action = (callback.data or "").partition(":")
            await _send(callback, await menu_service.toggle(callback.from_user.id, action))

        @dp.callback_query(F.data.startswith("rv:"))
        async def on_revert(callback: CallbackQuery) -> None:
            if not await _guard(callback, callback.from_user.id):
                return
            await callback.answer()
            await _send(callback, await menu_service.revert_last(callback.from_user.id))

        # Обычное сообщение — возможно, это присланное значение для правки.
        # Стоит ПОСЛЕДНИМ и ниже F.reply_to_message: реплай на карточку
        # диалога — это ответ клиенту, и перехватывать его нельзя.
        @dp.message(F.text)
        async def on_value(message: Message) -> None:
            reply = await menu_service.receive_value(message.from_user.id, message.text or "")
            if reply is None:
                return          # не наш текст — молчим, как и раньше
            await _send(message, reply)

    return dp


# Меню команд Telegram — появляется в интерфейсе само (пункт 5).
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("menu", "Управление ассистентом"),
    ("stats", "Статистика"),
    ("moderation", "Режим модерации"),
    ("dryrun", "DRY_RUN вкл/выкл"),
    ("pause", "Поставить агента на паузу"),
    ("resume", "Снять агента с паузы"),
    ("provider", "LLM-провайдер"),
    ("chat", "Статус чата по id"),
    ("reset", "Сбросить счётчик ответов в чате"),
)


async def set_bot_commands(bot: Bot) -> None:
    """Вызывается при старте приложения. Сбой не должен ронять запуск:
    список команд — удобство, а не работоспособность бота."""
    from aiogram.types import BotCommand

    try:
        await bot.set_my_commands([
            BotCommand(command=name, description=description)
            for name, description in BOT_COMMANDS
        ])
        logger.info("telegram bot commands registered")
    except Exception:
        logger.exception("failed to register telegram bot commands")


def _chat_id_from_card(card_text: str) -> str | None:
    """Достаёт id чата из текста карточки, на которую ответили реплаем."""
    for line in card_text.splitlines():
        if "чат " in line:
            tail = line.split("чат ", 1)[1].strip()
            return tail.split()[0] if tail else None
    return None
