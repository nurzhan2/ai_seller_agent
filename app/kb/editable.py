"""Что оператору РАЗРЕШЕНО править из Telegram — и как это проверяется.

Единый источник правды: отсюда строится и меню бота, и валидация ввода, и
подпись в журнале. Разложить это по трём местам означало бы, что рано или
поздно кнопка появится там, где проверки нет.

Белый список, а не «правь любой путь». Причины две. Во-первых, произвольный
путь позволил бы оператору с телефона снести `open_questions` или подменить
`mode: hourly` на `daily` — то есть сломать каталог способом, которого
доменная валидация даже не ожидает. Во-вторых, у каждого поля свой
осмысленный диапазон (цена — не то же самое, что часы), и проверять их
одинаково нельзя.

ПРО ДОМЕННЫЕ ПРОВЕРКИ. Схема (pydantic + `validate_no_orphan_disputed`)
ловит структуру, но не смысл: `weekend_per_hour: -500` и
`working_window.from: "25:00"` пройдут её насквозь. Второе при этом уронит
`is_within_working_hours` в рантайме — `time(25, 0)` бросает ValueError, —
то есть невалидное время суток остановило бы воркер касаний, а не
отклонилось бы на вводе. Поэтому проверки здесь, ДО сохранения.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.kb.overrides import OverrideError

# Верхняя граница цены. Не бизнес-правило, а защита от опечатки: лишний
# ноль в 15 000 даёт 150 000, и агент назовёт эту цену живому клиенту.
MAX_PRICE_RUB = 1_000_000
MAX_HOURS = 24
MAX_CAPACITY = 500

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_MMDD_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def _parse_money(text: str) -> int:
    cleaned = text.replace(" ", "").replace(" ", "").replace("₽", "").replace("руб", "")
    try:
        value = int(cleaned)
    except ValueError:
        raise OverrideError(
            f"«{text}» — не похоже на цену. Нужно целое число рублей, например: 3500"
        ) from None
    if value < 0:
        raise OverrideError("Цена не может быть отрицательной.")
    if value > MAX_PRICE_RUB:
        raise OverrideError(
            f"Цена {value} ₽ выглядит как опечатка (максимум {MAX_PRICE_RUB} ₽). "
            "Проверьте количество нулей."
        )
    return value


def _parse_hours(text: str) -> int:
    try:
        value = int(text.strip())
    except ValueError:
        raise OverrideError(
            f"«{text}» — не похоже на число часов. Нужно целое число, например: 3"
        ) from None
    if value < 1:
        raise OverrideError("Минимум часов не может быть меньше 1.")
    if value > MAX_HOURS:
        raise OverrideError(f"Минимум часов не может быть больше {MAX_HOURS} — это сутки.")
    return value


def _parse_capacity(text: str) -> int:
    try:
        value = int(text.strip())
    except ValueError:
        raise OverrideError(
            f"«{text}» — не похоже на число гостей. Нужно целое число, например: 12"
        ) from None
    if value < 1:
        raise OverrideError("Вместимость не может быть меньше 1 гостя.")
    if value > MAX_CAPACITY:
        raise OverrideError(f"Вместимость больше {MAX_CAPACITY} выглядит как опечатка.")
    return value


def _parse_time(text: str) -> str:
    value = text.strip()
    if not _TIME_RE.match(value):
        raise OverrideError(
            f"«{text}» — не похоже на время. Нужен формат ЧЧ:ММ от 00:00 до 23:59, например: 09:00"
        )
    return value


def _parse_dates(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]
    if not parts:
        raise OverrideError("Список дат пустой. Нужны даты в формате ММ-ДД через запятую.")
    for part in parts:
        if not _MMDD_RE.match(part):
            raise OverrideError(
                f"«{part}» — не похоже на дату. Нужен формат ММ-ДД, например: 01-01, 05-09"
            )
    # Дубликаты убираем молча: они безвредны (`Holidays.contains` работает
    # через set), но в журнале выглядели бы как ошибка оператора.
    return sorted(dict.fromkeys(parts))


@dataclass(frozen=True)
class EditableField:
    """`key` короткий: он едет в callback_data кнопки, а там лимит 64 байта
    на всю строку вместе с зоной и префиксом."""

    key: str
    label: str
    kind: str                       # money | hours | capacity | time | dates
    parse: Callable[[str], Any]
    hint: str
    # Шаблон пути; {zone} подставляется для зональных полей.
    path_template: str
    # True — узел является DisputedValue-листом и заменяется целиком
    # (`{"value": ..., "resolved_from": ...}`). False — простое скалярное
    # значение прямо по пути (например, working_window.from).
    is_disputed_leaf: bool = True
    # Ограничить поле категориями зон; None — доступно всем.
    categories: Optional[tuple[str, ...]] = None


_HOURLY = ("bath", "dome", "grill", "tent")
_DAILY = ("house", "yurt")

ZONE_FIELDS: tuple[EditableField, ...] = (
    EditableField(
        key="wd_hour", label="Будни, ₽/час", kind="money", parse=_parse_money,
        hint="Цена за час в будний день, например: 2500",
        path_template="$.catalog.zones[id={zone}].pricing.weekday_per_hour",
        categories=_HOURLY,
    ),
    EditableField(
        key="we_hour", label="Выходные, ₽/час", kind="money", parse=_parse_money,
        hint="Цена за час в выходной, например: 3500",
        path_template="$.catalog.zones[id={zone}].pricing.weekend_per_hour",
        categories=_HOURLY,
    ),
    EditableField(
        key="wd_day", label="Будни, ₽/сутки", kind="money", parse=_parse_money,
        hint="Цена за сутки в будни, например: 7000",
        path_template="$.catalog.zones[id={zone}].pricing.weekday_per_day",
        categories=("house",),
    ),
    EditableField(
        key="we_day", label="Выходные, ₽/сутки", kind="money", parse=_parse_money,
        hint="Цена за сутки в выходной, например: 15000",
        path_template="$.catalog.zones[id={zone}].pricing.weekend_per_day",
        categories=("house",),
    ),
    EditableField(
        key="per_day", label="₽/сутки", kind="money", parse=_parse_money,
        hint="Цена за сутки, например: 4000",
        path_template="$.catalog.zones[id={zone}].pricing.per_day",
        categories=("yurt",),
    ),
    EditableField(
        key="min_h", label="Минимум часов", kind="hours", parse=_parse_hours,
        hint="Минимальное время аренды в часах, например: 3",
        path_template="$.catalog.zones[id={zone}].pricing.min_hours",
        categories=_HOURLY,
    ),
    EditableField(
        key="cap", label="Вместимость, гостей", kind="capacity", parse=_parse_capacity,
        hint="Сколько гостей помещается, например: 12",
        path_template="$.catalog.zones[id={zone}].capacity",
    ),
    EditableField(
        key="pkg", label="Пакет на день, ₽", kind="money", parse=_parse_money,
        hint="Цена пакета на весь день, например: 5000",
        path_template="$.catalog.zones[id={zone}].day_package.price",
    ),
)

SCHEDULE_FIELDS: tuple[EditableField, ...] = (
    EditableField(
        key="work_from", label="Начало рабочего дня", kind="time", parse=_parse_time,
        hint="Во сколько открываемся, формат ЧЧ:ММ, например: 09:00",
        path_template="$.catalog.constants.working_window.from",
        is_disputed_leaf=False,
    ),
    EditableField(
        key="work_to", label="Конец рабочего дня", kind="time", parse=_parse_time,
        hint="Во сколько закрываемся, формат ЧЧ:ММ, например: 23:00",
        path_template="$.catalog.constants.working_window.to",
        is_disputed_leaf=False,
    ),
    EditableField(
        key="holidays", label="Праздничные даты", kind="dates", parse=_parse_dates,
        hint="Даты через запятую в формате ММ-ДД, например: 01-01, 02-23, 03-08",
        path_template="$.catalog.constants.holidays.dates",
        is_disputed_leaf=False,
    ),
)

ALL_FIELDS: dict[str, EditableField] = {
    f.key: f for f in (*ZONE_FIELDS, *SCHEDULE_FIELDS)
}


def field_by_key(key: str) -> EditableField:
    field = ALL_FIELDS.get(key)
    if field is None:
        raise OverrideError(f"Неизвестное поле {key!r}.")
    return field


def resolved_from_marker(user_id: int) -> str:
    """Пометка происхождения значения (пункт 7 задачи). Кладётся в
    существующее поле `resolved_from` DisputedValue, а не в новое
    `resolved_by`: смысл тот же — «откуда взялось это значение», — а лишнее
    поле потребовало бы менять схему, которую `extra="forbid"` защищает."""
    return f"оператор через Telegram (user_id={user_id})"


def build_override_value(field: EditableField, parsed: Any, user_id: int) -> Any:
    """Что именно ляжет в документ по пути поля.

    Для DisputedValue-листа — новый лист целиком, с пометкой происхождения и
    БЕЗ блока `disputed`: правка спорного поля закрывает спор, ради этого
    пункт 7 и существует. Ключ `provisional` (он есть, например, у
    `house_relax.weekend_per_day`) тоже не переносится — значение,
    подтверждённое владельцем через бота, больше не наше предварительное.
    """
    if not field.is_disputed_leaf:
        return parsed
    return {"value": parsed, "resolved_from": resolved_from_marker(user_id)}
