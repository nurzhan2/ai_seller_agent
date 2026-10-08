"""Самопроверка: оценка фактов (без сети) и расписание.

Случаи — живые поломки сентября–октября 2026.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.kb.loader import load_catalog
from app.ops.selfcheck import (
    Facts, ListingFact, OverrideFact, analyze, seconds_until, supervised_selfcheck,
    zone_prices,
)

NOW = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)   # 09:00 МСК
KB = load_catalog()


def L(item_id, title, price, zone=None, cat=None, denied=False):
    return ListingFact(item_id, title, price, zone, cat, denied)


def test_all_good_is_one_line_ok():
    facts = Facts(
        now=NOW,
        listings=[L("1", "Гриль-домик в Троицке", 1500, "grill_house"),
                  L("2", "Купольная беседка", 1000, None, "dome")],
        zone_staff={"grill_house": "5456508"}, yclients_staff={"5456508"},
        availability={"grill_house": "free"}, overrides=[], photo_ok=True,
        stats={"incoming": 5, "sent": 5, "failed": 0},
    )
    report = analyze(facts, KB)
    assert report.ok, report.render()
    assert report.render().startswith("✅")


def test_listing_that_switched_to_dome_prices_is_flagged():
    # 08.10: «Теплые беседки с мангалом» за 1000 ₽ висели на гриль-домике.
    report = analyze(Facts(now=NOW, listings=[
        L("7948042872", "Теплые беседки с мангалом в Ватутинках", 1000, "grill_house"),
    ]), KB)
    assert any("ниже минимальной" in w for w in report.warnings)


def test_unmapped_listing_uses_title_and_unknown_one_is_flagged():
    report = analyze(Facts(now=NOW, listings=[
        L("a", "Баня на дровах «Замок рыцаря»", 2500),
        L("b", "Что-то совсем новое", 3000),
        L("c", "Продажа корпусов", 10**7, denied=True),
    ]), KB)
    assert len(report.warnings) == 1 and "«Что-то совсем новое»" in report.warnings[0]


def test_guest_priced_tent_is_not_price_checked():
    tent = next(z for z in KB.catalog.zones if z.id == "tent")
    assert zone_prices(tent) is None
    report = analyze(Facts(now=NOW, listings=[
        L("t", "Теплый шатер30 человек", 1999, "tent")]), KB)
    assert report.ok


def test_missing_yclients_resource_is_a_problem_once():
    # Юрту удалили из YCLIENTS: одна строка про ресурс, без второй про «unknown».
    report = analyze(Facts(
        now=NOW, zone_staff={"yurt": "5877555", "tent": "5861184"},
        yclients_staff={"5861184"}, availability={"yurt": "unknown", "tent": "free"},
    ), KB)
    assert len(report.problems) == 1 and "yurt" in report.problems[0]


def test_old_override_warns_young_one_does_not():
    report = analyze(Facts(now=NOW, overrides=[
        OverrideFact(1, "$.x", 1500, NOW - timedelta(days=33)),
        OverrideFact(2, "$.y", 1000, NOW - timedelta(days=1)),
    ]), KB)
    assert len(report.warnings) == 1 and "#1" in report.warnings[0]


def test_photo_failure_silent_chats_and_failed_checks():
    report = analyze(Facts(
        now=NOW, photo_ok=False, silent_chats=["u2i-a"], stats={"failed": 4},
        errors=["ресурсы YCLIENTS (HTTPStatusError: 500)"],
    ), KB)
    text = report.render()
    assert report.problems and report.warnings
    assert text.startswith("🔴") and "u2i-a" in text and "YCLIENTS" in text


def test_seconds_until_next_9_msk():
    assert seconds_until(9, NOW) == 24 * 3600          # ровно 09:00 -> завтра
    assert seconds_until(9, NOW - timedelta(hours=1)) == 3600


def test_scheduler_survives_a_failed_run():
    sent, calls = [], {"n": 0}

    async def run():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return "ok"

    async def send(text):
        sent.append(text)

    async def sleep(_):
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    try:
        asyncio.run(supervised_selfcheck(run, send, hour_msk=9, sleep=sleep))
    except asyncio.CancelledError:
        pass
    assert sent[0].startswith("🔴") and sent[1] == "ok"


def test_railway_config_deadline_reminder_only_from_november():
    assert analyze(Facts(now=NOW), KB).ok
    later = analyze(Facts(now=datetime(2026, 11, 20, tzinfo=timezone.utc)), KB)
    assert any("railway.toml" in w for w in later.warnings)
