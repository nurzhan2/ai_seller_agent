"""Tests for app/pricing/engine.py.

Dates used throughout (verified, 2026):
    2026-07-13 Mon | 2026-07-16 Thu | 2026-07-17 Fri
    2026-07-18 Sat | 2026-07-19 Sun
    2026-01-05 provisional holiday | 2026-01-09 the day after the block
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, time
from decimal import Decimal

import pytest

from app.kb.loader import load_catalog
from app.pricing import engine as engine_module
from app.pricing.engine import PriceRequest, money, quote

MON = date(2026, 7, 13)
THU = date(2026, 7, 16)
FRI = date(2026, 7, 17)
SAT = date(2026, 7, 18)
SUN = date(2026, 7, 19)
HOLIDAY = date(2026, 1, 5)
AFTER_HOLIDAY = date(2026, 1, 9)
NOON = time(12, 0)


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


def q(kb, **kwargs):
    return quote(PriceRequest(**kwargs), kb)


# ==========================================================================
# 1. Every zone × weekday/weekend × minimum hours
# ==========================================================================

@pytest.mark.parametrize("zone_id,expected", [("bath_russian", 7500), ("bath_garage", 7500)])
def test_bath_weekday_min_hours(kb, zone_id, expected):
    r = q(kb, zone_id=zone_id, date=THU, start_time=NOON, hours=3, guests=6)
    assert r.status == "ok"
    assert r.total == money(expected)
    assert r.day_type == "weekday"


@pytest.mark.parametrize("zone_id,expected", [("bath_russian", 10500), ("bath_garage", 10500)])
def test_bath_weekend_min_hours(kb, zone_id, expected):
    r = q(kb, zone_id=zone_id, date=SAT, start_time=NOON, hours=3, guests=6)
    assert r.status == "ok"
    assert r.total == money(expected)
    assert r.day_type == "weekend"


def test_bath_friday_is_weekend(kb):
    r = q(kb, zone_id="bath_russian", date=FRI, start_time=NOON, hours=3, guests=4)
    assert r.status == "ok"
    assert r.day_type == "weekend"
    assert r.total == money(10500)


def test_bath_knight_weekday_under_special_threshold_prices_normally(kb):
    """4 hours < the 5-hour special threshold, so the plain rate applies."""
    r = q(kb, zone_id="bath_knight", date=THU, start_time=NOON, hours=4, guests=4)
    assert r.status == "ok"
    assert r.total == money(10000)


def test_bath_knight_weekend_ignores_weekday_special(kb):
    r = q(kb, zone_id="bath_knight", date=SAT, start_time=NOON, hours=5, guests=4)
    assert r.status == "ok"
    assert r.total == money(17500)


@pytest.mark.parametrize("zone_id", ["dome_bags", "dome_blue_chairs", "dome_chairs"])
def test_dome_weekday_and_weekend(kb, zone_id):
    # 6 гостей влезают в любой купол: у мешков 10 мест, у кресел и стульев по 7.
    weekday = q(kb, zone_id=zone_id, date=THU, start_time=NOON, hours=3, guests=6)
    weekend = q(kb, zone_id=zone_id, date=SAT, start_time=NOON, hours=3, guests=6)
    assert weekday.total == money(3000)
    assert weekend.total == money(4500)


def test_dome_sunday_uses_weekend_rate(kb):
    """The manager template said «Пт-Сб»; the price list says Пт-Вс and wins."""
    r = q(kb, zone_id="dome_bags", date=SUN, start_time=NOON, hours=3, guests=6)
    assert r.status == "ok"
    assert r.total == money(4500)


def test_grill_house_weekday_and_weekend(kb):
    weekday = q(kb, zone_id="grill_house", date=THU, start_time=NOON, hours=3, guests=8)
    weekend = q(kb, zone_id="grill_house", date=SAT, start_time=NOON, hours=3, guests=8)
    assert weekday.total == money(4500)
    assert weekend.total == money(6000)


def test_house_relax_weekday_daily(kb):
    r = q(kb, zone_id="house_relax", date=THU, hours=None, guests=8)
    assert r.status == "ok"
    assert r.total == money(7000)


def test_yurt_daily_ignores_hours(kb):
    """hours is meaningless for a daily zone and must not raise."""
    with_hours = q(kb, zone_id="yurt", date=SAT, hours=5, guests=2)
    without = q(kb, zone_id="yurt", date=SAT, hours=None, guests=2)
    assert with_hours.status == "ok" and without.status == "ok"
    assert with_hours.total == without.total == money(4000)


def test_yurt_checkin_is_confirmed(kb):
    """6.1: заезд в 14:00 — вопрос закрыт, предупреждать больше не о чем."""
    r = q(kb, zone_id="yurt", date=SAT, hours=None, guests=2)
    assert r.status == "ok"
    assert "6.1" not in r.advisory_question_ids
    assert r.blocking_question_ids == ()


# ==========================================================================
# 2. Disputed fields must block, never guess
# ==========================================================================

def test_house_relax_weekend_now_prices_at_the_provisional_anchor(kb):
    """14.1: закрыт нашим провизорным решением — анкор 15000 ₽ (прямое
    указание владельца, свежее прайса с 14500). Пол уступки 9500 остаётся
    в concessions.yaml и здесь не меняется."""
    r = q(kb, zone_id="house_relax", date=SAT, hours=None, guests=8)
    assert r.status == "ok"
    assert r.total == money(15000)
    assert r.prepayment == money(3000)      # 14.4: фикс для суточных зон


def test_house_relax_friday_is_weekend_like_every_zone(kb):
    """2.3: пятница — выходной для ВСЕХ зон. Отдельной зонозависимости
    day_type, которая была заведена ради домика, больше нет."""
    r = q(kb, zone_id="house_relax", date=FRI, hours=None, guests=8)
    assert r.day_type == "weekend"
    assert r.status == "ok"
    assert r.total == money(15000)


def test_house_relax_bath_heating_is_priced(kb):
    """2.4: доп. топка 1500 ₽/час — цифра подтверждена."""
    r = q(kb, zone_id="house_relax", date=THU, hours=None, guests=8,
          extras=(("bath_heating", 3),))
    assert r.status == "ok"
    assert r.total == money(7000 + 4500)


def test_bath_knight_five_weekday_hours_price_normally(kb):
    """1.4/1.5: спеццена «Рыцарской» отменена заказчиком. Пять часов в будни
    считаются по обычному тарифу, самый сложный краевой случай снят."""
    r = q(kb, zone_id="bath_knight", date=THU, start_time=time(10, 0), hours=5, guests=4)
    assert r.status == "ok"
    assert r.total == money(12500)
    assert r.blocking_question_ids == ()


def test_tent_weekday_hourly_same_as_weekend(kb):
    """5.1: у шатра нет разницы между буднями и выходными."""
    weekday = q(kb, zone_id="tent", date=THU, start_time=NOON, hours=3, guests=15)
    weekend = q(kb, zone_id="tent", date=SAT, start_time=NOON, hours=3, guests=15)
    assert weekday.status == "ok"
    assert weekday.total == weekend.total == money(7500)


def test_grill_house_min_hours_is_advisory_not_blocking(kb):
    """Decision from prompt 3.1: the arithmetic of 2 hours is the same
    whether the minimum is 2 or 3. What is disputed is the RIGHT to sell."""
    r = q(kb, zone_id="grill_house", date=SAT, start_time=NOON, hours=2, guests=6)
    assert r.status == "ok"
    assert r.total == money(4000)
    # 4.2 закрыт: базовый минимум 3 часа, две часа — только через уступку.
    assert r.requires_concession_tier == 5
    assert r.blocking_question_ids == ()


@pytest.mark.parametrize("extra_id", ["pet_fee", "guest_table", "extra_tent_nearby"])
def test_yurt_extras_are_not_priced_at_all(kb, extra_id):
    """6.2-6.5: заказчик ответил «переводим на админа». Цена 1000 ₽ за
    животных удалена из базы полностью — она не должна фигурировать нигде.
    Эти темы обрабатывает агент эскалацией, а не движок цен."""
    r = q(kb, zone_id="yurt", date=SAT, hours=None, guests=2, extras=((extra_id, 1),))
    assert r.status == "invalid"
    assert r.total is None


def test_pet_price_is_absent_from_the_knowledge_base():
    from app.kb.loader import KB_DIR

    catalog = (KB_DIR / "catalog.yaml").read_text(encoding="utf-8")
    assert "pet_fee" not in catalog
    assert "1000 ₽/час" not in catalog


def test_blocked_quote_never_carries_a_total(kb):
    for req in [
        PriceRequest("bath_russian", SAT, time(21, 0), 4, 6),   # past 23:00 -> escalate
        PriceRequest("dome_bags", SAT, time(20, 0), 5, 6),      # past 23:00 -> escalate
    ]:
        r = quote(req, kb)
        assert r.status == "blocked"
        assert r.total is None


# ==========================================================================
# 3. Tent guest tiers: 19 / 20 / 21 / 31
# ==========================================================================

def test_tent_19_guests_lower_tier(kb):
    r = q(kb, zone_id="tent", date=SAT, start_time=NOON, hours=3, guests=19)
    assert r.status == "ok"
    assert r.total == money(7500)


def test_tent_exactly_20_guests_uses_lower_tier(kb):
    """5.2: граница «до 20» включительно — ровно 20 гостей идут по 2500."""
    r = q(kb, zone_id="tent", date=SAT, start_time=NOON, hours=3, guests=20)
    assert r.status == "ok"
    assert r.total == money(7500)


def test_tent_21_guests_upper_tier(kb):
    r = q(kb, zone_id="tent", date=SAT, start_time=NOON, hours=3, guests=21)
    assert r.status == "ok"
    assert r.total == money(12000)


def test_tent_31_guests_exceeds_capacity(kb):
    r = q(kb, zone_id="tent", date=SAT, start_time=NOON, hours=3, guests=31)
    assert r.status == "invalid"
    assert r.suggested_alternatives == ()


# ==========================================================================
# 4. Promo: 5 vs 6 vs 11 vs 12 hours
# ==========================================================================

def test_dome_five_hours_no_free_hour(kb):
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(10, 0), hours=5, guests=8)
    assert r.status == "ok"
    assert r.applied_promo is None
    assert r.total == money(7500)


def test_dome_six_hours_gets_free_hour(kb):
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(10, 0), hours=6, guests=8)
    assert r.status == "ok"
    assert r.applied_promo == "sixth_hour_free"
    assert r.total == money(7500)          # 5 paid hours
    assert r.billable_hours == 5
    assert r.occupied_hours == 6


def test_dome_eleven_hours_both_readings_agree(kb):
    """11h: repeatable and non-repeatable both yield exactly 1 free hour, so
    the unresolved question 7.1 does not bite and the quote goes through."""
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(10, 0), hours=11, guests=8)
    assert r.status == "ok"
    assert r.billable_hours == 10


def test_dome_twelve_hours_still_gets_only_one_free_hour(kb):
    """7.1: подарочный час даётся ОДИН раз и не повторяется."""
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(10, 0), hours=12, guests=8)
    assert r.status == "ok"
    assert r.billable_hours == 11
    assert r.occupied_hours == 12


def test_min_hours_checked_against_occupied_not_billable(kb):
    """Prompt 3.1: the minimum guards slot OCCUPANCY, not money. 6 occupied
    hours (5 paid) must not trip the 3-hour minimum."""
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(10, 0), hours=6, guests=8)
    assert r.occupied_hours == 6
    assert r.billable_hours == 5
    assert r.requires_concession_tier is None


# ==========================================================================
# 5. Promo interaction and disputed zones
# ==========================================================================

def test_two_promos_intersect_best_one_wins(kb):
    """6 hours in a dome on a birthday: free-hour (7500) beats 10% off
    (8100). The loser is reported as an alternative, never stacked."""
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(10, 0), hours=6,
          guests=8, promo_hint="у нас день рождения")
    assert r.status == "ok"
    assert r.applied_promo == "sixth_hour_free"
    assert r.total == money(7500)
    assert "birthday_discount_10" in r.alternative_promos


def test_birthday_discount_wins_when_no_free_hour(kb):
    r = q(kb, zone_id="grill_house", date=SAT, start_time=NOON, hours=4,
          guests=8, promo_hint="день рождения")
    assert r.status == "ok"
    assert r.applied_promo == "birthday_discount_10"
    assert r.total == money(7200)          # 8000 - 10%


def test_birthday_discount_now_applies_to_baths(kb):
    """1.2: скидка ДЕЙСТВУЕТ на бани. Практика менеджеров из d20 оказалась
    верной, а запрет в прайсе — устаревшим."""
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3,
          guests=6, promo_hint="я именинница")
    assert r.status == "ok"
    assert r.applied_promo == "birthday_discount_10"
    assert r.total == money(9450)          # 10500 − 10%


def test_bath_without_promo_hint_prices_normally(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=6)
    assert r.status == "ok"
    assert r.applied_promo is None


def test_free_hour_promo_now_applies_to_bath(kb):
    """1.1: акция «5 часов + 6-й в подарок» ДЕЙСТВУЕТ на бани."""
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=time(10, 0), hours=6, guests=6)
    assert r.status == "ok"
    assert r.applied_promo == "sixth_hour_free"
    assert r.total == money(17500)         # платим 5 часов из 6
    assert r.billable_hours == 5


def test_bath_promos_do_not_stack(kb):
    """1.2: скидка ДР и «6-й час» не суммируются — выбирается выгодный."""
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=time(10, 0), hours=6,
          guests=6, promo_hint="день рождения")
    assert r.status == "ok"
    # 6-й час в подарок (17500) выгоднее скидки 10% (18900).
    assert r.applied_promo == "sixth_hour_free"
    assert r.total == money(17500)
    assert "birthday_discount_10" in r.alternative_promos


def test_day_package_gets_no_promo(kb):
    r = q(kb, zone_id="grill_house", date=THU, hours=None, guests=8,
          promo_hint="день рождения")
    assert r.status == "ok"
    assert r.applied_promo is None
    assert r.total == money(7000)


# ==========================================================================
# 6. Capacity
# ==========================================================================

def test_capacity_exceeded_is_invalid_with_alternatives(kb):
    r = q(kb, zone_id="bath_knight", date=SAT, start_time=NOON, hours=3, guests=9)
    assert r.status == "invalid"
    assert "bath_russian" in r.suggested_alternatives
    assert "tent" in r.suggested_alternatives


def test_dome_alternatives_are_now_offered(kb):
    """3.3: вместимости куполов подтверждены, поэтому купол снова можно
    предлагать как альтернативу — догадка вниз по потоку больше не уезжает."""
    r = q(kb, zone_id="bath_knight", date=SAT, start_time=NOON, hours=3, guests=9)
    assert r.status == "invalid"
    assert "dome_bags" in r.suggested_alternatives        # 10 мест
    assert "dome_chairs" not in r.suggested_alternatives  # 7 мест, не влезут


@pytest.mark.parametrize("zone_id,seats", [
    ("dome_bags", 10), ("dome_blue_chairs", 7), ("dome_chairs", 7),
])
def test_dome_capacities_are_confirmed(kb, zone_id, seats):
    """3.3: у каждого купола своя вместимость, и они разные."""
    ok = q(kb, zone_id=zone_id, date=SAT, start_time=NOON, hours=3, guests=seats)
    over = q(kb, zone_id=zone_id, date=SAT, start_time=NOON, hours=3, guests=seats + 1)
    assert ok.status == "ok"
    assert over.status == "invalid"
    assert "3.3" not in ok.advisory_question_ids


@pytest.mark.parametrize("zone_id,seats", [
    ("bath_russian", 12), ("bath_garage", 10), ("bath_knight", 6), ("grill_house", 12),
])
def test_confirmed_capacities(kb, zone_id, seats):
    """1.6 и 4.1: вместимости бань и гриль-домика."""
    ok = q(kb, zone_id=zone_id, date=SAT, start_time=NOON, hours=3, guests=seats)
    over = q(kb, zone_id=zone_id, date=SAT, start_time=NOON, hours=3, guests=seats + 1)
    assert ok.status == "ok"
    assert over.status == "invalid"


def test_capacity_exactly_at_limit_is_ok(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=10)
    assert r.status == "ok"


# ==========================================================================
# 7. Holidays
# ==========================================================================

def test_holiday_is_priced_as_weekend(kb):
    """7.2: праздники считаются по тарифу ВЫХОДНОГО дня, а не блокируются.
    5 января 2026 — понедельник по календарю, но это праздник."""
    assert HOLIDAY.weekday() == 0
    r = q(kb, zone_id="bath_russian", date=HOLIDAY, start_time=NOON, hours=3, guests=6)
    assert r.status == "ok"
    assert r.day_type == "weekend"
    assert r.total == money(10500)      # выходной тариф, не будний 7500


def test_day_after_holiday_prices_normally(kb):
    r = q(kb, zone_id="bath_russian", date=AFTER_HOLIDAY, start_time=NOON, hours=3, guests=6)
    assert r.status == "ok"
    assert r.total is not None


def test_new_year_dates_are_no_longer_blocked(kb):
    """Самые дорогие даты в году снова продаются."""
    for zone_id, guests in (("dome_bags", 5), ("grill_house", 5), ("tent", 5), ("yurt", 2)):
        r = q(kb, zone_id=zone_id, date=HOLIDAY, start_time=NOON, hours=3, guests=guests)
        assert r.status == "ok", zone_id
        assert r.day_type == "weekend"


# ==========================================================================
# 8. Closing hour / midnight
# ==========================================================================

def test_booking_past_closing_hour_escalates_without_a_question_id(kb):
    """14.2: окно 9:00-23:00 подтверждено — это больше не дыра в базе (нет
    question_id), а решённое операционное правило: бронь после 23:00 всегда
    уходит на подтверждение менеджера."""
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(21, 0), hours=4, guests=6)
    assert r.status == "blocked"
    assert r.blocking_question_ids == ()
    assert "продлить" in r.human_readable


def test_arrival_before_opening_hour_escalates_too(kb):
    """Раньше проверялся только закрывающий час, и заезд в 7:00 проходил
    насквозь: территория закрыта, а котировка выдавалась как обычная. Ранний
    заезд — то же «нужно, чтобы кто-то открыл», что и поздний выезд."""
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(7, 0), hours=3, guests=6)

    assert r.status == "blocked"
    assert r.blocking_question_ids == ()          # решённое правило, не дыра в базе
    assert "09:00" in r.human_readable
    assert r.total is None                        # цену клиенту не называем


def test_arrival_exactly_at_opening_hour_is_ok(kb):
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(9, 0), hours=3, guests=6)
    assert r.status == "ok"


def test_a_zone_without_its_own_window_falls_back_to_the_complex_hours(kb):
    """У юрты `booking_window: null`. Это значит «своего окна нет», а не
    «работает круглосуточно»: применяется общее окно комплекса
    (constants.working_window), а не отсутствие проверки."""
    zone = next(z for z in kb.catalog.zones if z.id == "yurt")
    assert zone.booking_window is None, "фикстура протухла — у юрты появилось своё окно"

    # Юрта суточная, часов у неё нет вовсе — заодно видно, что ранний заезд
    # проверяется сам по себе, а не как побочный эффект расчёта длительности.
    early = q(kb, zone_id="yurt", date=SAT, start_time=time(7, 0), guests=2)

    assert early.status == "blocked"
    assert "09:00" in early.human_readable


def test_the_complex_hours_are_the_ones_from_the_knowledge_base(kb):
    """Окно берётся из базы знаний, а не зашито числом: оператор правит его
    из Telegram ($.catalog.constants.working_window), и правка обязана
    доезжать до расчёта."""
    loosened = kb.model_copy(deep=True)
    loosened.catalog.constants.working_window.from_ = "06:00"
    next(z for z in loosened.catalog.zones if z.id == "yurt").booking_window = None

    r = q(loosened, zone_id="yurt", date=SAT, start_time=time(7, 0), guests=2)

    assert r.status != "blocked", "правка окна в базе знаний не доехала до движка"


def test_booking_ending_exactly_at_closing_hour_is_ok(kb):
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(19, 0), hours=3, guests=6)
    assert r.status == "ok"


def test_booking_until_23_is_now_allowed(kb):
    """8.1: территория работает 9:00-23:00, доплаты за поздние часы нет."""
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=time(20, 0), hours=3, guests=6)
    assert r.status == "ok"
    assert r.total == money(10500)


def test_booking_from_nine_in_the_morning_is_allowed(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=time(9, 0), hours=3, guests=6)
    assert r.status == "ok"


def test_booking_past_23_still_blocks_but_escalates_cleanly(kb):
    """14.2: решение владельца — бронь, выходящая за рабочее окно, никогда
    не считается по особому тарифу, а эскалируется. Это закрывает и 8.3:
    расчёта «по двум тарифам через полночь» в системе больше не бывает."""
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=time(21, 0), hours=4, guests=6)
    assert r.status == "blocked"
    assert r.blocking_question_ids == ()
    assert r.blocked_reason is not None and "менеджера" in r.blocked_reason


# ==========================================================================
# 9. Day packages
# ==========================================================================

def test_day_package_on_friday_is_invalid(kb):
    """The package is Пн-Чт only; silently falling back to hourly would hand
    the client a total they did not expect."""
    r = q(kb, zone_id="grill_house", date=FRI, hours=None, guests=8)
    assert r.status == "invalid"
    assert r.total is None


def test_day_package_weekday_ok(kb):
    r = q(kb, zone_id="dome_bags", date=MON, hours=None, guests=8)
    assert r.status == "ok"
    assert r.total == money(5000)


def test_tent_day_package_weekday(kb):
    r = q(kb, zone_id="tent", date=THU, hours=None, guests=25)
    assert r.status == "ok"
    assert r.total == money(7500)


def test_bath_has_no_day_package(kb):
    r = q(kb, zone_id="bath_russian", date=THU, hours=None, guests=6)
    assert r.status == "needs_input"
    assert "hours" in r.missing_fields


# ==========================================================================
# 10. Extras
# ==========================================================================

def test_extras_are_separate_lines(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=6,
          extras=(("oak_broom", 2), ("coal", 1)))
    assert r.status == "ok"
    assert r.total == money(10500 + 1600 + 800)
    codes = [line.code for line in r.lines]
    assert "extra:oak_broom" in codes and "extra:coal" in codes


def test_samovar_cheaper_on_weekday(kb):
    weekday = q(kb, zone_id="bath_russian", date=THU, start_time=NOON, hours=3,
                guests=6, extras=(("samovar", 1),))
    weekend = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3,
                guests=6, extras=(("samovar", 1),))
    assert weekday.total == money(7500 + 1000)
    assert weekend.total == money(10500 + 1500)


def test_unknown_extra_is_invalid(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=6,
          extras=(("unicorn", 1),))
    assert r.status == "invalid"


# ==========================================================================
# 11. needs_input
# ==========================================================================

def test_tent_without_guests_needs_input(kb):
    r = q(kb, zone_id="tent", date=SAT, start_time=NOON, hours=3, guests=None)
    assert r.status == "needs_input"
    assert r.missing_fields == ("guests",)


def test_hourly_zone_without_start_time_needs_input(kb):
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=None, hours=3, guests=6)
    assert r.status == "needs_input"
    assert "start_time" in r.missing_fields


def test_needs_input_asks_the_client_not_the_manager(kb):
    r = q(kb, zone_id="tent", date=SAT, start_time=NOON, hours=3, guests=None)
    assert r.blocking_question_ids == ()
    assert "?" in r.human_readable


def test_non_tent_zone_does_not_require_guests(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=None)
    assert r.status == "ok"


# ==========================================================================
# 12. invalid
# ==========================================================================

def test_unknown_zone_is_invalid(kb):
    r = q(kb, zone_id="no_such_zone", date=SAT, start_time=NOON, hours=3, guests=4)
    assert r.status == "invalid"


def test_zero_hours_is_invalid(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=0, guests=4)
    assert r.status == "invalid"


def test_negative_guests_is_invalid(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=-1)
    assert r.status == "invalid"


# ==========================================================================
# 13. Invariants
# ==========================================================================

def test_ok_never_carries_blocking_question_ids(kb):
    cases = [
        PriceRequest("bath_russian", SAT, NOON, 3, 6),
        PriceRequest("dome_bags", SAT, time(10, 0), 6, 8),
        PriceRequest("grill_house", SAT, NOON, 2, 6),
        PriceRequest("yurt", SAT, None, None, 2),
        PriceRequest("tent", SAT, NOON, 3, 19),
    ]
    for req in cases:
        r = quote(req, kb)
        assert r.status == "ok", req.zone_id
        assert r.blocking_question_ids == (), req.zone_id


def test_ok_total_is_always_positive(kb):
    for req in [
        PriceRequest("bath_russian", SAT, NOON, 3, 6),
        PriceRequest("dome_bags", MON, None, None, 8),
        PriceRequest("grill_house", SAT, NOON, 4, 8, (), "день рождения"),
    ]:
        r = quote(req, kb)
        assert r.status == "ok"
        assert r.total > 0


def test_human_readable_hides_derived_hourly_rate_when_promo_applied(kb):
    """Never teach the client a per-hour number that is not our tariff:
    6 hours for 7500 must not surface as «1250 ₽ в час»."""
    r = q(kb, zone_id="dome_bags", date=SAT, start_time=time(10, 0), hours=6, guests=8)
    assert r.applied_promo is not None
    derived = r.total / Decimal(r.occupied_hours)
    assert str(int(derived)) not in r.human_readable
    assert "1 ч — в подарок" in r.human_readable


def test_human_readable_has_no_greeting(kb):
    text = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=6).human_readable
    for greeting in ("Здравствуйте", "Добрый день", "Привет"):
        assert greeting not in text


def test_prepayment_equals_first_hour(kb):
    """9.1: предоплата = стоимость первого часа."""
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=6)
    assert r.prepayment == money(3500)


def test_prepayment_fixed_for_daily_zones(kb):
    """14.4: закрыт цифрой из official_pricing.md — фиксированные 3000 ₽ для
    суточных зон, где стоимости первого часа не существует."""
    r = q(kb, zone_id="yurt", date=SAT, hours=None, guests=2)
    assert r.status == "ok"
    assert r.prepayment == money(3000)
    assert "14.4" not in r.advisory_question_ids


def test_prepayment_fixed_for_day_packages(kb):
    r = q(kb, zone_id="dome_bags", date=MON, hours=None, guests=6)
    assert r.status == "ok"
    assert r.prepayment == money(3000)


def test_every_line_carries_provenance(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=6,
          extras=(("coal", 1),))
    assert all(line.source_field for line in r.lines)


def test_no_float_anywhere_in_the_engine():
    """Money must be Decimal end to end — a single float literal is enough
    to reintroduce rounding drift into a number a client will be charged."""
    for module in (engine_module,):
        tree = ast.parse(inspect.getsource(module))
        floats = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float)
        ]
        assert not floats, f"float literals found: {[f.value for f in floats]}"
        float_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "float"
        ]
        assert not float_calls, "the engine must never call float()"


def test_totals_are_decimal_typed(kb):
    r = q(kb, zone_id="bath_russian", date=SAT, start_time=NOON, hours=3, guests=6)
    assert isinstance(r.total, Decimal)
    assert all(isinstance(line.amount, Decimal) for line in r.lines)


def test_rounding_is_half_up():
    assert money(Decimal("0.5")) == Decimal("1")
    assert money(Decimal("1.5")) == Decimal("2")
    assert money(Decimal("2.4")) == Decimal("2")


# ==========================================================================
# 14. Status coverage — at least three cases each
# ==========================================================================

def test_status_coverage(kb):
    seen: dict[str, int] = {}
    cases = [
        PriceRequest("bath_russian", SAT, NOON, 3, 6),          # ok
        PriceRequest("dome_bags", SAT, time(10, 0), 6, 8),      # ok
        PriceRequest("yurt", SAT, None, None, 2),               # ok
        PriceRequest("bath_russian", SAT, time(21, 0), 4, 6),   # blocked, past 23:00
        PriceRequest("dome_bags", SAT, time(20, 0), 5, 6),      # blocked, past 23:00
        PriceRequest("grill_house", SAT, time(22, 0), 3, 6),    # blocked, past 23:00
        PriceRequest("no_such_zone", SAT, NOON, 3, 4),          # invalid
        PriceRequest("bath_knight", SAT, NOON, 3, 9),           # invalid
        PriceRequest("grill_house", FRI, None, None, 8),        # invalid
        PriceRequest("tent", SAT, NOON, 3, None),               # needs_input
        PriceRequest("dome_bags", SAT, None, 3, 6),             # needs_input
        PriceRequest("bath_russian", THU, None, None, 6),       # needs_input
    ]
    for req in cases:
        seen[quote(req, kb).status] = seen.get(quote(req, kb).status, 0) + 1
    for status in ("ok", "blocked", "invalid", "needs_input"):
        assert seen.get(status, 0) >= 3, f"{status}: {seen.get(status, 0)} cases"
