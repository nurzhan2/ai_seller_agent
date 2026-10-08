"""Домик для отдыха = баня «Гараж» или «Русская» без парилки (ответ заказчика).

Раньше занятость домика всегда была UNKNOWN — у него нет ресурса в YCLIENTS.
"""

import asyncio
from datetime import date, time

from app.booking.base import Availability, AvailabilityStatus
from app.booking.yclients import YClientsProvider

DAY = tuple(f"{h}:{m:02d}" for h in range(9, 24) for m in (0, 30) if (h, m) <= (23, 0))
EVENING_BUSY = tuple(s for s in DAY if not (16 <= int(s.split(":")[0]) < 20))


class Fake(YClientsProvider):
    def __init__(self, parts):
        super().__init__()
        self.parts = parts

    async def check_availability(self, zone_id, date_, start_time=None, hours=None):
        if zone_id in self.COMPOSITE_ZONES:
            return await super().check_availability(zone_id, date_, start_time, hours)
        from app.booking.yclients import interval_is_free

        slots = self.parts[zone_id]
        if slots is None:
            return Availability(AvailabilityStatus.UNKNOWN, reason="сбой")
        ok = interval_is_free(slots, start_time, hours, 0)
        return Availability(
            AvailabilityStatus.FREE if ok else AvailabilityStatus.BUSY, free_slots=slots,
        )


def run(provider, *args):
    return asyncio.run(provider.check_availability("house_relax", date(2026, 10, 14), *args))


def test_free_if_one_bath_free_all_day():
    got = run(Fake({"bath_garage": EVENING_BUSY, "bath_russian": DAY}))
    assert got.status == AvailabilityStatus.FREE and "bath_russian" in got.reason


def test_busy_if_both_baths_busy_during_the_day():
    got = run(Fake({"bath_garage": EVENING_BUSY, "bath_russian": EVENING_BUSY}))
    assert got.status == AvailabilityStatus.BUSY


def test_explicit_hours_are_respected():
    p = Fake({"bath_garage": EVENING_BUSY, "bath_russian": EVENING_BUSY})
    assert run(p, time(10, 0), 4).status == AvailabilityStatus.FREE
    assert run(p, time(15, 0), 3).status == AvailabilityStatus.BUSY


def test_unknown_only_when_nothing_is_free():
    assert run(Fake({"bath_garage": None, "bath_russian": DAY})).status == AvailabilityStatus.FREE
    assert run(Fake({"bath_garage": None, "bath_russian": EVENING_BUSY})).is_known is False


def test_free_slots_for_house_go_through_the_same_rule():
    p = Fake({"bath_garage": EVENING_BUSY, "bath_russian": EVENING_BUSY})
    got = asyncio.run(p.get_free_slots("house_relax", date(2026, 10, 14)))
    assert got.status == AvailabilityStatus.BUSY
