"""Логика меню бота, отделённая от aiogram.

Тот же принцип, что у `OpsService`: хендлер разбирает callback_data и
отправляет результат, а РЕШЕНИЯ принимаются здесь — и проверяются тестами
без Telegram и без сети.

ПРО ПРОВЕРКУ ПРАВ. Она делается ЗДЕСЬ, в каждом методе, а не только в
хендлере. Дублирование намеренное: хендлер — это внешний слой, и однажды
кто-нибудь добавит в него новую кнопку, забыв `_guard`. Правка каталога —
не то место, где такую забывчивость можно позволить: цена, изменённая
посторонним, уедет живому клиенту. Поэтому отказ живёт рядом с действием.

ПРО СОСТОЯНИЕ ВВОДА. `pending` — то, что оператор сейчас правит: между
нажатием кнопки поля и присланным значением бот должен помнить, о чём
речь. Живёт в памяти процесса, и это осознанно: незавершённая правка,
потерянная при рестарте, стоит одного лишнего нажатия, а хранить её в БД
значило бы заводить таблицу ради данных со временем жизни в полминуты.
Ключ — user_id: два оператора правят независимо.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from app.kb.editable import field_by_key
from app.kb.editor import CatalogEditor, EditPreview, human_value, price_example
from app.kb.overrides import OverrideError
from app.ops import menu

logger = logging.getLogger("parmangal.ops.menu")

ACCESS_DENIED = "Нет доступа."


@dataclass
class Reply:
    """Что бот должен показать. `edit` — правим предыдущее сообщение
    (обычная навигация по меню) или шлём новое (ответ на ввод значения)."""

    text: str
    markup: Any = None
    edit: bool = True
    alert: Optional[str] = None


@dataclass
class PendingEdit:
    token: str
    field_key: str
    zone_id: Optional[str]
    preview: Optional[EditPreview] = None


class MenuService:
    def __init__(self, editor: CatalogEditor, settings: Any, ops_service: Any,
                 stats_provider: Any = None, dialogs_provider: Any = None,
                 on_kb_reloaded: Any = None):
        self.editor = editor
        self.settings = settings
        self.ops = ops_service
        self.stats_provider = stats_provider
        self.dialogs_provider = dialogs_provider
        # Вызывается с новым KnowledgeBase после каждой успешной правки —
        # `app.state.kb`, `AgentLoop.kb` и `MessagePipeline.kb` держат
        # ссылку на снимок, а не читают БД сами, и без этого правка не
        # доехала бы до живого агента до рестарта процесса.
        self.on_kb_reloaded = on_kb_reloaded
        self.pending: dict[int, PendingEdit] = {}

    def _allowed(self, user_id: int) -> bool:
        return self.ops.is_allowed(user_id)

    # -- навигация ----------------------------------------------------------

    async def root(self, user_id: int) -> Reply:
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)
        self.pending.pop(user_id, None)
        return Reply(menu.main_menu_text(), menu.main_menu())

    async def open_section(self, user_id: int, section: str) -> Reply:
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)

        if section == "root":
            return await self.root(user_id)
        if section == "prices":
            kb = await self.editor.current_kb()
            return Reply("💰 Выберите зону:", menu.zones_menu(kb))
        if section == "schedule":
            kb = await self.editor.current_kb()
            return Reply(menu.schedule_card(kb), menu.schedule_menu())
        if section == "mode":
            return Reply(menu.mode_card(self.settings), menu.mode_menu(self.settings))
        if section == "stats":
            return Reply(await self._stats_text(), menu.main_menu())
        if section == "dialogs":
            return Reply(await self._dialogs_text(), menu.main_menu())
        return Reply("Неизвестный раздел.", menu.main_menu())

    async def open_zone(self, user_id: int, zone_id: str) -> Reply:
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)
        kb = await self.editor.current_kb()
        return Reply(menu.zone_card(kb, zone_id), menu.zone_fields_menu(kb, zone_id))

    async def _stats_text(self) -> str:
        if self.stats_provider is None:
            return "📊 Статистика пока недоступна."
        from app.ops.notifications import render_stats

        return render_stats(**await self.stats_provider())

    async def _dialogs_text(self) -> str:
        if self.dialogs_provider is None:
            return "📋 Источник диалогов не подключён."
        rows = await self.dialogs_provider()
        if not rows:
            return "📋 Диалогов пока нет."
        lines = ["📋 Последние диалоги", ""]
        for row in rows[:10]:
            mode = "оператор" if row.get("is_human_takeover") else "ИИ"
            lines.append(
                f"• {row.get('chat_id')} · {row.get('zone_id') or 'зона не определена'} "
                f"· {mode} · сообщений: {row.get('messages', 0)}"
            )
        return "\n".join(lines)

    # -- правка -------------------------------------------------------------

    async def start_edit(self, user_id: int, field_key: str, zone_id: Optional[str]) -> Reply:
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)
        try:
            field = field_by_key(field_key)
            current = await self.editor.current_value(field, zone_id)
        except OverrideError as exc:
            return Reply(f"⚠️ {exc}", menu.main_menu())

        token = secrets.token_urlsafe(6)
        self.pending[user_id] = PendingEdit(token=token, field_key=field_key, zone_id=zone_id)
        return Reply(menu.ask_value_card(field, human_value(current)), markup=None)

    async def receive_value(self, user_id: int, text: str) -> Optional[Reply]:
        """None означает «этот текст не для нас» — обычное сообщение в чате,
        а не ответ на запрос значения. Хендлер тогда не вмешивается."""
        pending = self.pending.get(user_id)
        if pending is None:
            return None
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)

        try:
            preview = await self.editor.preview(
                pending.field_key, text, user_id=user_id, zone_id=pending.zone_id
            )
        except OverrideError as exc:
            # Правка НЕ сохранена, состояние остаётся — оператор может
            # прислать исправленное значение, не начиная с меню заново.
            return Reply(f"⚠️ {exc}\n\nПришлите другое значение или вернитесь в меню.",
                         edit=False)

        pending.preview = preview
        kb_now = await self.editor.current_kb()
        before = price_example(kb_now, pending.zone_id)
        return Reply(menu.confirm_card(preview, before), menu.confirm_menu(pending.token), edit=False)

    async def confirm(self, user_id: int, token: str) -> Reply:
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)
        pending = self.pending.get(user_id)
        if pending is None or pending.preview is None or pending.token != token:
            # Токен разошёлся — скорее всего нажали «Сохранить» на старой
            # карточке. Сохранять по ней нельзя: она про другое значение.
            return Reply("Эта правка уже неактуальна. Начните заново.", menu.main_menu())

        try:
            result = await self.editor.apply(pending.preview, user_id=user_id)
        except OverrideError as exc:
            self.pending.pop(user_id, None)
            return Reply(f"⚠️ {exc}", menu.main_menu())

        await self.ops.store.log_action(
            "*", user_id, "catalog_edit",
            {"path": pending.preview.path, "from": pending.preview.previous_human,
             "to": pending.preview.new_human},
        )
        self.pending.pop(user_id, None)
        if self.on_kb_reloaded is not None:
            self.on_kb_reloaded(result.kb)
        return Reply(menu.saved_card(result, pending.preview), menu.saved_menu())

    async def cancel(self, user_id: int, token: str) -> Reply:
        self.pending.pop(user_id, None)
        return Reply("Правка отменена.", menu.main_menu())

    async def revert_last(self, user_id: int) -> Reply:
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)
        result = await self.editor.revert_last(user_id=user_id)
        if result is None:
            return Reply("Откатывать нечего — правок нет.", menu.main_menu())

        await self.ops.store.log_action(
            "*", user_id, "catalog_revert",
            {"override_id": result.record.id, "path": result.record.path},
        )
        if self.on_kb_reloaded is not None:
            self.on_kb_reloaded(result.kb)
        lines = [
            "↩️ Правка откачена",
            "",
            f"{result.record.path}",
            f"вернулось к: {human_value(result.record.previous_value)}",
        ]
        if result.price_example:
            lines += ["", "Как это выглядит для клиента:", result.price_example]
        return Reply("\n".join(lines), menu.main_menu())

    # -- режим работы -------------------------------------------------------

    async def set_moderation(self, user_id: int, mode: str) -> Reply:
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)
        message = await self.ops.set_moderation_mode(user_id, mode)
        return Reply(f"{message}\n\n{menu.mode_card(self.settings)}",
                     menu.mode_menu(self.settings))

    async def toggle(self, user_id: int, action: str) -> Reply:
        if not self._allowed(user_id):
            return Reply(ACCESS_DENIED, edit=False)
        if action == "dry_on":
            message = await self.ops.set_dry_run(user_id, True)
        elif action == "dry_off":
            message = await self.ops.set_dry_run(user_id, False)
        elif action == "pause":
            message = await self.ops.pause_all(user_id)
        elif action == "resume":
            message = await self.ops.resume_all(user_id)
        else:
            return Reply("Неизвестное действие.", menu.mode_menu(self.settings))
        return Reply(f"{message}\n\n{menu.mode_card(self.settings)}",
                     menu.mode_menu(self.settings))
