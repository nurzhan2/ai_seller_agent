"""scripts/inspect_yclients.py — только чтение, ничего не решает за оператора.

Главное, что здесь проверяется: скрипт печатает то, что реально пришло —
и объектный envelope (book_services: {categories, services}), и плоский
список, и ошибку YCLIENTS, и не-200, и не-JSON — ничего не проглатывает и
не подставляет вместо непонятного ответа тишину.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.booking import yclients_endpoints as ep
from scripts.inspect_yclients import (
    Check,
    _describe_objects,
    _summarize_data,
    run_check,
)

HEADERS = {"Authorization": "Bearer p, User u", "Accept": ep.ACCEPT_HEADER}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=ep.BASE_URL)


# --------------------------------------------------------------------------
# Форматирование
# --------------------------------------------------------------------------

def test_describe_objects_reports_zero_for_empty_list():
    assert _describe_objects([]) == "0 объектов"


def test_describe_objects_shows_first_three_with_id_and_title():
    items = [{"id": i, "title": f"Услуга {i}"} for i in range(5)]
    out = _describe_objects(items)
    assert "5 объект(ов)" in out
    assert "Услуга 0" in out
    assert "Услуга 3" not in out  # только первые три


def test_describe_objects_surfaces_is_online_and_service_type_flags():
    """Пункт 2 гипотезы из докстринга скрипта — эти поля обязаны быть видны
    сразу в выводе, а не только в сыром JSON."""
    out = _describe_objects([{"id": 1, "title": "Баня", "is_online": False}])
    assert "is_online=False" in out

    out2 = _describe_objects([{"id": 1, "title": "Баня", "service_type": 0}])
    assert "service_type=0" in out2


def test_summarize_data_handles_flat_list():
    assert "2 объект(ов)" in _summarize_data([{"id": 1}, {"id": 2}])


def test_summarize_data_handles_object_with_nested_lists():
    """Реальная форма book_services — {categories: [...], services: [...]},
    а не список. Раньше именно на этой форме код читал 0 услуг."""
    data = {"categories": [{"id": 1, "title": "Бани"}], "services": []}
    out = _summarize_data(data)
    assert "[categories]" in out
    assert "[services]" in out
    assert "0 объектов" in out


def test_summarize_data_handles_plain_object_without_lists():
    out = _summarize_data({"mode": "staff", "any_master": True})
    assert "объект (не список)" in out
    assert "any_master" in out


def test_summarize_data_handles_null():
    assert "пуст" in _summarize_data(None)


# --------------------------------------------------------------------------
# run_check — реальные варианты ответа, ничего не скрывается
# --------------------------------------------------------------------------

@respx.mock
async def test_run_check_prints_nested_envelope(capsys):
    respx.get(ep.BASE_URL + "/book_services/1").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "data": {"categories": [], "services": [{"id": 1, "title": "x"}]}, "meta": {}},
        )
    )
    async with _client() as client:
        await run_check(client, HEADERS, Check("book_services", "/book_services/1"))

    out = capsys.readouterr().out
    assert "код ответа: 200" in out
    assert "[services] 1 объект(ов)" in out


@respx.mock
async def test_run_check_reports_yclients_error_envelope(capsys):
    """success: false — не глотать, показать meta с сообщением."""
    respx.get(ep.BASE_URL + "/resources/1").mock(
        return_value=httpx.Response(
            200, json={"success": False, "data": None, "meta": {"message": "нет доступа"}}
        )
    )
    async with _client() as client:
        await run_check(client, HEADERS, Check("resources", "/resources/1"))

    out = capsys.readouterr().out
    assert "ОШИБКА ОТ YCLIENTS" in out
    assert "нет доступа" in out


@respx.mock
async def test_run_check_reports_non_200_status(capsys):
    respx.get(ep.BASE_URL + "/staff/1").mock(return_value=httpx.Response(404))
    async with _client() as client:
        await run_check(client, HEADERS, Check("staff (устаревший)", "/staff/1"))

    out = capsys.readouterr().out
    assert "код ответа: 404" in out


@respx.mock
async def test_run_check_flags_403_without_overclaiming_not_connected(capsys):
    """403 может значить «не подключено» ИЛИ «токену не хватает прав именно
    на этот метод» (разведка нашла оба случая на одном токене) — вывод
    обязан называть обе причины, а не только первую."""
    respx.get(ep.BASE_URL + "/company/1/staff").mock(return_value=httpx.Response(403))
    async with _client() as client:
        await run_check(client, HEADERS, Check("staff", "/company/1/staff"))

    out = capsys.readouterr().out
    assert "не подключена филиалом" in out
    assert "нет прав на этот конкретный метод" in out


@respx.mock
async def test_run_check_handles_non_json_body(capsys):
    respx.get(ep.BASE_URL + "/company/1/settings/online").mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    async with _client() as client:
        await run_check(client, HEADERS, Check("settings/online", "/company/1/settings/online"))

    out = capsys.readouterr().out
    assert "тело не JSON" in out


@respx.mock
async def test_run_check_handles_network_error(capsys):
    respx.get(ep.BASE_URL + "/book_staff/1").mock(side_effect=httpx.ConnectError("no route"))
    async with _client() as client:
        await run_check(client, HEADERS, Check("book_staff", "/book_staff/1"))

    out = capsys.readouterr().out
    assert "ОШИБКА СЕТИ" in out
