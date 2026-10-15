"""Накладка 15.10.2026: гриль-домик на 24.10 «на весь день».

Бот спросил занятость без времени, получил FREE (свободные старты были),
ответил «свободен, можете приезжать», а потом на «к 12:00» посчитал цену и
написал «Оформляю бронь» — при записи 15:30–21:00 в YCLIENTS.
"""

from datetime import date, time

import pytest

from app.agent.tools import ToolExecutor
from app.booking.base import Availability, AvailabilityStatus
from app.booking.yclients import busy_windows, interval_is_free
from app.kb.loader import load_catalog

# Живой ответ book_times для гриль-домика на 24.10 (seance 3 ч, старты до 23:00).
GRILL_2410 = ("9:00", "9:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
              "13:00", "13:30", "14:00", "14:30", "15:00", "21:00", "21:30", "22:00",
              "22:30", "23:00")
FREE_DAY = tuple(f"{h}:{m:02d}" for h in range(9, 24) for m in (0, 30) if (h, m) <= (23, 0))
TODAY = date(2026, 10, 15)


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


class Grill:
    async def check_availability(self, zone_id, date, start_time=None, hours=None):
        slots = GRILL_2410 if zone_id == "grill_house" else FREE_DAY
        if start_time is None:
            return Availability(AvailabilityStatus.FREE, free_slots=slots, seance_seconds=10800)
        ok = interval_is_free(slots, start_time, hours, 10800)
        return Availability(
            AvailabilityStatus.FREE if ok else AvailabilityStatus.BUSY,
            free_slots=slots, seance_seconds=10800,
        )


def test_busy_windows_finds_the_evening_booking():
    assert busy_windows(GRILL_2410, 10800) == ["15:00–21:00"]


def test_free_day_has_no_windows_in_both_slot_meanings():
    assert busy_windows(FREE_DAY, 10800) == []
    # Купола: «[t, t+3ч) свободен», поэтому старты только до 20:00.
    domes = tuple(f"{h}:{m:02d}" for h in range(9, 21) for m in (0, 30) if (h, m) <= (20, 0))
    assert busy_windows(domes, 10800) == []


def test_morning_gap_is_ignored_for_today_only():
    afternoon = tuple(s for s in FREE_DAY if int(s.split(":")[0]) >= 14)
    assert busy_windows(afternoon, 10800) == ["09:00–14:00"]
    assert busy_windows(afternoon, 10800, include_morning=False) == []


async def test_day_question_is_not_answered_free(kb):
    ex = ToolExecutor(kb, "d1", booking_provider=Grill(), today_fn=lambda: TODAY)
    result = await ex.run("check_availability", {"zone_id": "grill_house", "date": "2026-10-24"})
    assert result["status"] == "partially_busy"
    assert result["busy"] == ["15:00–21:00"]
    assert "НЕ говори «свободно»" in result["instruction"]


async def test_really_free_day_stays_free(kb):
    ex = ToolExecutor(kb, "d1", booking_provider=Grill(), today_fn=lambda: TODAY)
    result = await ex.run("check_availability", {"zone_id": "dome_bags", "date": "2026-10-24"})
    assert result["status"] == "free"


async def test_price_on_a_busy_slot_carries_a_stop_warning(kb):
    ex = ToolExecutor(kb, "d1", booking_provider=Grill(), today_fn=lambda: TODAY)
    result = await ex.run("calculate_price", {
        "zone_id": "grill_house", "date": "2026-10-24", "start_time": "12:00",
        "hours": 10, "guests": 9,
    })
    assert result.get("slot_busy") is True
    assert "Не подтверждай бронь" in result["instruction"]


async def test_price_on_a_free_slot_has_no_warning(kb):
    ex = ToolExecutor(kb, "d1", booking_provider=Grill(), today_fn=lambda: TODAY)
    result = await ex.run("calculate_price", {
        "zone_id": "grill_house", "date": "2026-10-24", "start_time": "10:00",
        "hours": 3, "guests": 9,
    })
    assert result["status"] == "ok" and "slot_busy" not in result


# Бронь Наталии 17.10 «с 15 до 18»: в book_times «15:00 свободно, 15:30–17:30
# нет, 18:00 свободно» — старт записи YCLIENTS отдаёт как свободный.
NATALIA_1710 = tuple(s for s in FREE_DAY if not ("15:30" <= s.zfill(5) <= "17:30"))


def test_record_start_counts_as_busy():
    assert busy_windows(NATALIA_1710, 10800) == ["15:00–18:00"]


def test_interval_ending_inside_a_record_is_busy():
    assert not interval_is_free(NATALIA_1710, time(12, 30), 3, 10800)   # до 15:30
    assert interval_is_free(NATALIA_1710, time(12, 0), 3, 10800)        # до 15:00
    assert interval_is_free(NATALIA_1710, time(18, 0), 3, 10800)        # после
    assert not interval_is_free(NATALIA_1710, time(15, 0), 3, 10800)
