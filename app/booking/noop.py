"""Провайдер-заглушка: всегда честно отвечает «не знаю».

Используется, пока YCLIENTS не подключён. Важно, что он не притворяется:
FREE от заглушки означал бы, что агент обещает клиенту свободную дату, ничего
не проверив.
"""

from __future__ import annotations

from datetime import date as DateType, time as TimeType
from decimal import Decimal
from typing import Optional

from app.booking.base import (
    Availability,
    AvailabilityStatus,
    BookingRequest,
    BookingResult,
    PaymentLink,
    Service,
)

UNKNOWN_REASON = "система бронирования не подключена"


class NoopBookingProvider:
    async def get_services(self) -> list[Service]:
        return []

    async def check_availability(
        self, zone_id: str, date: DateType, start_time: Optional[TimeType] = None,
        hours: Optional[int] = None,
    ) -> Availability:
        return Availability(AvailabilityStatus.UNKNOWN, reason=UNKNOWN_REASON)

    async def get_free_slots(self, zone_id: str, date: DateType) -> Availability:
        return Availability(AvailabilityStatus.UNKNOWN, reason=UNKNOWN_REASON)

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        return BookingResult(success=False, error=UNKNOWN_REASON)

    async def cancel_booking(self, booking_id: str) -> BookingResult:
        return BookingResult(success=False, error=UNKNOWN_REASON)

    async def create_payment_link(
        self, booking_id: str, amount: Decimal
    ) -> Optional[PaymentLink]:
        return None
