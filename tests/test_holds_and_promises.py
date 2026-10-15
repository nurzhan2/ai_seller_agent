"""Накладки октября 2026: двоим клиентам одно время и «бронь» на словах."""

from datetime import date, time

import pytest

from app.agent.hold_phrases import HONEST_LINE, strip_false_holds
from app.agent.tools import ToolExecutor
from app.booking.base import Availability, AvailabilityStatus
from app.booking.holds import HANDOFF_TTL, QUOTE_TTL, InMemoryHoldStore
from app.kb.loader import load_catalog

TODAY = date(2026, 10, 15)
DAY = date(2026, 10, 24)
FREE_DAY = tuple(f"{h}:{m:02d}" for h in range(9, 24) for m in (0, 30) if (h, m) <= (23, 0))


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


class AlwaysFree:
    async def check_availability(self, zone_id, date, start_time=None, hours=None):
        return Availability(AvailabilityStatus.FREE, free_slots=FREE_DAY, seance_seconds=10800)


def executor(kb, chat, holds):
    return ToolExecutor(kb, chat, booking_provider=AlwaysFree(), today_fn=lambda: TODAY,
                        hold_store=holds)


# ------------------------------------------------------------------ holds

async def test_quoted_slot_is_busy_for_another_chat_but_not_for_its_own(kb):
    holds = InMemoryHoldStore()
    first = executor(kb, "chat-A", holds)
    quote = await first.run("calculate_price", {
        "zone_id": "grill_house", "date": DAY.isoformat(), "start_time": "12:00",
        "hours": 3, "guests": 8,
    })
    assert quote["status"] == "ok"

    second = executor(kb, "chat-B", holds)
    other = await second.run("check_availability", {
        "zone_id": "grill_house", "date": DAY.isoformat(), "start_time": "13:00", "hours": 3,
    })
    assert other["status"] == "busy"

    same = executor(kb, "chat-A", holds)
    mine = await same.run("check_availability", {
        "zone_id": "grill_house", "date": DAY.isoformat(), "start_time": "12:00", "hours": 3,
    })
    assert mine["status"] == "free"


async def test_hold_shows_in_the_day_view_and_does_not_block_other_zones(kb):
    holds = InMemoryHoldStore()
    await executor(kb, "chat-A", holds).run("calculate_price", {
        "zone_id": "grill_house", "date": DAY.isoformat(), "start_time": "12:00",
        "hours": 3, "guests": 8,
    })
    day = await executor(kb, "chat-B", holds).run(
        "check_availability", {"zone_id": "grill_house", "date": DAY.isoformat()})
    assert day["status"] == "partially_busy"
    assert any(w.startswith("12:00–15:00") for w in day["busy"])

    dome = await executor(kb, "chat-B", holds).run("check_availability", {
        "zone_id": "dome_bags", "date": DAY.isoformat(), "start_time": "12:00", "hours": 3,
    })
    assert dome["status"] == "free"


async def test_hold_expires():
    clock = {"t": 1000.0}
    holds = InMemoryHoldStore(now_fn=lambda: clock["t"])
    await holds.add("grill_house", DAY, time(12, 0), 3, "chat-A", ttl=QUOTE_TTL)
    assert await holds.conflicts("grill_house", DAY, time(13, 0), 1, "chat-B")
    clock["t"] += QUOTE_TTL + 1
    assert not await holds.conflicts("grill_house", DAY, time(13, 0), 1, "chat-B")


async def test_handoff_hold_is_not_shortened_by_a_later_quote():
    clock = {"t": 0.0}
    holds = InMemoryHoldStore(now_fn=lambda: clock["t"])
    await holds.add("grill_house", DAY, time(12, 0), 3, "chat-A", ttl=HANDOFF_TTL)
    await holds.add("grill_house", DAY, time(12, 0), 3, "chat-A", ttl=QUOTE_TTL)
    clock["t"] = QUOTE_TTL + 10
    assert await holds.conflicts("grill_house", DAY, time(12, 0), 3, "chat-B")


# --------------------------------------------------------------- phrases

@pytest.mark.parametrize("text", [
    "Отлично, 9 человек разместятся. Оформляю бронь на 24 октября к 12:00. Как вас зовут?",
    "Смотрите всё, а мы пока придержим для вас время на 17 октября с 15:00.",
    "Прекрасно, буду ждать вас 24 октября к 12:00! Сколько будет гостей?",
    "Готово, бронь подтверждена. Ждём!",
])
def test_promises_are_stripped_and_honest_line_added(text):
    cleaned, stripped = strip_false_holds(text)
    assert stripped
    assert HONEST_LINE in cleaned
    for bad in ("Оформляю", "придержим", "буду ждать вас", "подтверждена"):
        assert bad not in cleaned


def test_question_and_price_survive():
    text = ("Аренда 3 ч × 2000 ₽ = 6000 ₽. Оформляю бронь на 17.10 к 15:00. "
            "Подскажите ваше имя?")
    cleaned, _ = strip_false_holds(text)
    assert "6000 ₽" in cleaned and "Подскажите ваше имя?" in cleaned


@pytest.mark.parametrize("text", [
    "Будем рады видеть вас! Адрес: д. Тупиково.",
    "Ждём вас в гости 🌸",
    "Хотите забронировать время? Тариф 2000 ₽/час.",
])
def test_ordinary_text_is_untouched(text):
    assert strip_false_holds(text) == (text, False)
