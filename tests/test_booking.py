"""Тесты слоя бронирования.

Главное свойство, которое здесь проверяется: любой сбой превращается в
UNKNOWN, а не в FREE и не в исключение. Провайдер, который на ошибке говорит
«свободно», хуже отсутствующего — агент пообещает клиенту занятую дату.
"""

from __future__ import annotations

import json
from datetime import date, time, timedelta
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


def record_url(company="1") -> str:
    return ep.BASE_URL + ep.BOOK_RECORD[1].format(company_id=company)


def payment_link_url(company="1") -> str:
    return ep.BASE_URL + ep.PAYMENT_LINK[1].format(company_id=company)


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


@respx.mock
async def test_disabled_mapping_row_is_treated_as_missing(verified):
    """Сеть отвечает УСПЕШНО и говорит «свободно» — иначе тест зеленел бы
    сам собой: без зарегистрированного мока запрос падает, и UNKNOWN
    получается по совсем другой причине, а проверка `enabled` в
    InMemoryZoneMapping.get могла быть удалена незамеченной."""
    route = respx.get(times_url()).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": [{"time": "12:00"}], "meta": {}}
        )
    )
    m = InMemoryZoneMapping()
    m.set("bath_russian", service_id="10", staff_id="20", company_id="1")
    m.rows["bath_russian"]["enabled"] = False
    provider = YClientsProvider(mapping=m, company_id="1")

    result = await provider.check_availability("bath_russian", DAY)

    assert result.status is AvailabilityStatus.UNKNOWN
    assert "не заведена" in result.reason
    assert route.call_count == 0, "выключенная строка не должна доходить до сети"


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
    """`data` НЕПУСТАЯ намеренно. С `data: null` тест зеленел бы и без
    проверки `success` вовсе — UNKNOWN приходил бы по ветке «данных нет».
    Опасен именно этот случай: конверт говорит «ошибка», а внутри лежит
    похожий на рабочий список, который так и просится быть принятым за
    расписание."""
    respx.get(times_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": False,
                "data": [{"time": "14:00"}, {"time": "15:00"}],
                "meta": {"message": "staff not found"},
            },
        )
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    result = await provider.get_free_slots("bath_russian", DAY)

    assert result.status is AvailabilityStatus.UNKNOWN
    assert result.free_slots == ()


@pytest.mark.parametrize("status", ep.ACCESS_DENIED_STATUSES)
@respx.mock
async def test_access_denied_becomes_unknown(mapping, verified, status):
    """401/403 — филиал не подключил интеграцию ИЛИ токену не хватает прав
    на этот метод; в обоих случаях деградирует так же, как любой сбой."""
    respx.get(times_url()).mock(return_value=httpx.Response(status))
    provider = YClientsProvider(mapping=mapping, company_id="1")
    result = await provider.get_free_slots("bath_russian", DAY)
    assert result.status is AvailabilityStatus.UNKNOWN


@respx.mock
async def test_access_denied_logs_a_distinct_message_without_overclaiming_cause(mapping, verified, caplog):
    """Разведка (scripts/inspect_yclients.py) нашла 403 на части методов при
    200 на соседних того же токена — «интеграция не подключена» была бы
    ложью в такой ситуации. Лог обязан называть ОБЕ возможные причины, а не
    только первую, и не деградировать до общего «request failed»."""
    respx.get(times_url()).mock(return_value=httpx.Response(403))
    provider = YClientsProvider(mapping=mapping, company_id="1")
    with caplog.at_level("WARNING", logger="parmangal.yclients"):
        await provider.get_free_slots("bath_russian", DAY)
    assert "не подключил интеграцию" in caplog.text
    assert "нет прав на этот конкретный метод" in caplog.text
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
    # Литерал, а не SLOTS_CACHE_TTL: сравнение константы с самой собой
    # зеленело бы при любом её значении, включая «кеш живёт вечно», — а
    # именно 60 секунд названы в имени теста и в докстринге провайдера.
    assert list(redis.ttls.values()) == [60]
    assert SLOTS_CACHE_TTL == 60


@respx.mock
async def test_booking_invalidates_cache(mapping, verified):
    """Иначе минуту показываем занятый слот свободным.

    Сброс проверяется ЧЕРЕЗ create_booking, а не прямым вызовом
    `invalidate_cache`: сам метод работал и раньше, а вот вызов его из
    брони можно было удалить — и тест, дёргавший метод напрямую, этого не
    замечал. Ломается здесь именно проводка, ради которой метод и написан.
    """
    respx.post(record_url()).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": [{"record_id": 42}], "meta": {}}
        )
    )
    redis = FakeRedis()
    provider = YClientsProvider(mapping=mapping, company_id="1", redis=redis)
    await redis.set("yclients:slots:bath_russian:2026-07-18", json.dumps(["14:00"]))

    result = await provider.create_booking(
        BookingRequest(
            zone_id="bath_russian", date=DAY, start_time=time(14, 0), occupied_hours=3,
            guests=6, client_name="Иван", client_phone="+79990000000",
        )
    )

    assert result.success is True
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
# Сотрудники (get_staff) — у этого заказчика сотрудник = зона комплекса.
# Устаревший метод /staff/{company_id}, не новый /company/{id}/staff —
# разведка нашла 200 на первом и 403 на втором для одного токена.
# --------------------------------------------------------------------------

def staff_url(company="1") -> str:
    return ep.BASE_URL + ep.STAFF_FULL_LIST_DEPRECATED[1].format(company_id=company)


@respx.mock
async def test_get_staff_parses_the_flat_list(mapping, verified):
    respx.get(staff_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"id": 100, "name": "Юрта"},
                    {"id": 101, "name": "Шатёр"},
                ],
                "meta": {},
            },
        )
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    staff = await provider.get_staff()

    assert [s.staff_id for s in staff] == ["100", "101"]
    assert staff[0].name == "Юрта"


@respx.mock
async def test_get_staff_is_empty_on_403(mapping, verified):
    """Если токену когда-нибудь не хватит прав и на этот метод — пусто, а
    не исключение. Совпадает с общим правилом деградации YClientsProvider."""
    respx.get(staff_url()).mock(return_value=httpx.Response(403))
    provider = YClientsProvider(mapping=mapping, company_id="1")

    assert await provider.get_staff() == []


# --------------------------------------------------------------------------
# Оплата
# --------------------------------------------------------------------------

@respx.mock
async def test_payment_link_unsupported_returns_none(mapping, verified):
    """Эндпоинт не подтверждён — этап оплаты остаётся за оператором.

    Сеть отвечает ГОТОВОЙ ссылкой: без этого мока тест зеленел бы и со
    снятым гейтом `PAYMENT_LINK_SUPPORTED` — неудавшийся запрос тоже даёт
    None. Здесь же None означает ровно одно: до сети не дошли."""
    route = respx.post(payment_link_url()).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"url": "https://pay.example/42"}, "meta": {}}
        )
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    assert await provider.create_payment_link("42", Decimal("7500")) is None
    assert route.call_count == 0, "неподтверждённый эндпоинт не должен вызываться"


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
    # today_fn зафиксирован ДО DAY: без него check_availability отклонит
    # DAY как прошедшую дату, как только реальные часы обгонят её, и тест
    # перестанет проверять деградацию провайдера — начнёт зелено падать по
    # совсем другой причине.
    ex = ToolExecutor(kb, "d1", booking_provider=provider, today_fn=lambda: DAY - timedelta(days=1))
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
    ex = ToolExecutor(kb, "d1", booking_provider=provider, today_fn=lambda: DAY - timedelta(days=1))
    result = await ex.run(
        "check_availability",
        {"zone_id": "bath_russian", "date": "2026-07-18", "start_time": "14:00"},
    )
    assert result["status"] == "busy"
    assert "18:00" in result["free_slots"]
    assert "не заканчивай разговор отказом" in result["instruction"]


# --------------------------------------------------------------------------
# Реальная структура ответа book_times
#
# Повод: HTTP 200 от YCLIENTS превращался в unknown, и агент уходил к
# менеджеру там, где мог сказать «занято, есть такие-то варианты».
# Структура — из официальной документации YCLIENTS (раздел «Получить список
# сеансов доступных для бронирования»): data — МАССИВ объектов с полями
# time ("17:30"), seance_length (секунды), datetime (ISO8601).
# --------------------------------------------------------------------------

REAL_BOOK_TIMES_RESPONSE = {
    "success": True,
    "data": [
        {"time": "12:00", "seance_length": 3600, "datetime": "2026-08-29T12:00:00+03:00"},
        {"time": "13:00", "seance_length": 3600, "datetime": "2026-08-29T13:00:00+03:00"},
        {"time": "18:00", "seance_length": 3600, "datetime": "2026-08-29T18:00:00+03:00"},
    ],
    "meta": [],
}


@respx.mock
async def test_real_book_times_response_is_parsed_into_free_slots(mapping, verified):
    respx.get(times_url()).mock(return_value=httpx.Response(200, json=REAL_BOOK_TIMES_RESPONSE))
    provider = YClientsProvider(mapping=mapping, company_id="1")

    result = await provider.get_free_slots("bath_russian", DAY)

    assert result.status is AvailabilityStatus.FREE
    assert result.free_slots == ("12:00", "13:00", "18:00")


@respx.mock
async def test_successful_response_with_no_seances_is_busy_not_unknown(mapping, verified):
    """Главная правка: «успешно, но сеансов нет» — это ЗАНЯТО, а не «не
    знаю». Раньше пустой список возвращался как FREE без слотов, то есть
    «свободно, но предложить нечего», и агент замолкал."""
    respx.get(times_url()).mock(
        return_value=httpx.Response(200, json={"success": True, "data": [], "meta": []})
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    result = await provider.get_free_slots("bath_russian", DAY)

    assert result.status is AvailabilityStatus.BUSY
    assert result.is_known is True          # мы ЗНАЕМ, что занято
    assert result.free_slots == ()


@respx.mock
async def test_success_false_body_is_unknown_even_on_http_200(mapping, verified):
    """200 в HTTP ещё не значит успех: конверт v2.0 несёт success отдельно.
    Это единственный оставшийся путь к unknown — реальный сбой запроса.

    `data` заполнена сеансами — теми самыми, которые превратились бы в
    «свободно», если бы флаг конверта не проверялся. С `data: null` тест
    проверял бы не то, что написано в его имени: пустые данные дают unknown
    сами по себе."""
    respx.get(times_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": False,
                "data": [{"time": "12:00", "seance_length": 3600}],
                "meta": {"message": "staff not found"},
            },
        )
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    result = await provider.get_free_slots("bath_russian", DAY)

    assert result.status is AvailabilityStatus.UNKNOWN


@respx.mock
async def test_unrecognised_shape_is_unknown_not_busy(mapping, verified):
    """Неразобранный ответ НЕ должен выдаваться за «занято»: сказать
    клиенту «занято» на основании непонятого ответа хуже, чем «уточню»."""
    respx.get(times_url()).mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"unexpected": 1}, "meta": []})
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    result = await provider.get_free_slots("bath_russian", DAY)

    assert result.status is AvailabilityStatus.UNKNOWN
    assert "формат" in (result.reason or "")


@respx.mock
async def test_nested_seances_object_is_also_parsed(mapping, verified):
    """Соседний метод той же документации (book_staff_seances) отдаёт
    объект {seance_date, seances: [...]}. Ошибиться формой здесь значит
    молча превратить рабочий ответ в unknown — разбираем оба варианта."""
    respx.get(times_url()).mock(
        return_value=httpx.Response(200, json={
            "success": True,
            "data": {"seance_date": 1492041600, "seances": [{"time": "10:00"}]},
            "meta": [],
        })
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    result = await provider.get_free_slots("bath_russian", DAY)

    assert result.status is AvailabilityStatus.FREE
    assert result.free_slots == ("10:00",)


@respx.mock
async def test_check_availability_on_a_free_day_without_time_reports_free(mapping, verified):
    respx.get(times_url()).mock(return_value=httpx.Response(200, json=REAL_BOOK_TIMES_RESPONSE))
    provider = YClientsProvider(mapping=mapping, company_id="1")

    result = await provider.check_availability("bath_russian", DAY)

    assert result.status is AvailabilityStatus.FREE
    assert "18:00" in result.free_slots


@respx.mock
async def test_check_availability_with_no_seances_reports_busy(mapping, verified):
    """Сквозь весь путь: агент получает «занято» и может предложить
    альтернативы, а не эскалировать."""
    respx.get(times_url()).mock(
        return_value=httpx.Response(200, json={"success": True, "data": [], "meta": []})
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")

    result = await provider.check_availability("bath_russian", DAY, start_time=time(14, 0))

    assert result.status is AvailabilityStatus.BUSY


@respx.mock
async def test_agent_tool_says_busy_and_offers_alternatives_on_an_empty_day(kb, mapping, verified):
    """То, ради чего всё это: инструмент агента отдаёт busy с инструкцией
    предложить варианты — вместо unknown с «уточню у менеджера»."""
    respx.get(times_url()).mock(
        return_value=httpx.Response(200, json={"success": True, "data": [], "meta": []})
    )
    provider = YClientsProvider(mapping=mapping, company_id="1")
    ex = ToolExecutor(kb, "d1", booking_provider=provider, today_fn=lambda: DAY - timedelta(days=1))

    result = await ex.run(
        "check_availability",
        {"zone_id": "bath_russian", "date": DAY.isoformat(), "start_time": "14:00"},
    )

    assert result["status"] == "busy"
    assert "find_next_available" in result["instruction"]


@respx.mock
async def test_find_next_available_skips_empty_days_and_returns_ascending(kb, mapping, verified):
    """Дни без сеансов теперь BUSY, а не FREE-без-слотов — поиск обязан их
    пропускать и отдавать даты по возрастанию."""
    empty = {"success": True, "data": [], "meta": []}
    for offset in range(14):
        day = DAY + timedelta(days=offset)
        # Свободны только +1 и +3, остальные пустые.
        body = REAL_BOOK_TIMES_RESPONSE if offset in (1, 3) else empty
        respx.get(times_url(day=day)).mock(return_value=httpx.Response(200, json=body))

    provider = YClientsProvider(mapping=mapping, company_id="1")
    ex = ToolExecutor(kb, "d1", booking_provider=provider, today_fn=lambda: DAY)

    result = await ex.run(
        "find_next_available",
        {"zone_id": "bath_russian", "from_date": DAY.isoformat(), "limit": 3},
    )

    dates = [entry["date"] for entry in result["dates"]]
    assert dates == [(DAY + timedelta(days=1)).isoformat(), (DAY + timedelta(days=3)).isoformat()]
    assert dates == sorted(dates)
