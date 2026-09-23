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
    Staff,
)
from app.booking.mapping import InMemoryZoneMapping

logger = logging.getLogger("parmangal.yclients")

SLOTS_CACHE_KEY = "yclients:slots:{zone_id}:{date}"
SLOTS_CACHE_TTL = 60

# Ключи, под которыми у YCLIENTS может лежать список сеансов, если data —
# объект, а не массив. По документации book_times отдаёт МАССИВ объектов
# {time, seance_length, datetime}, и это основной путь; но соседний
# book_staff_seances в той же документации отдаёт объект
# {seance_date, seances: [...]}, поэтому вложенный список тоже разбирается —
# ошибиться формой здесь означает молча превратить рабочий ответ в unknown.
_SEANCE_LIST_KEYS = ("seances", "times", "slots", "data")


def _parse_seances(data: Any) -> Optional[tuple[str, ...]]:
    """Времена сеансов из data. None — формат не распознан (НЕ «пусто»).

    Пустой кортеж и None различаются намеренно: первое значит «ответ понят,
    сеансов нет» (это BUSY), второе — «ответ не понят» (это UNKNOWN).
    Свалить их в одно значило бы сказать клиенту «занято» там, где мы на
    самом деле не разобрали ответ.
    """
    if isinstance(data, dict):
        for key in _SEANCE_LIST_KEYS:
            nested = data.get(key)
            if isinstance(nested, list):
                data = nested
                break
        else:
            return None
    if not isinstance(data, list):
        return None

    slots: list[str] = []
    for item in data:
        if isinstance(item, dict):
            value = item.get("time")
            if value:
                slots.append(str(value))
        elif isinstance(item, str) and item:
            # На случай, если придёт плоский список времён.
            slots.append(item)
    return tuple(slots)


def _parse_seance_seconds(data: Any) -> Optional[int]:
    """seance_length из book_times — у одного сотрудника-зоны он одинаков
    для всех сеансов. None — поля нет (другая форма ответа)."""
    if isinstance(data, dict):
        for key in _SEANCE_LIST_KEYS:
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return None
    lengths = [
        int(item["seance_length"])
        for item in data
        if isinstance(item, dict) and isinstance(item.get("seance_length"), (int, float))
    ]
    return max(lengths) if lengths else None


def _to_minutes(value: str) -> Optional[int]:
    """«9:00» и «09:00» — одно и то же время. YCLIENTS отдаёт часы БЕЗ
    ведущего нуля («9:00», живой лог 2026-09-23), а сравнение шло со
    strftime('%H:%M') = «09:00» — любое утреннее время до 10:00 молча
    считалось занятым."""
    try:
        hours, minutes = str(value).strip().split(":")[:2]
        return int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        return None


# Территория закрывается в 23:00 (catalog.yaml: constants.working_window).
# Хвост брони после закрытия (баня «с 20 до 00» — в банях можно позже)
# никто другой занять не может, и YCLIENTS сеансов после закрытия не
# отдаёт — проверять его по free_slots значит всегда получать «занято».
DAY_CLOSE_MINUTES = 23 * 60


def interval_is_free(
    free_slots: tuple[str, ...],
    start_time: TimeType,
    hours: Optional[int],
    seance_seconds: Optional[int] = None,
    close_minutes: int = DAY_CLOSE_MINUTES,
) -> bool:
    """Свободен ли ВЕСЬ интервал [start, start+hours), а не одно время начала.

    Живой случай 2026-09-19: занятость проверялась только по времени
    начала — бронь, начинающаяся посреди запрошенного интервала, не
    мешала сказать клиенту «свободно».

    Смысл свободного времени t в ответе book_times: [t, t+L) свободен, где
    L — seance_length. Если L == 0 (сотрудник-зона без услуги — так у
    бань), t значит лишь «не внутри чужой записи», и шаг проверки — шаг
    сетки ответа (обычно 30 минут). Интервал покрывается контрольными
    точками s, s+L, s+2L, ... и последней e-L; каждая обязана быть в
    free_slots.
    """
    free = {m for m in (_to_minutes(s) for s in free_slots) if m is not None}
    start = start_time.hour * 60 + start_time.minute
    if start not in free:
        return False
    if not hours or hours <= 0:
        return True

    length = (seance_seconds or 0) // 60
    if length <= 0:
        ordered = sorted(free)
        gaps = [b - a for a, b in zip(ordered, ordered[1:]) if b > a]
        length = min(gaps) if gaps else 30
    end = min(start + hours * 60, close_minutes)

    checkpoints = set(range(start, end - length + 1, length))
    if end - length > start:
        checkpoints.add(end - length)
    return all(point in free for point in checkpoints)


def _slots_to_availability(
    slots: tuple[str, ...], seance_seconds: Optional[int] = None
) -> Availability:
    """Три состояния, а не два: есть сеансы -> FREE, нет -> BUSY.

    Раньше пустой список возвращался как FREE без слотов — то есть
    «свободно, но предложить нечего». Агент на это отвечал клиенту
    «свободно» и замолкал. Пусто на успешном ответе означает ровно
    «на эту дату записаться не к чему» — для клиента это «занято», и
    именно с этим агент может работать: предложить другое время или дату.
    """
    if slots:
        return Availability(
            AvailabilityStatus.FREE, free_slots=slots, seance_seconds=seance_seconds
        )
    return Availability(
        AvailabilityStatus.BUSY,
        reason="на эту дату свободных сеансов нет",
        seance_seconds=seance_seconds,
    )


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

        401/403 ловится отдельно и пишется в лог понятной строкой, а не
        общим «request failed» — но БЕЗ утверждения единственной причины.
        Разведка (scripts/inspect_yclients.py, 2026-08-26) на одном токене
        получила 200 на часть методов и 403 на другие того же филиала —
        то есть 401/403 значит либо «филиал не подключил интеграцию»
        (тогда отказ на ВСЕХ методах), либо «у токена нет прав именно на
        этот метод» (тогда часть методов отвечает 200, как здесь). Раньше
        лог называл только первую причину — вводило в заблуждение, если
        соседний метод только что отработал нормально.
        """
        ep.assert_spec_verified()
        method = spec[0]
        client = self._client or httpx.AsyncClient(base_url=ep.BASE_URL, timeout=self._timeout)
        try:
            response = await client.request(method, path, headers=self._headers(), **kwargs)
            if response.status_code in ep.ACCESS_DENIED_STATUSES:
                logger.warning(
                    "yclients: доступ отклонён (status=%s) на %s. Либо филиал не "
                    "подключил интеграцию в личном кабинете YCLIENTS (тогда откажут "
                    "все методы), либо у токена просто нет прав на этот конкретный "
                    "метод (тогда соседние методы могут отвечать 200 нормально) — "
                    "не считать автоматически первым без проверки других методов.",
                    response.status_code,
                    path,
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

        # Сырой ответ в лог. Повод: book_times отдавал HTTP 200, а занятость
        # становилась unknown — по коду ответа отличить «пришло пусто»,
        # «пришло success:false» и «пришло не то, чего мы ждём» было
        # невозможно, а тела ответа в логе не было вовсе. Здесь нет
        # персональных данных: book_times/book_services возвращают время и
        # услуги, не клиентов. Обрезано, чтобы один ответ не занял пол-лога.
        logger.info(
            "yclients %s -> %s", path, json.dumps(payload, ensure_ascii=False)[:1500],
        )

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
        # По официальной документации book_services (developers.yclients.com,
        # раздел "Онлайн-запись" -> "Получить список услуг доступных для
        # бронирования", проверено 2026-08-24) data — ОБЪЕКТ вида
        # {"categories": [...], "services": [...]}, а не плоский список.
        # Раньше здесь ждали список: isinstance(data, list) было False на
        # ЛЮБОМ настоящем ответе, и метод молча возвращал [] — то есть
        # "услуг 0" получалось даже если у заказчика всё заведено верно.
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, list):
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
            for item in services
        ]

    # -- сотрудники (= зоны у этого заказчика) ------------------------------

    async def get_staff(self) -> list[Staff]:
        """Список сотрудников — физически зон комплекса, см. Staff.

        Осознанно устаревший метод STAFF_FULL_LIST_DEPRECATED, а не новый
        STAFF_FULL_LIST: разведка (scripts/inspect_yclients.py) показала
        200 на /staff/{company_id} и 403 на /company/{company_id}/staff для
        одного и того же токена — разные уровни прав, а не «нет доступа
        вообще» (см. ep.ACCESS_DENIED_STATUSES и _request()).
        """
        if not ep.SPEC_VERIFIED:
            return []
        data = await self._request(
            ep.STAFF_FULL_LIST_DEPRECATED,
            ep.STAFF_FULL_LIST_DEPRECATED[1].format(company_id=self.company_id),
        )
        if not isinstance(data, list):
            return []
        return [
            Staff(staff_id=str(item.get("id")), name=str(item.get("name") or item.get("title") or ""))
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
        if interval_is_free(slots.free_slots, start_time, hours, slots.seance_seconds):
            return Availability(
                AvailabilityStatus.FREE,
                free_slots=slots.free_slots,
                seance_seconds=slots.seance_seconds,
            )
        return Availability(
            AvailabilityStatus.BUSY,
            reason=(
                f"на {wanted} на {hours} ч занято" if hours else f"на {wanted} занято"
            ),
            free_slots=slots.free_slots,
            seance_seconds=slots.seance_seconds,
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
            # Пустой список в кеше — это «на эту дату сеансов нет», а не
            # «кеша нет»: раньше он возвращался как FREE без слотов.
            # Формат кеша: {"slots": [...], "len": seance_seconds}; голый
            # список — прежний формат (живёт максимум SLOTS_CACHE_TTL).
            if isinstance(cached, dict):
                return _slots_to_availability(
                    tuple(cached.get("slots") or ()), cached.get("len")
                )
            return _slots_to_availability(tuple(cached))

        data = await self._request(
            ep.BOOK_TIMES,
            ep.BOOK_TIMES[1].format(
                company_id=row.get("company_id") or self.company_id,
                staff_id=row.get("staff_id", "0"),
                date=date.isoformat(),
            ),
        )
        if data is None:
            # ЕДИНСТВЕННЫЙ путь к UNKNOWN: запрос не удался (сеть, 4xx/5xx,
            # success:false в теле). «Успешно, но сеансов нет» сюда НЕ
            # попадает — см. ниже.
            return Availability(AvailabilityStatus.UNKNOWN, reason="сервис недоступен")

        slots = _parse_seances(data)
        if slots is None:
            # Ответ успешный, но формы, которой мы не знаем. Честнее сказать
            # «не знаю», чем принять неразобранное за «свободных сеансов
            # нет» — второе агент озвучит клиенту как «занято».
            logger.warning(
                "yclients book_times: не удалось разобрать data (%s) — %s",
                type(data).__name__, json.dumps(data, ensure_ascii=False)[:500],
            )
            return Availability(AvailabilityStatus.UNKNOWN, reason="неизвестный формат ответа")

        seance_seconds = _parse_seance_seconds(data)
        await self._cache_set(zone_id, date, {"slots": list(slots), "len": seance_seconds})
        return _slots_to_availability(slots, seance_seconds)

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
        """Подключено к агенту — вызывается из
        `app/agent/tools.py:_tool_create_booking`, когда включён
        `AUTO_BOOKING_ENABLED`.

        AUTO_BOOKING_ENABLED выключен по умолчанию (см. app/config.py):
        аудит 2026-08-28 показал, что этот метод ставит реальную запись в
        YCLIENTS без единой проверки оплаты — только занятости.
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
