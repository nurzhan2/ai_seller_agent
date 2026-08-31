"""Часовые пояса и журнал вызовов — по следам инцидента 2026-08-31.

Клиент просил записать его на сегодня, 31 августа. Агент сначала предложил
этот день как свободный, затем ответил «сегодняшняя дата уже прошла», затем
снова предложил — и так по кругу. Две болезни одного корня: даты не было в
промте (модель взяла свою), а код считал «сегодня» по UTC.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.agent.dates import resolve_relative_date
from app.agent.prompts import build_now_block, build_system_prompt
from app.clock import MOSCOW_TZ, describe_now, moscow_today
from app.kb.loader import load_catalog


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


# --------------------------------------------------------------------------
# Граница суток: 01:00 МСК первого числа
# --------------------------------------------------------------------------

# 01:00 МСК 1 сентября = 22:00 UTC 31 августа. По Москве уже сентябрь, по UTC
# ещё август — это и есть окно, в котором «сегодня» клиента превращалось во
# вчера. Каждый час с 00:00 до 03:00 МСК даёт такое расхождение.
MIDNIGHT_EDGE_UTC = datetime(2026, 8, 31, 22, 0, tzinfo=timezone.utc)


def test_moscow_and_utc_disagree_about_the_date_at_that_moment():
    """Проверка самой фикстуры: если однажды перестанет расходиться —
    тесты ниже станут бессмысленными молча."""
    assert MIDNIGHT_EDGE_UTC.date() == date(2026, 8, 31)
    assert MIDNIGHT_EDGE_UTC.astimezone(MOSCOW_TZ).date() == date(2026, 9, 1)


def test_moscow_today_returns_the_moscow_date_not_the_utc_one(monkeypatch):
    import app.clock as clock

    monkeypatch.setattr(clock, "moscow_now", lambda: MIDNIGHT_EDGE_UTC.astimezone(MOSCOW_TZ))

    assert clock.moscow_today() == date(2026, 9, 1)


def test_today_resolves_to_the_moscow_day_at_01_00_msk():
    """«Сегодня» в час ночи первого сентября — это первое сентября, а не
    тридцать первое августа."""
    moscow_day = MIDNIGHT_EDGE_UTC.astimezone(MOSCOW_TZ).date()

    resolution = resolve_relative_date("сегодня", today=moscow_day)

    assert resolution.date == date(2026, 9, 1)


def test_a_booking_for_the_moscow_today_is_not_refused_as_past():
    """Ровно тот отказ, что получил клиент: дата, которую агент сам назвал
    сегодняшней, отвергалась как прошедшая."""
    from app.agent.tools import ToolExecutor

    moscow_day = MIDNIGHT_EDGE_UTC.astimezone(MOSCOW_TZ).date()   # 2026-09-01
    utc_day = MIDNIGHT_EDGE_UTC.date()                            # 2026-08-31
    kb_ = load_catalog()

    # Исполнитель, живущий по Москве, дату «сегодня» принимает.
    ex = ToolExecutor(kb_, "d1", today_fn=lambda: moscow_day)
    result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        ex.run("check_availability", {"zone_id": "bath_russian",
                                      "date": moscow_day.isoformat(), "start_time": "14:00"})
    )
    assert result.get("error") != "дата уже прошла"

    # А живущий по UTC — отвергает её как прошедшую. Это старое поведение.
    ex_utc = ToolExecutor(kb_, "d1", today_fn=lambda: utc_day + timedelta(days=1))
    refused = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        ex_utc.run("check_availability", {"zone_id": "bath_russian",
                                          "date": utc_day.isoformat(), "start_time": "14:00"})
    )
    assert refused.get("error") == "дата уже прошла"


def test_the_shipped_executor_uses_the_moscow_date():
    """Дефолт `today_fn` — именно `moscow_today`, а НЕ `date.today()`.

    Сравнивать сами даты здесь нельзя: 21 час в сутки они совпадают, и
    такой тест зеленел бы при откате на UTC — расходятся они только с 00:00
    до 03:00 МСК. Поэтому проверяется тождество функции."""
    from app.agent.tools import ToolExecutor
    import app.clock as clock

    ex = ToolExecutor(load_catalog(), "d1")

    assert ex._today_fn is clock.moscow_today


def test_relative_dates_are_resolved_against_the_moscow_day(monkeypatch):
    """`resolve_relative_date` без явного `today` обязан спросить московскую
    дату. Патчим именно её: если код вернётся к `date.today()`, патч на него
    не подействует и «завтра» посчитается от UTC-дня."""
    import app.agent.dates as dates

    monkeypatch.setattr(dates, "moscow_today", lambda: date(2026, 9, 1))

    assert resolve_relative_date("сегодня").date == date(2026, 9, 1)
    assert resolve_relative_date("завтра").date == date(2026, 9, 2)


# --------------------------------------------------------------------------
# Дата в системном промте
# --------------------------------------------------------------------------

def test_the_prompt_carries_todays_moscow_date_and_weekday(kb):
    block = build_now_block(MIDNIGHT_EDGE_UTC)

    assert "01.09.2026" in block          # московская дата, не 31.08
    assert "вторник" in block             # день недели
    assert "01:00 МСК" in block           # время
    assert "ЕДИНСТВЕННЫЙ источник" in block


def test_the_prompt_tells_the_model_not_to_use_its_own_idea_of_today(kb):
    rules = build_system_prompt(kb)[0]["text"]

    assert "Сейчас:" in rules, "правила обязаны отсылать к блоку с датой"
    assert "не используй" in rules or "не полагайся" in rules


def test_describe_now_is_moscow_even_for_a_utc_moment():
    assert describe_now(MIDNIGHT_EDGE_UTC).startswith("01.09.2026")


# --------------------------------------------------------------------------
# Журнал вызовов инструментов
# --------------------------------------------------------------------------

async def test_every_tool_call_is_recorded_with_arguments_and_result(kb):
    """Разбор инцидента 2026-08-31 пришлось вести по косвенным уликам: в БД
    лежали только токены и стоимость, а какую дату модель передала в
    create_booking — нигде."""
    from app.agent.loop import AgentLoop
    from tests.test_agent import FakeAnthropic, FakeResponse, TextBlock, ToolUseBlock

    client = FakeAnthropic([
        FakeResponse(content=[ToolUseBlock("resolve_date", {"text": "сегодня"})]),
        FakeResponse(content=[TextBlock("Готово")]),
    ])

    result = await AgentLoop(client, kb).run_turn("d1", [], "когда свободно?")

    trace = result.llm_meta["tool_trace"]
    assert len(trace) == 1
    assert trace[0]["tool"] == "resolve_date"
    assert trace[0]["arguments"] == {"text": "сегодня"}
    assert trace[0]["result"]["date"] == moscow_today().isoformat()


async def test_the_trace_survives_values_json_cannot_serialise(kb):
    """Результаты инструментов содержат Decimal и date. Журнал не должен
    ронять ход клиента ради собственной аккуратности."""
    from app.agent.loop import AgentLoop
    from tests.test_agent import FakeAnthropic, FakeResponse, TextBlock, ToolUseBlock

    client = FakeAnthropic([
        FakeResponse(content=[ToolUseBlock("calculate_price", {
            "zone_id": "bath_russian", "date": "2026-09-05",
            "start_time": "14:00", "hours": 3, "guests": 6})]),
        FakeResponse(content=[TextBlock("Посчитала")]),
    ])

    result = await AgentLoop(client, kb).run_turn("d1", [], "сколько стоит?")

    trace = result.llm_meta["tool_trace"]
    assert trace[0]["tool"] == "calculate_price"
    assert trace[0]["arguments"]["hours"] == 3
    assert trace[0]["result"], "результат обязан быть записан, а не потерян"


async def test_the_trace_is_written_next_to_the_message_in_the_database():
    """llm_meta едет в БД с каждым исходящим — журнал лежит там же, а не в
    отдельной таблице, которая разъедется с сообщением."""
    from app.db.models import Message

    assert hasattr(Message, "llm_meta")
