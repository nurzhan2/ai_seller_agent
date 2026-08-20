"""Абстракция системы бронирования.

Заказчик уже мигрировал Bnovo → Google Calendar → YCLIENTS. Смена системы
должна стоить один новый файл, поэтому весь остальной код знает только про
этот интерфейс и никогда — про YCLIENTS напрямую.

Ключевое соглашение: `AvailabilityStatus.UNKNOWN` — законный ответ, а не
ошибка. «Не знаю» и «занято» — принципиально разные вещи: на первое агент
эскалирует, на второе предлагает альтернативу. Провайдер, который на сбое
возвращает FREE, опаснее отсутствующего провайдера.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as DateType, time as TimeType
from decimal import Decimal
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


class AvailabilityStatus(str, Enum):
    FREE = "free"
    BUSY = "busy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Availability:
    status: AvailabilityStatus
    reason: Optional[str] = None
    free_slots: tuple[str, ...] = ()

    @property
    def is_known(self) -> bool:
        return self.status is not AvailabilityStatus.UNKNOWN


@dataclass(frozen=True)
class Service:
    service_id: str
    title: str
    duration_seconds: Optional[int] = None


@dataclass(frozen=True)
class BookingRequest:
    zone_id: str
    date: DateType
    start_time: TimeType
    # ВАЖНО: часы ЗАНЯТОСТИ, а не оплаченные. При акции «5 часов + 6-й в
    # подарок» гость занимает площадку 6 часов, оплачивает 5. Если
    # заблокировать 5, шестой час окажется свободным для чужой брони.
    occupied_hours: int
    guests: int
    client_name: Optional[str] = None
    client_phone: Optional[str] = None
    comment: Optional[str] = None


@dataclass(frozen=True)
class BookingResult:
    success: bool
    booking_id: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class PaymentLink:
    """Ссылка, привязанная к КОНКРЕТНОЙ брони и сумме.

    Агенту разрешено отправлять только такую ссылку. Реквизиты, номера карт,
    телефоны получателей и названия банков запрещены навсегда — см.
    docs/analysis/REPORT.md, раздел «Ограничения для агента».
    """

    url: str
    booking_id: str
    amount: Decimal


@runtime_checkable
class BookingProvider(Protocol):
    async def get_services(self) -> list[Service]: ...

    async def check_availability(
        self, zone_id: str, date: DateType, start_time: Optional[TimeType] = None,
        hours: Optional[int] = None,
    ) -> Availability: ...

    async def get_free_slots(self, zone_id: str, date: DateType) -> Availability: ...

    async def create_booking(self, request: BookingRequest) -> BookingResult: ...

    async def cancel_booking(self, booking_id: str) -> BookingResult: ...

    async def create_payment_link(
        self, booking_id: str, amount: Decimal
    ) -> Optional[PaymentLink]: ...
