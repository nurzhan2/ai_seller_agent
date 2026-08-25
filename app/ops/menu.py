"""Инлайн-меню бота: клавиатуры и тексты.

Чистые функции без aiogram-контекста и без сети — как и
`app/ops/notifications.py`, и по той же причине: то, что оператор увидит,
важнее механики кнопок, и проверяться должно построчно.

ПРО callback_data. Telegram ограничивает её 64 БАЙТАМИ, и это не
рекомендация — превышение отваливается ошибкой при отправке клавиатуры,
причём уже в проде. Поэтому префиксы короткие (`m:` вместо `menu:`), ключи
полей короткие (`we_hour`, см. app/kb/editable.py), а самое длинное, что
сюда попадает, — id зоны (`dome_blue_chairs`, 16 символов). Формат
проверяется тестом, а не глазами.
"""

from __future__ import annotations

from typing import Any, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.kb.editable import SCHEDULE_FIELDS, ZONE_FIELDS, EditableField
from app.kb.editor import human_value

CB_MAX_BYTES = 64

# Префиксы callback_data. Один символ + двоеточие — см. докстринг.
CB_MENU = "m"          # m:root | m:prices | m:schedule | m:stats | m:mode | m:dialogs
CB_ZONE = "z"          # z:<zone_id>
CB_EDIT = "e"          # e:<field_key>:<zone_id?>
CB_CONFIRM = "ok"      # ok:<token>
CB_CANCEL = "no"       # no:<token>
CB_MODE = "md"         # md:<all|concessions_only|off>
CB_TOGGLE = "tg"       # tg:<dry_on|dry_off|pause|resume>
CB_REVERT = "rv"       # rv:last


def _cb(*parts: str) -> str:
    data = ":".join(parts)
    if len(data.encode("utf-8")) > CB_MAX_BYTES:
        # Ловим на сборке, а не при отправке в Telegram: там это выглядело бы
        # как «кнопка не работает» без объяснений.
        raise ValueError(f"callback_data длиннее {CB_MAX_BYTES} байт: {data!r}")
    return data


# --------------------------------------------------------------------------
# Главное меню
# --------------------------------------------------------------------------

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Цены и услуги", callback_data=_cb(CB_MENU, "prices"))],
        [InlineKeyboardButton(text="🕐 График работы", callback_data=_cb(CB_MENU, "schedule"))],
        [InlineKeyboardButton(text="📊 Статистика", callback_data=_cb(CB_MENU, "stats"))],
        [InlineKeyboardButton(text="⚙️ Режим работы", callback_data=_cb(CB_MENU, "mode"))],
        [InlineKeyboardButton(text="📋 Диалоги", callback_data=_cb(CB_MENU, "dialogs"))],
    ])


def main_menu_text() -> str:
    return "Что делаем?"


def _back(to: str = "root") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=_cb(CB_MENU, to))


# --------------------------------------------------------------------------
# Цены: зоны -> поля зоны
# --------------------------------------------------------------------------

def zones_menu(kb: Any) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=zone.name, callback_data=_cb(CB_ZONE, zone.id))]
        for zone in kb.catalog.zones
    ]
    rows.append([_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fields_for_zone(kb: Any, zone_id: str) -> list[EditableField]:
    """Поля, осмысленные для ЭТОЙ зоны. Показывать «₽/сутки» у почасовой
    бани — значит предлагать правку, которая никуда не применится:
    такого ключа в её `pricing` просто нет, и правка упала бы на «нет пути»
    уже после ввода значения."""
    zone = next((z for z in kb.catalog.zones if z.id == zone_id), None)
    if zone is None:
        return []
    category = zone.category.value
    result = []
    for field in ZONE_FIELDS:
        if field.categories is not None and category not in field.categories:
            continue
        # Поле есть в документе? day_package есть не у всех зон.
        if field.key == "pkg" and not zone.day_package:
            continue
        result.append(field)
    return result


def zone_card(kb: Any, zone_id: str) -> str:
    zone = next((z for z in kb.catalog.zones if z.id == zone_id), None)
    if zone is None:
        return "Зона не найдена."

    lines = [f"💰 {zone.name}", ""]
    for field in fields_for_zone(kb, zone_id):
        raw_node = _zone_field_node(zone, field)
        lines.append(f"{field.label}: {human_value(raw_node)}")
    lines.append("")
    lines.append("Что меняем?")
    return "\n".join(lines)


def _zone_field_node(zone: Any, field: EditableField) -> Any:
    """Текущее значение поля прямо из модели зоны — без похода в сырой
    документ: карточка рисуется из того же KnowledgeBase, который уже
    собран с наложенными правками."""
    if field.key == "cap":
        return {"value": zone.capacity.value} if zone.capacity.is_resolved() else {"disputed": True}
    if field.key == "pkg":
        return (zone.day_package or {}).get("price")
    mapping = {
        "wd_hour": "weekday_per_hour", "we_hour": "weekend_per_hour",
        "wd_day": "weekday_per_day", "we_day": "weekend_per_day",
        "per_day": "per_day", "min_h": "min_hours",
    }
    return zone.pricing.get(mapping[field.key])


def zone_fields_menu(kb: Any, zone_id: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f.label, callback_data=_cb(CB_EDIT, f.key, zone_id))]
        for f in fields_for_zone(kb, zone_id)
    ]
    rows.append([_back("prices")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------
# График работы
# --------------------------------------------------------------------------

def schedule_card(kb: Any) -> str:
    window = kb.catalog.constants.working_window
    holidays = kb.catalog.constants.holidays
    return "\n".join([
        "🕐 График работы",
        "",
        f"Рабочее окно: {window.from_} — {window.to}",
        "(за его пределами отложенные напоминания клиентам не уходят)",
        "",
        f"Праздничные даты ({len(holidays.dates)}): {', '.join(holidays.dates) or 'нет'}",
        "",
        "Что меняем?",
    ])


def schedule_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f.label, callback_data=_cb(CB_EDIT, f.key))]
        for f in SCHEDULE_FIELDS
    ]
    rows.append([_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------
# Режим работы
# --------------------------------------------------------------------------

def mode_card(settings: Any) -> str:
    moderation = {
        "all": "всё на одобрении",
        "concessions_only": "только ценовые уступки",
        "off": "полная автономия",
    }.get(settings.moderation_mode, settings.moderation_mode)
    return "\n".join([
        "⚙️ Режим работы",
        "",
        f"Модерация: {settings.moderation_mode} — {moderation}",
        f"DRY_RUN: {'включён — клиентам ничего не уходит' if settings.dry_run else 'выключен — агент пишет клиентам'}",
        f"Агент: {'НА ПАУЗЕ' if getattr(settings, 'agent_paused', False) else 'работает'}",
    ])


def mode_menu(settings: Any) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=("✅ " if settings.moderation_mode == mode else "") + label,
        callback_data=_cb(CB_MODE, mode),
    )] for mode, label in (
        ("all", "Модерация: всё"),
        ("concessions_only", "Модерация: только скидки"),
        ("off", "Модерация: выключена"),
    )]
    rows.append([InlineKeyboardButton(
        text="🔓 Выключить DRY_RUN" if settings.dry_run else "🔒 Включить DRY_RUN",
        callback_data=_cb(CB_TOGGLE, "dry_off" if settings.dry_run else "dry_on"),
    )])
    paused = bool(getattr(settings, "agent_paused", False))
    rows.append([InlineKeyboardButton(
        text="▶️ Снять с паузы" if paused else "⏸ Поставить на паузу",
        callback_data=_cb(CB_TOGGLE, "resume" if paused else "pause"),
    )])
    rows.append([_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------
# Подтверждение правки
# --------------------------------------------------------------------------

def confirm_card(preview: Any, price_before: Optional[str]) -> str:
    zone_part = f" · {preview.zone_id}" if preview.zone_id else ""
    lines = [
        f"Проверьте правку{zone_part}",
        "",
        f"{preview.field.label}",
        f"было:   {preview.previous_human}",
        f"станет: {preview.new_human}",
    ]
    if price_before:
        lines += ["", f"Сейчас: {price_before}"]
    lines += ["", "Сохранить?"]
    return "\n".join(lines)


def confirm_menu(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data=_cb(CB_CONFIRM, token))],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=_cb(CB_CANCEL, token))],
    ])


def saved_card(result: Any, preview: Any) -> str:
    lines = [
        "✅ Сохранено",
        "",
        f"{preview.field.label}: {preview.previous_human} → {preview.new_human}",
    ]
    if result.price_example:
        lines += ["", "Как это выглядит для клиента:", result.price_example]
    lines += ["", "Если это ошибка — «Откатить последнюю правку» ниже."]
    return "\n".join(lines)


def saved_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Откатить последнюю правку", callback_data=_cb(CB_REVERT, "last"))],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data=_cb(CB_MENU, "root"))],
    ])


def ask_value_card(field: EditableField, current_human: str) -> str:
    return "\n".join([
        f"{field.label}",
        "",
        f"Сейчас: {current_human}",
        "",
        f"Пришлите новое значение сообщением.\n{field.hint}",
    ])
