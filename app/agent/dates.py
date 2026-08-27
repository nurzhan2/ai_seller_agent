"""Разбор дат, названных клиентом словами — в коде, не доверяя модели.

Живой баг, ради которого этот модуль появился: клиент написал «29 августа»,
модель сама досчитала это до 2025-08-29 (прошлый год), YCLIENTS ответил 422
на book_times, занятость стала UNKNOWN, диалог ушёл в эскалацию. Держать в
голове текущий год и переносить дату на следующий, если названный месяц уже
прошёл, — ровно тот класс арифметики, который надёжнее делать в коде, а не
надеяться, что модель посчитает верно каждый раз.

Если год не назван явно — берётся БЛИЖАЙШАЯ БУДУЩАЯ дата: «29 августа» в
конце августа 2026 года — это 2026-08-29 (месяц ещё не прошёл), а «15
января», сказанное в августе 2026 — это 2027-01-15 (январь этого года уже
позади). Сегодняшняя дата не считается прошедшей.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as DateType, timedelta
from typing import Optional

_MONTHS_GENITIVE = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# Порядок alternation значения не имеет: \b не даёт "завтра" случайно
# совпасть внутри "послезавтра" — между "после" и "завтра" нет границы
# слова (обе стороны — словесные символы для \w с юникодом).
_RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_RELATIVE_RE = re.compile(
    r"\b(" + "|".join(_RELATIVE_DAYS) + r")\b", re.IGNORECASE
)

# «29 августа», «29 августа 2027» — регистр не важен, год необязателен.
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS_GENITIVE) + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)

# «29.08», «29.08.2027», «29/08/2027» — числовой формат, день первым
# (обычная русская запись, не американская MM/DD).
_NUMERIC_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\b")


@dataclass(frozen=True)
class DateResolution:
    date: DateType
    # Не идёт в ответ инструмента как есть — пригождается в логах/тестах,
    # чтобы отличить «клиент сам назвал год» от «мы его домыслили».
    year_inferred: bool


def _nearest_future(day: int, month: int, today: DateType) -> Optional[DateType]:
    try:
        candidate = DateType(today.year, month, day)
    except ValueError:
        return None   # 31 февраля и т.п. — не гадаем, какое число имелось в виду
    if candidate < today:
        try:
            candidate = candidate.replace(year=today.year + 1)
        except ValueError:
            return None   # 29 февраля, следующий год невисокосный
    return candidate


def resolve_relative_date(text: str, today: Optional[DateType] = None) -> Optional[DateResolution]:
    """Первая распознанная дата во фразе, или None, если не распозналась.

    Порядок попыток: готовый ISO (модель могла передать корректную дату
    сама — это не ошибка, а нормальный путь), относительные слова
    (сегодня/завтра/послезавтра), «29 августа [год]», «29.08[.год]». Первое,
    что нашлось, и возвращается — не пытаемся выбирать между несколькими
    датами в одной фразе, это решение не для эвристики.
    """
    today = today or DateType.today()
    stripped = text.strip()

    try:
        return DateResolution(DateType.fromisoformat(stripped), year_inferred=False)
    except ValueError:
        pass

    relative = _RELATIVE_RE.search(stripped)
    if relative:
        offset = _RELATIVE_DAYS[relative.group(1).lower()]
        return DateResolution(today + timedelta(days=offset), year_inferred=False)

    day_month = _DAY_MONTH_RE.search(stripped)
    if day_month:
        day = int(day_month.group(1))
        month = _MONTHS_GENITIVE[day_month.group(2).lower()]
        year_str = day_month.group(3)
        if year_str:
            try:
                return DateResolution(DateType(int(year_str), month, day), year_inferred=False)
            except ValueError:
                return None
        resolved = _nearest_future(day, month, today)
        return DateResolution(resolved, year_inferred=True) if resolved else None

    numeric = _NUMERIC_RE.search(stripped)
    if numeric:
        day, month = int(numeric.group(1)), int(numeric.group(2))
        year_str = numeric.group(3)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        if year_str:
            try:
                return DateResolution(DateType(int(year_str), month, day), year_inferred=False)
            except ValueError:
                return None
        resolved = _nearest_future(day, month, today)
        return DateResolution(resolved, year_inferred=True) if resolved else None

    return None
