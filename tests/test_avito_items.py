"""Тесты клиента объявлений (GET /core/v1/items) и экспорта в CSV."""

from __future__ import annotations

import csv
import io

import httpx
import pytest
import respx

from app.channels import avito_endpoints as ep
from app.channels.avito_items import AvitoItemsClient, Listing
from app.config import Settings
from tests.test_avito import FakeRedis, token_url  # переиспользуем фейк и хелпер


@pytest.fixture
def settings():
    return Settings(
        avito_client_id="test_id", avito_client_secret="test_secret",
        avito_user_id="777", dry_run=False, avito_max_retries=3,
    )


def _mock_token():
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )


def _items_url() -> str:
    return ep.BASE_URL + ep.LIST_ITEMS[1]


@respx.mock
async def test_list_all_items_single_page(settings):
    _mock_token()
    respx.get(_items_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "meta": {"page": 1, "per_page": 100},
                "resources": [
                    {"id": 1, "title": "Баня Гараж", "url": "https://avito.ru/1",
                     "status": "active", "price": 2500, "address": "Тупиково"},
                    {"id": 2, "title": "Купол", "url": "https://avito.ru/2",
                     "status": "active", "price": None, "address": None},
                ],
            },
        )
    )
    client = AvitoItemsClient(settings=settings)
    listings = await client.list_all_items()
    await client.aclose()

    assert len(listings) == 2
    assert listings[0] == Listing("1", "Баня Гараж", "https://avito.ru/1", "active", 2500, "Тупиково")
    assert listings[1].price is None
    assert listings[1].address is None


@respx.mock
async def test_list_all_items_paginates(settings):
    """per_page=100 и первая страница полная -> должен запросить вторую."""
    _mock_token()
    full_page = [
        {"id": i, "title": f"Item {i}", "url": None, "status": "active", "price": None, "address": None}
        for i in range(100)
    ]
    second_page = [
        {"id": 100, "title": "Последнее", "url": None, "status": "active", "price": None, "address": None}
    ]

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(200, json={"meta": {}, "resources": full_page})
        return httpx.Response(200, json={"meta": {}, "resources": second_page})

    respx.get(_items_url()).mock(side_effect=handler)

    client = AvitoItemsClient(settings=settings)
    listings = await client.list_all_items()
    await client.aclose()

    assert calls["n"] == 2
    assert len(listings) == 101
    assert listings[-1].title == "Последнее"


@respx.mock
async def test_list_all_items_passes_status_filter(settings):
    _mock_token()
    captured = {}

    def handler(request):
        captured["status"] = request.url.params.get("status")
        return httpx.Response(200, json={"meta": {}, "resources": []})

    respx.get(_items_url()).mock(side_effect=handler)

    client = AvitoItemsClient(settings=settings)
    await client.list_all_items(status="active,old")
    await client.aclose()

    assert captured["status"] == "active,old"


@respx.mock
async def test_401_triggers_token_refresh(settings):
    tokens = iter(["stale", "fresh"])
    respx.post(token_url()).mock(
        side_effect=lambda req: httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
    )

    calls: list[str] = []

    def handler(request):
        calls.append(request.headers["Authorization"])
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"meta": {}, "resources": []})

    respx.get(_items_url()).mock(side_effect=handler)

    client = AvitoItemsClient(settings=settings)
    await client.list_all_items()
    await client.aclose()

    assert calls == ["Bearer stale", "Bearer fresh"]


@respx.mock
async def test_retries_on_500(settings):
    _mock_token()
    responses = [httpx.Response(500), httpx.Response(200, json={"meta": {}, "resources": []})]
    route = respx.get(_items_url()).mock(side_effect=responses)

    client = AvitoItemsClient(settings=settings)
    await client.list_all_items()
    await client.aclose()

    assert route.call_count == 2


def test_listing_is_a_plain_frozen_record():
    listing = Listing("1", "title", "url", "active", 100, "addr")
    assert listing.item_id == "1"
    with pytest.raises(Exception):
        listing.item_id = "2"  # frozen


# --------------------------------------------------------------------------
# CSV export shape (без сети — просто проверяем формат строк)
# --------------------------------------------------------------------------

def test_csv_rows_have_expected_columns():
    listings = [
        Listing("1", "Баня", "https://x/1", "active", 2500, "Тупиково"),
        Listing("2", "Купол", None, "old", None, None),
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["item_id", "title", "url", "status", "price", "address"])
    for listing in listings:
        writer.writerow(
            [listing.item_id, listing.title, listing.url or "", listing.status,
             listing.price if listing.price is not None else "", listing.address or ""]
        )
    buffer.seek(0)
    rows = list(csv.reader(buffer))
    assert rows[0] == ["item_id", "title", "url", "status", "price", "address"]
    assert rows[1] == ["1", "Баня", "https://x/1", "active", "2500", "Тупиково"]
    assert rows[2] == ["2", "Купол", "", "old", "", ""]
