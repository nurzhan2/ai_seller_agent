"""Тесты админки, метрик и предохранителя по расходу."""

from __future__ import annotations

import base64
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
