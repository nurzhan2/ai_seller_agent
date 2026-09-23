"""Правки по обратной связи заказчика 2026-09-17…23 (рабочий чат «Ии для переписки»).

Каждый тест — живой случай из переписки или из логов Railway, а не
абстрактное свойство: если он краснеет, повторится ровно то, на что
жаловался заказчик.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from app.agent.tools import ToolExecutor
from app.booking.base import Availability, AvailabilityStatus
from app.booking.yclients import _parse_seance_seconds, interval_is_free
from app.channels.item_scope import ALLOW, DENY, classify_title
from app.kb.loader import load_catalog


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


# ---------------------------------------------------------------- занятость

# Живой ответ book_times, баня «Русский стиль», сб 26.09 (лог 2026-09-23):
# занято с 10:30 до 22:00, у сотрудника-зоны нет услуги (seance_length 0).
RUSSIAN_2609 = ("9:00", "9:30", "10:00", "22:00", "22:30", "23:00")


def test_morning_slot_without_leading_zero_is_free():
    """YCLIENTS отдаёт «9:00», сравнение шло с «09:00» — утро до 10:00
    всегда было «занято»."""
    assert interval_is_free(RUSSIAN_2609, time(9, 0), None, 0)


def test_interval_is_checked_whole_not_only_start():
    """Начало 10:00 свободно, но с 10:30 занято — 3 часа с 10:00 нельзя."""
    assert not interval_is_free(RUSSIAN_2609, time(10, 0), 3, 0)


def test_live_case_2309_evening_is_busy_but_22_is_free():
    """Клиент просил 20:00–00:00: это действительно занято. А 22:00–00:00
    свободно — хвост после закрытия (23:00) не проверяется, в банях можно
    позже."""
    assert not interval_is_free(RUSSIAN_2609, time(20, 0), 4, 0)
    assert interval_is_free(RUSSIAN_2609, time(22, 0), 2, 0)


def test_three_hour_seance_near_closing_is_not_false_busy():
    """Купол: seance_length 3 ч. Старт 20:00 значит [20:00, 23:00) свободно;
    старты 20:30+ YCLIENTS не отдаёт (не влезают до закрытия) — это не
    «занято»."""
    slots = tuple(f"{h}:{m:02d}" for h in range(9, 21) for m in (0, 30) if (h, m) <= (20, 0))
    assert interval_is_free(slots, time(20, 0), 3, 10800)
    assert interval_is_free(slots, time(14, 0), 6, 10800)


def test_booking_in_the_middle_of_the_interval_blocks_it():
    """Случай 19.09: бронь 16:00–19:00 посреди запрошенного окна."""
    free = tuple(f"{h}:{m:02d}" for h in range(9, 23) for m in (0, 30)
                 if not (16 <= h < 19))
    assert interval_is_free(free, time(12, 0), 3, 0)
    assert not interval_is_free(free, time(14, 0), 6, 0)


def test_seance_length_is_read_from_book_times():
    data = [{"time": "9:00", "seance_length": 10800}, {"time": "9:30", "seance_length": 10800}]
    assert _parse_seance_seconds(data) == 10800
    assert _parse_seance_seconds([{"time": "9:00"}]) is None


# ---------------------------------------------------------------- альтернативы

class _FreeOnly:
    def __init__(self, free_zone_ids):
        self.free = set(free_zone_ids)
        self.asked: list[str] = []

    async def check_availability(self, zone_id, date, start_time=None, hours=None):
        self.asked.append(zone_id)
        if zone_id in self.free:
            return Availability(AvailabilityStatus.FREE, free_slots=("20:00",))
        return Availability(AvailabilityStatus.BUSY, reason="занято")


async def test_invented_guests_do_not_hide_a_free_small_bath(kb):
    """Живой случай 2026-09-23: клиент не называл число гостей, модель
    подставила guests=10 — и свободная «Рыцарская» (до 6) выпала."""
    provider = _FreeOnly({"bath_knight", "dome_bags"})
    ex = ToolExecutor(kb, "d1", booking_provider=provider, today_fn=lambda: date(2026, 9, 23))
    ex.known_guests = None  # клиент гостей не называл

    result = await ex.run("check_availability", {
        "zone_id": "bath_russian", "date": "2026-09-26",
        "start_time": "20:00", "hours": 4, "guests": 10,
    })

    ids = [a["zone_id"] for a in result["alternatives"]]
    assert ids[0] == "bath_knight", "свободная баня — первой, а не купол"


async def test_guests_named_by_client_still_filter(kb):
    provider = _FreeOnly({"bath_knight", "dome_bags"})
    ex = ToolExecutor(kb, "d1", booking_provider=provider, today_fn=lambda: date(2026, 9, 23))
    ex.known_guests = 10  # клиент сам сказал «нас 10»

    result = await ex.run("check_availability", {
        "zone_id": "bath_russian", "date": "2026-09-26",
        "start_time": "20:00", "hours": 4, "guests": 10,
    })

    ids = [a["zone_id"] for a in result["alternatives"]]
    assert "bath_knight" not in ids
    assert "dome_bags" in ids


async def test_same_category_is_offered_first(kb):
    """Купол занят — сначала другие купола, а не бани."""
    everything = {z.id for z in kb.catalog.zones}
    ex = ToolExecutor(kb, "d1", booking_provider=_FreeOnly(everything),
                      today_fn=lambda: date(2026, 9, 23))

    found = await ex._free_neighbours("dome_bags", date(2026, 9, 26), time(14, 0), 3, None)

    categories = [next(z for z in kb.catalog.zones if z.id == a["zone_id"]).category for a in found]
    assert categories[:2] == [categories[0]] * 2
    assert all(a["zone_id"].startswith("dome_") for a in found[:2])


# ---------------------------------------------------------------- объявления

def test_vacancy_category_is_denied_whatever_the_title():
    decision, reason = classify_title("Банный комплекс ищет помощника", "Вакансии")
    assert (decision, reason) == (DENY, "category_deny")


def test_live_vacancy_title_1709_is_denied_without_category():
    """«Администратор базы отдыха» — ни одного прежнего deny-слова."""
    decision, _ = classify_title("Администратор базы отдыха")
    assert decision == DENY


def test_bath_listing_is_still_allowed():
    decision, _ = classify_title("Приватная Русская баня в Ватутинках", "Предложение услуг")
    assert decision == ALLOW


# ---------------------------------------------------------------- каталог

def test_firewood_is_not_included_in_bath_price(kb):
    """Максим 2026-08-28: «дрова надо убрать, они у нас не входят в сумму»."""
    for zone in kb.catalog.zones:
        if zone.category.value == "bath":
            assert "дрова" not in [str(x) for x in (zone.includes or [])]
