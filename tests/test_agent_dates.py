"""app/agent/dates.py — разбор дат из речи клиента, без доверия модели.

Повод: агент сам досчитал «29 августа» до прошлого года, отправил
book_times на 2025-08-29, YCLIENTS ответил 422, занятость стала UNKNOWN,
диалог ушёл в эскалацию. Каждый тест здесь фиксирует `today`, а не полагается
на реальные часы машины — иначе тест сам однажды получит ту же болезнь,
которую здесь лечим.
"""

from __future__ import annotations

from datetime import date

from app.agent.dates import resolve_relative_date

TODAY = date(2026, 8, 27)   # конец августа 2026 — тот же живой случай


def test_day_and_month_without_year_picks_the_nearest_future_date():
    """Буквально пример из задачи: «29 августа» в конце августа 2026 —
    это 2026-08-29, месяц ещё не прошёл."""
    resolution = resolve_relative_date("29 августа", today=TODAY)
    assert resolution.date == date(2026, 8, 29)
    assert resolution.year_inferred is True


def test_a_month_that_already_passed_rolls_over_to_next_year():
    """Второй пример из задачи: «15 января», сказанное в августе 2026 —
    январь этого года уже прошёл, значит 2027-01-15."""
    resolution = resolve_relative_date("15 января", today=TODAY)
    assert resolution.date == date(2027, 1, 15)
    assert resolution.year_inferred is True


def test_todays_own_date_is_not_considered_passed():
    """29 августа, названное РОВНО 29 августа — тот же год, не следующий:
    сегодняшняя дата не считается прошедшей."""
    resolution = resolve_relative_date("29 августа", today=date(2026, 8, 29))
    assert resolution.date == date(2026, 8, 29)


def test_relative_words():
    assert resolve_relative_date("сегодня", today=TODAY).date == TODAY
    assert resolve_relative_date("завтра", today=TODAY).date == date(2026, 8, 28)
    assert resolve_relative_date("послезавтра", today=TODAY).date == date(2026, 8, 29)


def test_explicit_year_is_kept_as_is_even_in_the_past():
    """Клиент сам назвал год — не наше дело его домысливать. Проверка «дата
    в прошлом» — забота вызывающего инструмента (resolve_date/
    check_availability), не этой функции."""
    resolution = resolve_relative_date("29 августа 2025", today=TODAY)
    assert resolution.date == date(2025, 8, 29)
    assert resolution.year_inferred is False


def test_explicit_future_year_is_kept_as_is():
    resolution = resolve_relative_date("29 августа 2027", today=TODAY)
    assert resolution.date == date(2027, 8, 29)


def test_numeric_day_dot_month_without_year():
    resolution = resolve_relative_date("29.08", today=TODAY)
    assert resolution.date == date(2026, 8, 29)
    assert resolution.year_inferred is True


def test_numeric_day_dot_month_dot_year():
    resolution = resolve_relative_date("29.08.2025", today=TODAY)
    assert resolution.date == date(2025, 8, 29)


def test_iso_date_passes_through_unchanged():
    """Модель могла и сама прислать корректный ISO — не ошибка, обычный путь."""
    resolution = resolve_relative_date("2026-08-29", today=TODAY)
    assert resolution.date == date(2026, 8, 29)
    assert resolution.year_inferred is False


def test_unrecognized_text_returns_none():
    assert resolve_relative_date("как-нибудь на днях", today=TODAY) is None


def test_impossible_calendar_date_returns_none_rather_than_guessing():
    """31 февраля — не гадаем, какое число имелось в виду на самом деле."""
    assert resolve_relative_date("31 февраля", today=TODAY) is None


def test_finds_the_date_phrase_inside_a_longer_sentence():
    """Устойчивость на случай, если модель передаст не голую фразу, а
    кусок предложения клиента — токен инструмента описывает "дословно",
    но код не должен падать, если получил чуть больше контекста."""
    resolution = resolve_relative_date("хотим приехать 29 августа на бани", today=TODAY)
    assert resolution.date == date(2026, 8, 29)
