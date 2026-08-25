"""Провайдер YCLIENTS.

Вся логика реализована; сетевые вызовы заблокированы, пока
`yclients_endpoints.SPEC_VERIFIED` равен False. Такое состояние ведёт себя как
Noop-провайдер: везде UNKNOWN, никаких выдуманных «свободно».

Три свойства, ради которых этот файл сложнее обёртки над httpx:

  * ЛЮБОЙ сбой (сеть, 500, отсутствие маппинга, неподтверждённый спек)
    превращается в UNKNOWN, а не в исключение и не в FREE;
  * кеш свободных слотов на 60 секунд, чтобы диалог из пяти сообщений не
    устроил пять одинаковых запросов, но и не показывал вчерашнюю картину;
  * бронируются ЧАСЫ ЗАНЯТОСТИ, а не оплаченные часы.
"""

from __future__ import annotations

import json
import logging
from datetime import date as DateType, time as TimeType
from decimal import Decimal
from typing import Any, Optional

import httpx

from app.booking import yclients_endpoints as ep
from app.booking.base import (
    Availability,
    AvailabilityStatus,
    BookingRequest,
    BookingResult,
    PaymentLink,
    Service,
)
from app.booking.mapping import InMemoryZoneMapping

logger = logging.getLogger("parmangal.yclients")

SLOTS_CACHE_KEY = "yclients:slots:{zone_id}:{date}"
SLOTS_CACHE_TTL = 60


class YClientsProvider:
    def __init__(
        self,
        partner_token: str = "",
        user_token: str = "",
        company_id: str = "",
        mapping: Optional[InMemoryZoneMapping] = None,
        client: Optional[httpx.AsyncClient] = None,
        redis: Any = None,
        timeout: float = 15.0,
    ):
        self.partner_token = partner_token
        self.user_token = user_token
        self.company_id = company_id
        self.mapping = mapping or InMemoryZoneMapping()
        self.redis = redis
        self._client = client
        self._timeout = timeout

    # -- транспорт ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            ep.AUTH_HEADER: ep.AUTH_TEMPLATE.format(
                partner_token=self.partner_token, user_token=self.user_token
            ),
            "Accept": ep.ACCEPT_HEADER,
        }

    async def _request(self, spec: tuple[str, str], path: str, **kwargs) -> Optional[dict]:
        """Возвращает data из конверта или None при любом сбое.

        Интеграция подключается филиалом вручную в личном кабинете YCLIENTS —
        пока филиал не нажал «Подключить», формально верные токены всё равно
        получат отказ в доступе (типично 401/403). Это состояние ловится
        отдельно и пишется в лог понятной строкой, а не общим «request
        failed»: дежурный должен сразу понимать, что чинить нужно не код, а
        подключение интеграции у заказчика.
        """
        ep.assert_spec_verified()
        method = spec[0]
        client = self._client or httpx.AsyncClient(base_url=ep.BASE_URL, timeout=self._timeout)
        try:
            response = await client.request(method, path, headers=self._headers(), **kwargs)
            if response.status_code in ep.INTEGRATION_NOT_CONNECTED_STATUSES:
                logger.warning(
                    "yclients: интеграция не подключена филиалом (или токены отозваны) — "
                    "status=%s. Это не ошибка кода: филиал должен нажать «Подключить» "
                    "в личном кабинете YCLIENTS.",
                    response.status_code,
                )
                return None
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            # Сознательно широкий except: наружу этот слой обязан отдавать
            # UNKNOWN, а не ронять диалог с живым клиентом.
            logger.warning("yclients request failed: %s", type(exc).__name__)
            return None
        finally:
            if self._client is None:
                await client.aclose()

        if isinstance(payload, dict) and payload.get("success") is False:
            logger.warning(
                "yclients returned success=false: %s",
                (payload.get("meta") or {}).get("message") or payload.get("meta"),
            )
            return None
        return payload.get("data") if isinstance(payload, dict) else None

    # -- услуги ------------------------------------------------------------

    async def get_services(self) -> list[Service]:
        if not ep.SPEC_VERIFIED:
            return []
        data = await self._request(
            ep.SERVICES, ep.SERVICES[1].format(company_id=self.company_id)
        )
        if not isinstance(data, list):
            return []
        return [
            Service(
                service_id=str(item.get("id")),
                title=str(item.get("title", "")),
                duration_seconds=item.get("seance_length"),
                # price_min/price_max — не подтверждены заказчиком (см.
                # Service). Читаем защитно: отсутствие поля не должно
                # уронить список услуг целиком.
                price_min=item.get("price_min"),
                price_max=item.get("price_max"),
            )
            for item in data
        ]

    # -- занятость ---------------------------------------------------------

    async def check_availability(
        self, zone_id: str, date: DateType, start_time: Optional[TimeType] = None,
        hours: Optional[int] = None,
    ) -> Availability:
        row = self.mapping.get(zone_id)
        if row is None:
            # Каталог услуг у заказчика неполный — это ожидаемое состояние,
            # а не ошибка. Отвечаем «не знаю», агент уходит к менеджеру.
            return Availability(
                AvailabilityStatus.UNKNOWN,
                reason=f"зона {zone_id} не заведена в системе бронирования",
            )
        if not ep.SPEC_VERIFIED:
            return Availability(
                AvailabilityStatus.UNKNOWN, reason="схема YCLIENTS не подтверждена"
            )

        slots = await self.get_free_slots(zone_id, date)
        if not slots.is_known:
            return slots
        if start_time is None:
            return slots

        wanted = start_time.strftime("%H:%M")
        if wanted in slots.free_slots:
            return Availability(AvailabilityStatus.FREE, free_slots=slots.free_slots)
        return Availability(
            AvailabilityStatus.BUSY,
            reason=f"на {wanted} занято",
            free_slots=slots.free_slots,
        )

    async def get_free_slots(self, zone_id: str, date: DateType) -> Availability:
        row = self.mapping.get(zone_id)
        if row is None:
            return Availability(
                AvailabilityStatus.UNKNOWN, reason=f"зона {zone_id} не заведена"
            )
        if not ep.SPEC_VERIFIED:
            return Availability(
                AvailabilityStatus.UNKNOWN, reason="схема YCLIENTS не подтверждена"
            )

        cached = await self._cache_get(zone_id, date)
        if cached is not None:
            return Availability(AvailabilityStatus.FREE, free_slots=tuple(cached))

        data = await self._request(
            ep.BOOK_TIMES,
            ep.BOOK_TIMES[1].format(
                company_id=row.get("company_id") or self.company_id,
                staff_id=row.get("staff_id", "0"),
                date=date.isoformat(),
            ),
        )
        if data is None:
            return Availability(AvailabilityStatus.UNKNOWN, reason="сервис недоступен")

        slots = tuple(str(item.get("time")) for item in data if item.get("time"))
        await self._cache_set(zone_id, date, list(slots))
        return Availability(AvailabilityStatus.FREE, free_slots=slots)

    # -- кеш ---------------------------------------------------------------

    async def _cache_get(self, zone_id: str, date: DateType) -> Optional[list]:
        if self.redis is None:
            return None
        raw = await self.redis.get(SLOTS_CACHE_KEY.format(zone_id=zone_id, date=date.isoformat()))
        if not raw:
            return None
        try:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (json.JSONDecodeError, AttributeError):
            return None

    async def _cache_set(self, zone_id: str, date: DateType, slots: list) -> None:
        if self.redis is None:
            return
        await self.redis.set(
            SLOTS_CACHE_KEY.format(zone_id=zone_id, date=date.isoformat()),
            json.dumps(slots),
            ex=SLOTS_CACHE_TTL,
        )

    async def invalidate_cache(self, zone_id: str, date: DateType) -> None:
        """Вызывается после брони — иначе минуту показываем занятый слот
        свободным."""
        if self.redis is None:
            return
        await self.redis.delete(
            SLOTS_CACHE_KEY.format(zone_id=zone_id, date=date.isoformat())
        )

    # -- бронирование ------------------------------------------------------

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        """Реализовано, но к агенту НЕ подключено.

        AUTO_BOOKING_ENABLED остаётся False до стабильных метрик модерации:
        неверная автобронь стоит дороже, чем неотвеченное сообщение.
        """
        row = self.mapping.get(request.zone_id)
        if row is None:
            return BookingResult(False, error=f"зона {request.zone_id} не заведена")
        if not ep.SPEC_VERIFIED:
            return BookingResult(False, error="схема YCLIENTS не подтверждена")

        # Блокируем ЧАСЫ ЗАНЯТОСТИ. При акции 5+1 это 6, а не оплаченные 5 —
        # иначе шестой час уйдёт другому клиенту.
        seance_length = request.occupied_hours * 3600

        data = await self._request(
            ep.BOOK_RECORD,
            ep.BOOK_RECORD[1].format(company_id=row.get("company_id") or self.company_id),
            json={
                "phone": request.client_phone,
                "fullname": request.client_name,
                "comment": request.comment,
                "appointments": [
                    {
                        "id": 1,
                        "services": [row.get("service_id")],
                        "staff_id": row.get("staff_id"),
                        "datetime": f"{request.date.isoformat()}T{request.start_time.strftime('%H:%M:%S')}",
                        "seance_length": seance_length,
                    }
                ],
            },
        )
        if data is None:
            return BookingResult(False, error="сервис недоступен")

        await self.invalidate_cache(request.zone_id, request.date)
        booking_id = None
        if isinstance(data, list) and data:
            booking_id = str(data[0].get("record_id") or data[0].get("id"))
        elif isinstance(data, dict):
            booking_id = str(data.get("record_id") or data.get("id"))
        return BookingResult(success=True, booking_id=booking_id)

    async def cancel_booking(self, booking_id: str) -> BookingResult:
        if not ep.SPEC_VERIFIED:
            return BookingResult(False, error="схема YCLIENTS не подтверждена")
        data = await self._request(
            ep.DELETE_RECORD, ep.DELETE_RECORD[1].format(record_id=booking_id)
        )
        return BookingResult(success=data is not None)

    # -- оплата ------------------------------------------------------------

    async def create_payment_link(
        self, booking_id: str, amount: Decimal
    ) -> Optional[PaymentLink]:
        """Ссылка ВСЕГДА привязана к брони и сумме.

        Если эндпоинта нет (PAYMENT_LINK_SUPPORTED=False) — возвращаем None, и
        этап оплаты остаётся за оператором. Это не деградация: агенту и так
        запрещено вести оплату реквизитами.
        """
        if not ep.PAYMENT_LINK_SUPPORTED or not ep.SPEC_VERIFIED:
            return None
        data = await self._request(
            ep.PAYMENT_LINK,
            ep.PAYMENT_LINK[1].format(company_id=self.company_id),
            json={"record_id": booking_id, "amount": str(amount)},
        )
        if not isinstance(data, dict) or not data.get("url"):
            return None
        return PaymentLink(url=str(data["url"]), booking_id=booking_id, amount=amount)
