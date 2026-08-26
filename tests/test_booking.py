"""Тесты слоя бронирования.

Главное свойство, которое здесь проверяется: любой сбой превращается в
UNKNOWN, а не в FREE и не в исключение. Провайдер, который на ошибке говорит
«свободно», хуже отсутствующего — агент пообещает клиенту занятую дату.
"""

from __future__ import annotations

import json
from datetime import date, time
from decimal import Decimal

import httpx
import pytest
import respx

from app.agent.tools import ToolExecutor
from app.booking import yclients_endpoints as ep
from app.booking.base import Availability, AvailabilityStatus, BookingProvider, BookingRequest
from app.booking.mapping import InMemoryZoneMapping, coverage_report
from app.booking.noop import NoopBookingProvider
from app.booking.yclients import SLOTS_CACHE_TTL, YClientsProvider
from app.kb.loader import load_catalog

DAY = date(2026, 7, 18)


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        self.store[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture
def mapping():
    m = InMemoryZoneMapping()
    m.set("bath_russian", service_id="10", staff_id="20", company_id="1")
    return m


@pytest.fixture
def verified(monkeypatch):
    monkeypatch.setattr(ep, "SPEC_VERIFIED", True)


def times_url(company="1", staff="20", day=DAY) -> str:
    return ep.BASE_URL + ep.BOOK_TIMES[1].format(
        company_id=company, staff_id=staff, date=day.isoformat()
    )


# --------------------------------------------------------------------------
# Спек подтверждён заказчиком (ключи получены, факты сверены — не наша
# догадка). Флаг переключился с False на True — три теста ниже раньше
# проверяли поведение ПО УМОЛЧАНИЮ при неподтверждённом спеке; сейчас это
# больше не дефолт, поэтому unverified-поведение тестируется через явный
# monkeypatch обратно на False, а не через реальное состояние модуля.
# --------------------------------------------------------------------------

def test_spec_is_marked_verified():
    assert ep.SPEC_VERIFIED is True


async def test_unverified_spec_yields_unknown_not_free(mapping, monkeypatch):
    monkeypatch.setattr(ep, "SPEC_VERIFIED", False)
    provider = YClientsProvider(mapping=mapping)
    result = await provider.check_availability("bath_russian", DAY, time(14, 0))
    assert result.status is AvailabilityStatus.UNKNOWN
    assert not result.is_known


async def test_unverified_spec_blocks_booking(mapping, monkeypatch):
    monkeypatch.setattr(ep, "SPEC_VERIFIED", False)
    provider = YClientsProvider(mapping=mapping)
    result = await provider.create_booking(
        BookingRequest("bath_russian", DAY, time(14, 0), occupied_hours=3, guests=6)
    )
    assert result.success is False


# --------------------------------------------------------------------------
# Отсутствующий маппинг
# --------------------------------------------------------------------------

async def test_unmapped_zone_is_unknown(mapping, verified):
    """Неполный каталог услуг — ожидаемое состояние, а не ошибка."""
    provider = YClientsProvider(mapping=mapping)
    result = await provider.check_availability("tent", DAY, time(12, 0))
    assert result.status is AvailabilityStatus.UNKNOWN
    assert "не заведена" in result.reason


async def test_disabled_mapping_row_is_treated_as_missing(verified):
    m = InMemoryZoneMapping()
    m.set("bath_russian", service_id="10", staff_id="20", company_id="1")
    m.rows["bath_russian"]["enabled"] = False
    provider = YClientsProvider(mapping=m)
    result = await provider.check_availability("bath_russian", DAY)
    assert result.status is AvailabilityStatus.UNKNOWN


# --------------------------------------------------------------------------
# Сбои сервиса
# --------------------------------------------------------------------------

@respx.mock
async def test_500_becomes_unknown_not_free(mapping, verified):
    respx.get(times_url()).mock(return_value=httpx.Response(500))
    provider = YClientsProvider(mapping=mapping, company_id="1")
    result = await provider.check_availability("bath_russian", DAY, time(14, 0))
    assert result.status is AvailabilityStatus.UNKNOWN


@respx.mock
async def test_network_error_becomes_unknown(mapping, verified):
    respx.get(times_url()).mock(side_effect=httpx.ConnectError("no route"))
    provider = YClientsProvider(mapping=mapping, company_id="1")
    assert (await provider.get_free_slots("bath_russian", DAY)).status is AvailabilityStatus.UNKNOWN


@respx.mock
async def test_success_false_envelope_becomes_unknown(mapping, verified):
    respx.get(times_url()).mock(
        return_value=httpx.Response(200, json={"success": False, "data": None, "meta": {}})
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")
    assert (await provider.get_free_slots("bath_russian", DAY)).status is AvailabilityStatus.UNKNOWN


@pytest.mark.parametrize("status", ep.INTEGRATION_NOT_CONNECTED_STATUSES)
@respx.mock
async def test_integration_not_connected_becomes_unknown(mapping, verified, status):
    """Токены валидны по формату, но филиал не нажал «Подключить» в
    личном кабинете YCLIENTS — деградирует так же, как любой другой сбой."""
    respx.get(times_url()).mock(return_value=httpx.Response(status))
    provider = YClientsProvider(mapping=mapping, company_id="1")
    result = await provider.get_free_slots("bath_russian", DAY)
    assert result.status is AvailabilityStatus.UNKNOWN


@respx.mock
async def test_integration_not_connected_logs_a_distinct_message(mapping, verified, caplog):
    """Дежурный должен сразу понимать по логу, что чинить нужно подключение
    интеграции у заказчика, а не искать баг в коде."""
    respx.get(times_url()).mock(return_value=httpx.Response(403))
    provider = YClientsProvider(mapping=mapping, company_id="1")
    with caplog.at_level("WARNING", logger="parmangal.yclients"):
        await provider.get_free_slots("bath_russian", DAY)
    assert "не подключена филиалом" in caplog.text
    assert "request failed" not in caplog.text


# --------------------------------------------------------------------------
# Нормальный ответ
# --------------------------------------------------------------------------

@respx.mock
async def test_free_slot_is_reported_free(mapping, verified):
    respx.get(times_url()).mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "data": [{"time": "14:00"}, {"time": "16:00"}], "meta": {}},
        )
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")
    result = await provider.check_availability("bath_russian", DAY, time(14, 0))
    assert result.status is AvailabilityStatus.FREE


@respx.mock
async def test_taken_slot_is_busy_and_offers_alternatives(mapping, verified):
    respx.get(times_url()).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": [{"time": "16:00"}], "meta": {}}
        )
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")
    result = await provider.check_availability("bath_russian", DAY, time(14, 0))
    assert result.status is AvailabilityStatus.BUSY
    assert "16:00" in result.free_slots


# --------------------------------------------------------------------------
# Кеш
# --------------------------------------------------------------------------

@respx.mock
async def test_slots_are_cached_for_60_seconds(mapping, verified):
    route = respx.get(times_url()).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": [{"time": "14:00"}], "meta": {}}
        )
    )
    redis = FakeRedis()
    provider = YClientsProvider(mapping=mapping, company_id="1", redis=redis)

    await provider.get_free_slots("bath_russian", DAY)
    await provider.get_free_slots("bath_russian", DAY)

    assert route.call_count == 1, "второй запрос должен идти из кеша"
    assert list(redis.ttls.values()) == [SLOTS_CACHE_TTL]


@respx.mock
async def test_booking_invalidates_cache(mapping, verified):
    """Иначе минуту показываем занятый слот свободным."""
    redis = FakeRedis()
    provider = YClientsProvider(mapping=mapping, company_id="1", redis=redis)
    await redis.set("yclients:slots:bath_russian:2026-07-18", json.dumps(["14:00"]))

    await provider.invalidate_cache("bath_russian", DAY)
    assert await redis.get("yclients:slots:bath_russian:2026-07-18") is None


# --------------------------------------------------------------------------
# Часы занятости против оплаченных
# --------------------------------------------------------------------------

@respx.mock
async def test_booking_blocks_occupied_hours_not_paid_hours(mapping, verified):
    """Акция 5+1: занять надо 6 часов, иначе шестой уйдёт другому клиенту."""
    captured: dict = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": [{"record_id": 42}], "meta": {}})

    respx.post(ep.BASE_URL + ep.BOOK_RECORD[1].format(company_id="1")).mock(side_effect=handler)

    provider = YClientsProvider(mapping=mapping, company_id="1")
    result = await provider.create_booking(
        BookingRequest("bath_russian", DAY, time(10, 0), occupied_hours=6, guests=8)
    )

    assert result.success is True
    assert captured["appointments"][0]["seance_length"] == 6 * 3600


# --------------------------------------------------------------------------
# Каталог услуг (get_services) — envelope book_services это ОБЪЕКТ
# {categories, services}, а не плоский список (developers.yclients.com,
# "Онлайн-запись" -> "Получить список услуг доступных для бронирования").
# Раньше get_services() ждал список и получал [] на любом настоящем ответе.
# --------------------------------------------------------------------------

def services_url(company="1") -> str:
    return ep.BASE_URL + ep.SERVICES[1].format(company_id=company)


@respx.mock
async def test_get_services_parses_the_real_envelope_shape(mapping, verified):
    respx.get(services_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "categories": [{"id": 1, "title": "Бани"}],
                    "services": [
                        {"id": 10, "title": "Русская баня", "price_min": 2500, "price_max": 3500},
                        {"id": 11, "title": "Баня Гараж", "price_min": 3000, "price_max": 3000},
                    ],
                },
                "meta": {},
            },
        )
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    services = await provider.get_services()

    assert [s.service_id for s in services] == ["10", "11"]
    assert services[0].title == "Русская баня"
    assert services[0].price_min == 2500


@respx.mock
async def test_get_services_is_empty_when_services_key_is_missing(mapping, verified):
    """Не роняем список, если форма ответа отличается — просто пусто."""
    respx.get(services_url()).mock(
        return_value=httpx.Response(200, json={"success": True, "data": {}, "meta": {}})
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    assert await provider.get_services() == []


# --------------------------------------------------------------------------
# Оплата
# --------------------------------------------------------------------------

async def test_payment_link_unsupported_returns_none(mapping, verified):
    """Эндпоинт не подтверждён — этап оплаты остаётся за оператором."""
    provider = YClientsProvider(mapping=mapping, company_id="1")
    assert await provider.create_payment_link("42", Decimal("7500")) is None


async def test_noop_provider_never_promises_availability():
    provider = NoopBookingProvider()
    assert (await provider.check_availability("bath_russian", DAY)).status is AvailabilityStatus.UNKNOWN
    assert (await provider.create_payment_link("1", Decimal("100"))) is None


def test_providers_satisfy_the_protocol():
    assert isinstance(NoopBookingProvider(), BookingProvider)
    assert isinstance(YClientsProvider(), BookingProvider)


# --------------------------------------------------------------------------
# Покрытие каталога
# --------------------------------------------------------------------------

def test_coverage_report_lists_mapped_and_unmapped(kb, mapping):
    all_ids = [z.id for z in kb.catalog.zones]
    report = coverage_report(mapping, all_ids)
    assert report["mapped"] == ["bath_russian"]
    assert "tent" in report["unmapped"]
    assert report["total_zones"] == len(all_ids)


def test_empty_mapping_reports_zero_coverage(kb):
    all_ids = [z.id for z in kb.catalog.zones]
    report = coverage_report(InMemoryZoneMapping(), all_ids)
    assert report["coverage"] == 0.0
    assert report["unmapped"] == sorted(all_ids)


# --------------------------------------------------------------------------
# Подключение к агенту
# --------------------------------------------------------------------------

async def test_agent_tool_degrades_to_unknown(kb, mapping):
    provider = YClientsProvider(mapping=mapping)
    ex = ToolExecutor(kb, "d1", booking_provider=provider)
    result = await ex.run(
        "check_availability", {"zone_id": "bath_russian", "date": "2026-07-18", "start_time": "14:00"}
    )
    assert result["status"] == "unknown"
    assert "escalate_to_human" in result["instruction"]


@respx.mock
async def test_agent_tool_reports_busy_with_alternatives(kb, mapping, verified):
    respx.get(times_url()).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": [{"time": "18:00"}], "meta": {}}
        )
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")
    ex = ToolExecutor(kb, "d1", booking_provider=provider)
    result = await ex.run(
        "check_availability",
        {"zone_id": "bath_russian", "date": "2026-07-18", "start_time": "14:00"},
    )
    assert result["status"] == "busy"
    assert "18:00" in result["free_slots"]
    assert "не заканчивай разговор отказом" in result["instruction"]
