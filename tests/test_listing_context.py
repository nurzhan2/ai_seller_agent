"""Тесты разрешения зоны по объявлению (Часть 3 промта №11)."""

from __future__ import annotations

import pytest

from app.agent.listing_context import (
    ItemZoneRow,
    ListingResolution,
    build_listing_hint,
    resolve_listing,
    site_fallback_hint,
)
from app.kb.loader import load_catalog


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


class FakeLookup:
    def __init__(self, rows: dict[str, ItemZoneRow]):
        self.rows = rows

    async def get(self, item_id):
        return self.rows.get(item_id)


# --------------------------------------------------------------------------
# resolve_listing
# --------------------------------------------------------------------------

async def test_no_item_id_is_unknown(kb):
    resolution = await resolve_listing(None, FakeLookup({}), kb)
    assert resolution.status == "unknown"


async def test_no_lookup_is_unknown(kb):
    resolution = await resolve_listing("123", None, kb)
    assert resolution.status == "unknown"


async def test_unmapped_item_is_unknown(kb):
    resolution = await resolve_listing("999", FakeLookup({}), kb)
    assert resolution.status == "unknown"


async def test_direct_zone_mapping_resolves(kb):
    lookup = FakeLookup({"123": ItemZoneRow(zone_id="tent")})
    resolution = await resolve_listing("123", lookup, kb)
    assert resolution.status == "resolved"
    assert resolution.zone_id == "tent"


async def test_category_mapping_with_multiple_zones_is_ambiguous(kb):
    """Ровно ситуация из промта: объявление про баню, конкретная баня неясна."""
    lookup = FakeLookup({"123": ItemZoneRow(category="bath")})
    resolution = await resolve_listing("123", lookup, kb)
    assert resolution.status == "ambiguous"
    assert set(resolution.candidate_zone_ids) == {"bath_russian", "bath_garage", "bath_knight"}
    assert resolution.category == "bath"


async def test_category_mapping_with_single_zone_resolves_directly(kb):
    """Категория «дом» сейчас у нас одна зона — неоднозначности нет."""
    lookup = FakeLookup({"123": ItemZoneRow(category="house")})
    resolution = await resolve_listing("123", lookup, kb)
    assert resolution.status == "resolved"
    assert resolution.zone_id == "house_relax"


async def test_row_with_neither_field_is_unknown(kb):
    lookup = FakeLookup({"123": ItemZoneRow()})
    resolution = await resolve_listing("123", lookup, kb)
    assert resolution.status == "unknown"


async def test_unknown_category_falls_back_to_unknown(kb):
    lookup = FakeLookup({"123": ItemZoneRow(category="not_a_real_category")})
    resolution = await resolve_listing("123", lookup, kb)
    assert resolution.status == "unknown"


# --------------------------------------------------------------------------
# build_listing_hint
# --------------------------------------------------------------------------

def test_resolved_hint_names_the_zone(kb):
    hint = build_listing_hint(ListingResolution(status="resolved", zone_id="tent"), kb)
    assert hint is not None
    assert "Шатёр" in hint
    assert "не переспрашивай" in hint


def test_ambiguous_hint_lists_all_three_baths_one_question(kb):
    """Это ключевое поведение: один вопрос, конкретные варианты, не каталог целиком."""
    resolution = ListingResolution(
        status="ambiguous",
        candidate_zone_ids=("bath_russian", "bath_garage", "bath_knight"),
        category="bath",
    )
    hint = build_listing_hint(resolution, kb)
    assert hint is not None
    assert "Русский стиль" in hint
    assert "Гараж" in hint
    assert "Рыцарская" in hint
    assert "ОДИН уточняющий вопрос" in hint
    assert "весь каталог" in hint


def test_unknown_produces_no_hint(kb):
    assert build_listing_hint(ListingResolution(status="unknown"), kb) is None


def test_resolved_hint_handles_dangling_zone_id_gracefully(kb):
    """Если в БД окажется устаревший zone_id, подсказка не должна падать."""
    hint = build_listing_hint(ListingResolution(status="resolved", zone_id="ghost_zone"), kb)
    assert hint is None


# --------------------------------------------------------------------------
# site_fallback_hint — никогда не выдумывает URL
# --------------------------------------------------------------------------

def test_site_fallback_escalates_when_url_unknown(kb):
    """14.7 не отвечен ни в одном источнике проекта — до ответа агент
    эскалирует, а не придумывает адрес."""
    hint = site_fallback_hint(kb)
    assert "уточню у менеджера" in hint.lower() or "escalate_to_human" in hint
    assert "http" not in hint


def test_site_fallback_uses_url_once_confirmed():
    """Если поле когда-нибудь разрешится, подсказка должна отдать именно его,
    а не общую заглушку — проверяем на синтетическом объекте (pydantic
    model_copy), не трогая реальный catalog.yaml (там пока не подтверждено)."""
    from app.kb.loader import DisputedValue

    kb = load_catalog()
    patched_catalog = kb.catalog.model_copy(
        update={"site_url": DisputedValue[str](value="https://parmangal.example")}
    )
    patched_kb = kb.model_copy(update={"catalog": patched_catalog})

    hint = site_fallback_hint(patched_kb)
    assert "https://parmangal.example" in hint
