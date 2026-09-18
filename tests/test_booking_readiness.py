"""Переключатель автобронирования с блокировкой (app/booking/readiness.py).

Главное свойство: включённый AUTO_BOOKING_ENABLED при незакрытых пунктах
НИЧЕГО не ставит в YCLIENTS. До этой правки условие «включать только с
проверкой оплаты» жило в комментарии к конфигу и ничего не блокировало.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agent.tools import ToolExecutor
from app.booking import readiness
from app.booking.readiness import (
    CUSTOMER,
    DEVELOPMENT,
    auto_booking_blockers,
    auto_booking_effective,
)
from app.config import Settings
from app.kb.loader import load_catalog


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


@pytest.fixture(scope="module")
def kb_without_handoff(kb):
    other = kb.model_copy(deep=True)
    other.payment.payment.handoff_on_payment_step = False
    return other


def test_the_shipped_state_lists_every_missing_piece(kb):
    """Боевое состояние 2026-09-18: четыре пункта, два — решение заказчика,
    два — работа разработки. Появится новый пункт или закроется старый —
    тест упадёт, и это повод обновить описание для заказчика."""
    blockers = {b.code: b.owner for b in auto_booking_blockers(kb)}

    assert blockers == {
        "handoff": CUSTOMER,
        "link_type": CUSTOMER,
        "payment_link": DEVELOPMENT,
        "payment_gate": DEVELOPMENT,
    }


def test_the_flag_alone_does_not_turn_booking_on(kb, kb_without_handoff):
    on = Settings(auto_booking_enabled=True)

    assert auto_booking_effective(on, kb) is False
    # Даже если заказчик снимет передачу оплаты человеку — остальное держит.
    assert auto_booking_effective(on, kb_without_handoff) is False


def test_with_every_piece_closed_the_flag_decides(kb_without_handoff, monkeypatch):
    monkeypatch.setattr(readiness, "auto_booking_blockers", lambda kb: [])

    assert auto_booking_effective(Settings(auto_booking_enabled=True), kb_without_handoff) is True
    assert auto_booking_effective(Settings(auto_booking_enabled=False), kb_without_handoff) is False


class _Provider:
    """Всегда свободно; запоминает, пытались ли поставить бронь."""

    def __init__(self):
        self.bookings = []

    async def check_availability(self, zone_id, date, start_time=None, hours=None):
        from app.booking.base import Availability, AvailabilityStatus
        return Availability(AvailabilityStatus.FREE)

    async def get_free_slots(self, zone_id, date):
        from app.booking.base import Availability, AvailabilityStatus
        return Availability(AvailabilityStatus.FREE)

    async def create_booking(self, request):
        from app.booking.base import BookingResult
        self.bookings.append(request)
        return BookingResult(True, booking_id="rec-1")


async def test_a_switched_on_flag_without_readiness_never_reaches_yclients(
    kb_without_handoff, monkeypatch, caplog,
):
    """ГЛАВНЫЙ СЛУЧАЙ. Флаг включён, передача оплаты человеку снята — до
    этой правки агент поставил бы в YCLIENTS настоящую бронь без оплаты."""
    monkeypatch.setattr("app.agent.tools.get_settings",
                        lambda: Settings(auto_booking_enabled=True))
    provider = _Provider()
    ex = ToolExecutor(kb_without_handoff, "d1", booking_provider=provider,
                      today_fn=lambda: date(2026, 8, 27))
    await ex.run("calculate_price", {"zone_id": "bath_russian", "date": "2026-08-29",
                                     "start_time": "14:00", "hours": 3, "guests": 6})

    result = await ex.run("create_booking", {
        "zone_id": "bath_russian", "date": "2026-08-29", "start_time": "14:00",
        "client_name": "Анна", "client_phone": "+79990000000",
    })

    assert result["booked"] is False
    assert provider.bookings == []
    assert "НЕ ГОТОВО" in caplog.text.upper() or "не готово" in caplog.text
