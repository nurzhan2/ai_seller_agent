"""Тесты админки, метрик и предохранителя по расходу."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.admin.routes as admin_routes
from app.admin.routes import router as admin_router
from app.booking.mapping import InMemoryZoneMapping
from app.config import Settings
from app.kb.loader import load_catalog
from app.metrics import DailyCostGuard, render_metrics

ADMIN_USER = "operator"
ADMIN_PASSWORD = "s3cure-password"


def auth_header(user=ADMIN_USER, password=ADMIN_PASSWORD) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


def make_client(settings: Settings, kb=None, monkeypatch=None) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router)
    app.state.kb = kb
    app.state.zone_mapping = InMemoryZoneMapping()
    admin_routes.get_settings = lambda: settings
    return TestClient(app)


@pytest.fixture
def settings():
    return Settings(admin_user=ADMIN_USER, admin_password=ADMIN_PASSWORD)


# --------------------------------------------------------------------------
# Доступ
# --------------------------------------------------------------------------

def test_admin_requires_auth(settings, kb):
    client = make_client(settings, kb)
    assert client.get("/admin/readiness").status_code == 401


def test_wrong_password_is_rejected(settings, kb):
    client = make_client(settings, kb)
    response = client.get("/admin/readiness", headers=auth_header(password="wrong"))
    assert response.status_code == 401


def test_correct_credentials_pass(settings, kb):
    client = make_client(settings, kb)
    assert client.get("/admin/readiness", headers=auth_header()).status_code == 200


def test_unset_password_disables_admin_rather_than_opening_it(kb):
    """Незаданный пароль не должен означать «вход свободный»."""
    client = make_client(Settings(admin_user="", admin_password=""), kb)
    assert client.get("/admin/readiness", headers=auth_header()).status_code == 503


# --------------------------------------------------------------------------
# Страница готовности
# --------------------------------------------------------------------------

def test_readiness_shows_both_columns(settings, kb):
    client = make_client(settings, kb)
    html = client.get("/admin/readiness", headers=auth_header()).text
    assert "ready_for_pricing" in html
    assert "ready_for_dialog" in html


def test_readiness_lists_baths_as_price_ready(settings, kb):
    client = make_client(settings, kb)
    html = client.get("/admin/readiness", headers=auth_header()).text
    assert "bath_russian" in html
    assert "Общие блокеры" in html


def test_catalog_page_lists_open_questions(settings, kb):
    client = make_client(settings, kb)
    html = client.get("/admin/catalog", headers=auth_header()).text
    assert "2.1" in html and "13.4" in html


def test_booking_page_reports_empty_catalog_honestly(settings, kb):
    """Каталог YCLIENTS пуст — страница обязана это показать, а не скрыть."""
    client = make_client(settings, kb)
    html = client.get("/admin/booking", headers=auth_header()).text
    assert "0%" in html
    assert "нет" in html


def test_booking_page_does_not_call_it_catalog_coverage(settings, kb):
    """«Покрытие каталога 0%» читалось как «у заказчика пуст каталог
    YCLIENTS» — ошибка диагностики, из-за которой пустой zone_service_map
    (наша связка) путали с пустым каталогом услуг заказчика. Старая
    формулировка не должна вернуться."""
    client = make_client(settings, kb)
    html = client.get("/admin/booking", headers=auth_header()).text
    assert "Покрытие каталога" not in html
    assert "связанные с услугами YCLIENTS" in html


def test_booking_page_shows_yclients_service_count_separately(settings, kb):
    """Отдельная строка «сколько услуг видно в YCLIENTS» — чтобы отличить
    «у заказчика пусто» от «мы просто не связали»."""

    class _FakeProvider:
        async def get_services(self):
            return [object(), object(), object()]

    app = FastAPI()
    app.include_router(admin_router)
    app.state.kb = kb
    app.state.zone_mapping = InMemoryZoneMapping()
    app.state.booking_provider = _FakeProvider()
    admin_routes.get_settings = lambda: settings
    client = TestClient(app)

    html = client.get("/admin/booking", headers=auth_header()).text
    assert "Услуг видно в YCLIENTS" in html
    assert "3" in html


def test_booking_page_without_provider_says_not_connected(settings, kb):
    client = make_client(settings, kb)
    html = client.get("/admin/booking", headers=auth_header()).text
    assert "не подключена" in html


def test_prompt_page_renders_system_prompt(settings, kb):
    client = make_client(settings, kb)
    html = client.get("/admin/prompt", headers=auth_header()).text
    assert "администратор" in html.lower()
    assert "кешируется" in html


def test_pages_without_providers_do_not_crash(settings, kb):
    client = make_client(settings, kb)
    for path in ("/admin/dialogs", "/admin/leads", "/admin/concessions", "/admin/costs"):
        assert client.get(path, headers=auth_header()).status_code == 200


# --------------------------------------------------------------------------
# Экспорт лидов
# --------------------------------------------------------------------------

class _Leads:
    async def list_leads(self):
        return [
            {"name": "Борис", "phone": "89160000000", "zone_id": "house_relax",
             "date": "2026-07-18", "guests": 7, "notes": ""}
        ]


def test_leads_csv_export(settings, kb):
    client = make_client(settings, kb)
    client.app.state.lead_provider = _Leads()
    response = client.get("/admin/leads.csv", headers=auth_header())
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "Борис" in response.text


# --------------------------------------------------------------------------
# Расход (промт №12: разбивка по LLM-провайдеру)
# --------------------------------------------------------------------------

class _Costs:
    async def list_costs(self):
        return [
            {"date": "2026-08-19", "llm_provider": "anthropic", "model": "claude-sonnet-5",
             "dialogs": 12, "cost_rub": "84.30", "cost_per_dialog": "7.03"},
            {"date": "2026-08-19", "llm_provider": "deepseek", "model": "deepseek-v4-pro",
             "dialogs": 3, "cost_rub": "5.10", "cost_per_dialog": "1.70"},
        ]


def test_costs_page_breaks_down_by_llm_provider(settings, kb):
    client = make_client(settings, kb)
    client.app.state.cost_provider = _Costs()
    html = client.get("/admin/costs", headers=auth_header()).text
    assert "провайдер" in html
    assert "anthropic" in html and "deepseek" in html


# --------------------------------------------------------------------------
# Метрики
# --------------------------------------------------------------------------

def test_metrics_render():
    payload, content_type = render_metrics()
    assert b"parmangal_" in payload
    assert "text/plain" in content_type


# --------------------------------------------------------------------------
# Предохранитель по расходу
# --------------------------------------------------------------------------

def test_cost_guard_trips_once_at_limit():
    paused = []
    guard = DailyCostGuard(Decimal("100"), on_pause=lambda: paused.append(True))

    assert guard.add(Decimal("60")) is False
    assert guard.add(Decimal("50")) is True      # 110 > 100
    assert guard.add(Decimal("10")) is False     # повторно не срабатывает
    assert paused == [True]


def test_cost_guard_resets():
    guard = DailyCostGuard(Decimal("10"))
    guard.add(Decimal("20"))
    guard.reset()
    assert guard.tripped is False
    assert guard.spent == Decimal("0")


def test_cost_guard_uses_decimal_not_float():
    guard = DailyCostGuard(Decimal("0.3"))
    guard.add(Decimal("0.1"))
    guard.add(Decimal("0.1"))
    assert guard.add(Decimal("0.1")) is True, "0.1*3 должно быть ровно 0.3"


def test_cost_guard_is_seeded_from_what_was_already_spent_today():
    """Без затравки дневной лимит считался бы «с последнего рестарта», а на
    Railway контейнер перезапускается ещё и при каждом деплое."""
    guard = DailyCostGuard(Decimal("100"))

    already_tripped = guard.seed(Decimal("90"))

    assert already_tripped is False
    assert guard.add(Decimal("15")) is True      # 105 > 100, а не 15


def test_cost_guard_seeded_over_the_limit_reports_it_without_re_alerting():
    """Рестарт после исчерпанного лимита не должен ни поднимать агента в
    работу, ни слать алерт заново — он уже уходил, когда лимит превысили."""
    paused = []
    guard = DailyCostGuard(Decimal("100"), on_pause=lambda: paused.append(True))

    assert guard.seed(Decimal("140")) is True
    assert guard.tripped is True
    assert paused == []


def test_cost_guard_counter_rolls_over_at_moscow_midnight():
    """Сутки — московские, как у лимита исходящих и лимита уступок."""
    now = [datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc)]   # 23:00 МСК
    guard = DailyCostGuard(Decimal("100"), now_fn=lambda: now[0])

    assert guard.add(Decimal("90")) is False
    now[0] = datetime(2026, 8, 29, 21, 30, tzinfo=timezone.utc)  # 00:30 МСК, новые сутки
    assert guard.add(Decimal("90")) is False, "счётчик обязан начаться заново"
    assert guard.spent == Decimal("90")


def test_cost_guard_can_trip_again_the_next_day():
    now = [datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)]
    tripped = []
    guard = DailyCostGuard(Decimal("100"), on_pause=lambda: tripped.append(1),
                           now_fn=lambda: now[0])

    assert guard.add(Decimal("150")) is True
    now[0] = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    assert guard.add(Decimal("150")) is True
    assert len(tripped) == 2


def test_a_zero_limit_is_a_switched_off_guard_not_an_instant_trip():
    """Ноль — «потолка нет» (осознанно, с WARNING в стартовом логе), а не
    «лимит исчерпан первым же рублём»."""
    paused = []
    guard = DailyCostGuard(Decimal("0"), on_pause=lambda: paused.append(True))

    assert guard.enabled is False
    assert guard.add(Decimal("100000")) is False
    assert paused == []


def test_the_shipped_cost_limit_is_not_zero():
    """Незаданная переменная не должна означать «лимита нет» — тот же
    принцип, что у OUTBOUND_DAILY_LIMIT."""
    from app.config import Settings

    assert Settings().daily_cost_limit_rub > 0


# --------------------------------------------------------------------------
# Предохранитель подключён к реальному пути вызова модели
# --------------------------------------------------------------------------

async def test_every_turn_feeds_its_cost_to_the_guard(kb):
    """Ход агента стоит денег — и эти деньги обязаны попасть в счётчик.
    Иначе лимит остаётся строчкой в RUNBOOK, а не предохранителем."""
    from app.agent.loop import AgentLoop

    from tests.test_agent import FakeAnthropic, FakeResponse, TextBlock

    guard = DailyCostGuard(Decimal("1000"))
    client = FakeAnthropic([FakeResponse(content=[TextBlock("Здравствуйте!")])])
    loop = AgentLoop(client, kb, cost_guard=guard)

    result = await loop.run_turn("d1", [], "привет")

    assert guard.spent > 0
    assert guard.spent == Decimal(result.llm_meta["cost_rub"])


async def test_a_turn_killed_by_the_guard_rail_still_costs_money(kb):
    """Последний рубеж не отдаёт текст клиенту, но модель уже отработала.
    Предохранитель ограничивает трату, а не выдачу."""
    from app.agent.loop import AgentLoop

    from tests.test_agent import FakeAnthropic, FakeResponse, TextBlock

    guard = DailyCostGuard(Decimal("1000"))
    client = FakeAnthropic([FakeResponse(content=[TextBlock("С вас 3500 ₽ за баню.")])])
    loop = AgentLoop(client, kb, cost_guard=guard)

    result = await loop.run_turn("d1", [], "сколько стоит баня?")

    assert result.escalated is True          # рубеж сработал
    assert guard.spent > 0                   # но ход всё равно оплачен


async def test_a_broken_guard_does_not_break_the_dialogue(kb):
    """Предохранитель сломан — разговор с живым клиентом продолжается."""
    from app.agent.loop import AgentLoop

    from tests.test_agent import FakeAnthropic, FakeResponse, TextBlock

    class _Broken:
        def add(self, cost):
            raise RuntimeError("счётчик сломан")

    client = FakeAnthropic([FakeResponse(content=[TextBlock("Здравствуйте!")])])
    loop = AgentLoop(client, kb, cost_guard=_Broken())

    result = await loop.run_turn("d1", [], "привет")

    assert result.text == "Здравствуйте!"


# --------------------------------------------------------------------------
# /admin/dialogs: видно, отвечает ли агент в чате прямо сейчас
# --------------------------------------------------------------------------

class _FakeDialogProvider:
    def __init__(self, rows):
        self.rows = rows

    async def list_dialogs(self):
        return self.rows


def _dialogs_client(settings, rows):
    app = FastAPI()
    app.include_router(admin_router)
    app.state.kb = None
    app.state.zone_mapping = InMemoryZoneMapping()
    app.state.dialog_provider = _FakeDialogProvider(rows)
    admin_routes.get_settings = lambda: settings
    return TestClient(app)


def test_dialogs_marks_a_blocklisted_chat_as_blocked():
    """Ровно вопрос, с которого начался разбор: агент в этом диалоге
    отвечает или уже нет?"""
    settings = Settings(admin_user=ADMIN_USER, admin_password=ADMIN_PASSWORD)
    client = _dialogs_client(settings, [{"chat_id": "c-1", "item_id": "8172444564", "messages": 15}])

    html = client.get("/admin/dialogs", headers=auth_header()).text

    assert "заблокирован фильтром" in html


def test_dialogs_marks_a_normal_chat_as_answered():
    settings = Settings(admin_user=ADMIN_USER, admin_password=ADMIN_PASSWORD)
    client = _dialogs_client(settings, [{"chat_id": "c-1", "item_id": "9999-обычное", "messages": 3}])

    html = client.get("/admin/dialogs", headers=auth_header()).text

    assert "агент отвечает" in html


def test_dialogs_shows_the_effective_filter_so_config_drift_is_visible():
    """Список прямо на странице — чтобы не выяснять по косвенным
    признакам, применился ли конфиг."""
    settings = Settings(admin_user=ADMIN_USER, admin_password=ADMIN_PASSWORD)
    client = _dialogs_client(settings, [])

    html = client.get("/admin/dialogs", headers=auth_header()).text

    assert "8172444564" in html and "чёрный список" in html


def test_dialogs_warns_when_the_allowlist_overrides_the_blocklist():
    """Пока AVITO_ALLOWED_ITEMS задан, чёрный список не применяется —
    это должно быть видно, а не выясняться по поведению."""
    settings = Settings(
        admin_user=ADMIN_USER, admin_password=ADMIN_PASSWORD, avito_allowed_items="item-1",
    )
    client = _dialogs_client(settings, [])

    html = client.get("/admin/dialogs", headers=auth_header()).text

    assert "БЕЛЫЙ список" in html
