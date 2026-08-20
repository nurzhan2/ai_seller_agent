"""Tests for app/pricing/concessions.py and app/pricing/quote_gate.py — R1-R12.

Ladder mechanics worth keeping in mind while reading these:
  * a tier that is *meaningless* for the situation is SKIPPED, not blocking;
  * after `max_non_price_attempts_before_price` (2) real non-price offers,
    the remaining non-price tiers are skipped and price tiers open;
  * условие ценовых ступеней — ЗАГРУЗКА, а не близость даты (ответ 2.1).
    Порог max_occupancy_ratio провизорный (0.5, наш), поэтому каждая выдача
    штампуется provisional_policy=True;
  * occupancy_ratio=None означает «посчитать неоткуда» — это НЕ отказ, а
    передача решения оператору (requires_operator_approval).
"""

from __future__ import annotations

import copy
from datetime import date, time
from decimal import Decimal

import pytest
import yaml

from app.kb.loader import KB_DIR, KnowledgeBase, load_catalog
from app.pricing.concessions import (
    ConcessionRequest,
    DialogConcessionState,
    decide,
    is_applicable,
)
from app.pricing.engine import PriceRequest, money, quote
from app.pricing.quote_gate import RATCHET_WARNING, apply_dialog_floor

SAT = date(2026, 7, 18)
THU = date(2026, 7, 16)
MON = date(2026, 7, 13)
HOLIDAY = date(2026, 1, 5)
NOON = time(12, 0)
ALL_NON_PRICE = (1, 2, 3, 4)


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


def _raw():
    return {
        "catalog": yaml.safe_load((KB_DIR / "catalog.yaml").read_text(encoding="utf-8")),
        "promos": yaml.safe_load((KB_DIR / "promos.yaml").read_text(encoding="utf-8")),
        "concessions": yaml.safe_load((KB_DIR / "concessions.yaml").read_text(encoding="utf-8")),
        "payment": yaml.safe_load((KB_DIR / "payment.yaml").read_text(encoding="utf-8")),
    }


@pytest.fixture(scope="module")
def kb_no_window():
    """The window removed entirely — the pre-3.2 state, where R7 shut every
    price tier down. Kept so the R7 denial path stays covered."""
    raw = copy.deepcopy(_raw())
    raw["concessions"]["policy"]["conditions"]["max_occupancy_ratio"] = {
        "value": None,
        "disputed": {"variants": [], "sources": [], "question_id": "14.5"},
    }
    return KnowledgeBase.model_validate(raw)


@pytest.fixture(scope="module")
def kb_generous():
    """Per-dialog limit and attempt cap raised, so tests can reach branches
    the shipping limits would short-circuit."""
    raw = copy.deepcopy(_raw())
    raw["concessions"]["policy"]["max_concessions_per_dialog"] = 10
    raw["concessions"]["policy"]["max_non_price_attempts_before_price"] = 10
    return KnowledgeBase.model_validate(raw)


@pytest.fixture(scope="module")
def bath_quote(kb):
    """Bath on a weekend — the one scope with a confirmed concession floor."""
    return quote(PriceRequest("bath_russian", SAT, NOON, 3, 6), kb)


@pytest.fixture(scope="module")
def dome_quote(kb):
    return quote(PriceRequest("dome_bags", SAT, NOON, 3, 6), kb)


@pytest.fixture(scope="module")
def dome_monday_7h(kb):
    """A booking where a non-price tier genuinely applies: 7 hours bills as
    6 (5+1) = 6000, which the 5000 weekday package beats."""
    return quote(PriceRequest("dome_bags", MON, time(10, 0), 7, 6), kb)


def req(quote_obj, **kwargs):
    base = dict(
        dialog_id="d-test",
        quote=quote_obj,
        observed_triggers=("price_objection",),
        days_until_date=1,
        slot_confirmed_free=True,
        booking_date=SAT,
        # Низкая загрузка: «за 2 дня всё свободно» из ответа 2.1.
        occupancy_ratio=0.2,
        already_used_tiers=(),
        concessions_today=0,
        client_constraints=frozenset(),
        base_price_quoted=True,
    )
    base.update(kwargs)
    return ConcessionRequest(**base)


# ==========================================================================
# R1 — no trigger, no concession. Ever.
# ==========================================================================

def test_r1_no_triggers_denied(kb, bath_quote):
    d = decide(req(bath_quote, observed_triggers=()), kb)
    assert not d.allowed
    assert "R1" in d.denial_reason


def test_r1_unknown_trigger_does_not_count(kb, bath_quote):
    d = decide(req(bath_quote, observed_triggers=("client_seemed_nice",)), kb)
    assert not d.allowed
    assert "R1" in d.denial_reason


def test_r1_any_valid_trigger_unlocks(kb, bath_quote):
    for trigger in ("price_objection", "hours_objection", "going_silent", "soft_decline"):
        d = decide(req(bath_quote, observed_triggers=(trigger,)), kb)
        assert d.allowed, trigger


# ==========================================================================
# R2 — the first price a client hears is always the base price
# ==========================================================================

def test_r2_no_concession_before_base_price_quoted(kb, bath_quote):
    d = decide(req(bath_quote, base_price_quoted=False), kb)
    assert not d.allowed
    assert "R2" in d.denial_reason


def test_r2_allowed_once_base_price_quoted(kb, bath_quote):
    assert decide(req(bath_quote, base_price_quoted=True), kb).allowed


def test_r2_holds_even_with_strong_trigger(kb, bath_quote):
    d = decide(
        req(bath_quote, base_price_quoted=False,
            observed_triggers=("price_objection", "soft_decline")),
        kb,
    )
    assert not d.allowed
    assert "R2" in d.denial_reason


# ==========================================================================
# R3 — ascending tiers, and the skipped state
# ==========================================================================

def test_r3_starts_at_tier_one(kb, bath_quote):
    assert decide(req(bath_quote), kb).tier == 1


def test_r3_advances_past_used_tiers(kb_generous, bath_quote):
    d = decide(req(bath_quote, already_used_tiers=(1,)), kb_generous)
    assert d.tier != 1


def test_r3_inapplicable_tier_is_skipped_not_blocking(kb, bath_quote):
    """The headline fix: the client insists on a weekend, so tier 1 can
    never be used — the ladder must step over it, not stall forever."""
    d = decide(req(bath_quote, client_constraints=frozenset({"weekend_only"})), kb)
    assert d.allowed
    assert d.tier != 1
    assert 1 in d.skipped_tiers


def test_r3_date_fixed_also_skips_tier_one(kb, bath_quote):
    d = decide(req(bath_quote, client_constraints=frozenset({"date_fixed"})), kb)
    assert 1 in d.skipped_tiers


def test_r3_exhausted_ladder_denies(kb_generous, bath_quote):
    d = decide(req(bath_quote, already_used_tiers=(1, 2, 3, 4, 5, 6)), kb_generous)
    assert not d.allowed
    assert "R3/R5" in d.denial_reason


# ==========================================================================
# R4 — price tiers unlock after applicable non_price tiers are done
# ==========================================================================

def test_r4_two_attempts_open_price_tiers(kb, bath_quote):
    """max_non_price_attempts_before_price=2: after two real offers the
    price tiers open even though tier 4 was never used."""
    d = decide(req(bath_quote, already_used_tiers=(1, 2)), kb)
    assert d.allowed
    assert d.kind == "price"
    assert 4 not in d.already_used if hasattr(d, "already_used") else True
    assert 3 in d.skipped_tiers and 4 in d.skipped_tiers


def test_r4_first_applicable_tier_is_non_price(kb, dome_monday_7h):
    """A dome on Monday for 7 hours: shifting to a weekday is pointless (it
    already is one) and there is no cheaper zone, but the day package (5000)
    genuinely beats the hourly total (6000) — so tier 3 is offered first."""
    d = decide(req(dome_monday_7h, booking_date=MON), kb)
    assert d.allowed
    assert d.kind == "non_price"
    assert d.tier == 3
    assert set(d.skipped_tiers) >= {1, 2}


def test_r4_price_tier_after_all_four_non_price(kb, bath_quote):
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE), kb)
    assert d.allowed
    assert d.kind == "price"


def test_r4_all_non_price_skipped_opens_price_immediately(kb):
    """Когда ни одна неценовая ступень не применима, лестница законно
    приходит к ценовой на первом же шаге.

    Здесь клиент зафиксировал день, зону и часы, а акция «6-й час в подарок»
    уже учтена в базовом расчёте — предлагать её второй раз нечего.
    """
    six_hours = quote(PriceRequest("bath_russian", SAT, time(10, 0), 6, 6), kb)
    assert six_hours.applied_promo == "sixth_hour_free"

    d = decide(
        req(
            six_hours,
            client_constraints=frozenset({"weekend_only", "zone_fixed", "hours_fixed"}),
        ),
        kb,
    )
    assert d.kind == "price"
    assert set(d.skipped_tiers) >= {1, 2, 3, 4}


# ==========================================================================
# R5 — one tier per dialog
# ==========================================================================

def test_r5_used_tier_is_not_repeated(kb, bath_quote):
    assert decide(req(bath_quote, already_used_tiers=(1,)), kb).tier != 1


def test_r5_never_returns_an_already_used_tier(kb_generous, bath_quote):
    """The core R5 property, stated directly: whatever the ladder picks, it
    is never something this dialog already spent."""
    for used in [(), (1,), (1, 2), (1, 2, 3), (1, 2, 3, 4), (1, 2, 3, 4, 5)]:
        d = decide(req(bath_quote, already_used_tiers=used), kb_generous)
        if d.tier is not None and d.allowed:
            assert d.tier not in used, f"tier {d.tier} re-granted after {used}"


# ==========================================================================
# R6 — every condition must hold
# ==========================================================================

def test_r6_slot_not_free_denies(kb, bath_quote):
    d = decide(req(bath_quote, slot_confirmed_free=False), kb)
    assert not d.allowed
    assert "R6" in d.denial_reason


def test_r6_holiday_is_derived_not_trusted(kb, bath_quote):
    """A caller cannot switch this rule off: there is no is_holiday flag to
    pass any more, and the date is checked against constants.holidays."""
    d = decide(req(bath_quote, booking_date=HOLIDAY), kb)
    assert not d.allowed
    assert "R6" in d.denial_reason
    assert "праздничная" in d.denial_reason


def test_r6_non_holiday_passes(kb, bath_quote):
    assert decide(req(bath_quote, booking_date=SAT), kb).allowed


def test_high_occupancy_denies_price_tier(kb, bath_quote):
    """Ответ 2.1: «если за два дня одна банька осталась — сдаём за 15 тыс.».
    Дата, которая и так продаётся, скидки не получает."""
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE, occupancy_ratio=0.8), kb)
    assert not d.allowed
    assert "R7" in d.denial_reason
    assert "загрузка" in d.denial_reason


def test_low_occupancy_allows_price_tier(kb, bath_quote):
    """«Если за 2 дня всё свободно — сдаём за 9 500»."""
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE, occupancy_ratio=0.3), kb)
    assert d.allowed


def test_occupancy_boundary_is_inclusive(kb, bath_quote):
    at_limit = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE, occupancy_ratio=0.5), kb)
    above = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE, occupancy_ratio=0.51), kb)
    assert at_limit.allowed
    assert not above.allowed


def test_far_date_no_longer_matters_if_slot_is_empty(kb, bath_quote):
    """Ключевое отличие от прежней логики: условие — загрузка, а не близость
    даты. Пустая дата через месяц теперь скидку получает."""
    d = decide(
        req(bath_quote, already_used_tiers=ALL_NON_PRICE,
            days_until_date=30, occupancy_ratio=0.1),
        kb,
    )
    assert d.allowed


def test_unknown_occupancy_goes_to_operator_not_denied(kb, bath_quote):
    """В YCLIENTS сейчас 0 зон из 10, загрузку посчитать неоткуда. Это НЕ
    отказ: решение принимает оператор кнопкой в Telegram."""
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE, occupancy_ratio=None), kb)
    assert d.allowed is False
    assert d.requires_operator_approval is True
    assert "оператор" in d.denial_reason.lower() or "решение" in d.denial_reason.lower()


def test_denied_by_occupancy_does_not_ask_the_operator(kb, bath_quote):
    """Отказ и «спросите оператора» — разные исходы, их нельзя путать."""
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE, occupancy_ratio=0.9), kb)
    assert d.requires_operator_approval is False


# ==========================================================================
# R7 — window, provisional or missing
# ==========================================================================

def test_r7_missing_threshold_forbids_price_tiers(kb_no_window, bath_quote):
    """Если порога загрузки нет вовсе — ценовые ступени закрыты."""
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE), kb_no_window)
    assert not d.allowed
    assert "R7" in d.denial_reason
    assert d.blocking_question_ids == ("14.5",)


def test_r7_missing_window_still_allows_non_price(kb_no_window, bath_quote):
    d = decide(req(bath_quote, client_constraints=frozenset()), kb_no_window)
    assert d.allowed
    assert d.kind == "non_price"


def test_r7_provisional_window_permits_and_stamps(kb, bath_quote):
    """The shipping config: the window is ours, not the client's, so the
    grant is marked so an audit can tell whose rule it was."""
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE), kb)
    assert d.allowed
    assert d.provisional_policy is True


def test_r7_non_price_grants_are_not_stamped_provisional(kb, bath_quote):
    d = decide(req(bath_quote), kb)
    assert d.kind == "non_price"
    assert d.provisional_policy is False


# ==========================================================================
# R8 — floors
# ==========================================================================

def test_r8_dome_price_tier_denied_floor_is_null(kb, dome_quote):
    """Only bath-on-weekend has a confirmed floor. A dome must refuse and
    name the question that would unblock it."""
    d = decide(req(dome_quote, already_used_tiers=(1, 2, 3, 4, 5),
                   client_constraints=frozenset({"weekend_only"})), kb)
    assert not d.allowed
    assert "R8" in d.denial_reason
    assert d.blocking_question_ids == ("13.2",)


def test_r8_bath_weekend_rate_floor_is_confirmed(kb, bath_quote):
    d = decide(req(bath_quote, already_used_tiers=(1, 2, 3, 4, 5)), kb)
    assert d.allowed
    assert d.new_quote.total == money(7500)     # 3h × 2500 floor
    assert d.revenue_delta == money(-3000)
    assert d.revenue_delta_basis == "base_rate"


def test_r8_bath_weekday_has_no_confirmed_rate_floor(kb):
    """The client confirmed the weekend floor only; a weekday bath must not
    inherit it."""
    weekday_quote = quote(PriceRequest("bath_russian", THU, NOON, 3, 6), kb)
    d = decide(req(weekday_quote, already_used_tiers=(1, 2, 3, 4, 5),
                   booking_date=THU, days_until_date=1), kb)
    assert not d.allowed
    assert "R8" in d.denial_reason


def test_r8_never_drops_below_floor(kb, bath_quote):
    d = decide(req(bath_quote, already_used_tiers=(1, 2, 3, 4, 5)), kb)
    assert d.new_quote.base_rate == money(2500)
    assert d.new_quote.total >= money(7500)


# ==========================================================================
# R9 — ratchet, at the grant and at the gate
# ==========================================================================

def test_r9_price_cannot_climb_back_at_grant(kb, bath_quote):
    d = decide(
        req(bath_quote, already_used_tiers=(1, 2, 3, 4, 5), floor_reached=money(6000)),
        kb,
    )
    assert not d.allowed
    assert "R9" in d.denial_reason


def test_r9_equal_price_is_allowed(kb, bath_quote):
    d = decide(
        req(bath_quote, already_used_tiers=(1, 2, 3, 4, 5), floor_reached=money(7500)),
        kb,
    )
    assert d.allowed


def test_r9_no_prior_floor_is_unconstrained(kb, bath_quote):
    d = decide(req(bath_quote, already_used_tiers=(1, 2, 3, 4, 5), floor_reached=None), kb)
    assert d.allowed


def test_r9_gate_clamps_a_fresh_base_quote(kb, bath_quote):
    """The leak this gate exists for: base 10500 → concession 7500 → the
    orchestrator recalculates and the stateless engine says 10500 again.
    Step three must come back as 7500."""
    step1 = bath_quote
    assert step1.total == money(10500)

    granted = decide(req(step1, already_used_tiers=(1, 2, 3, 4, 5)), kb)
    assert granted.allowed
    step2 = granted.new_quote
    assert step2.total == money(7500)

    state = DialogConcessionState(
        base_price_quoted=True,
        used_tiers=frozenset({1, 2, 3, 4, 5, 6}),
        floor_reached=step2.total,
    )
    step3_raw = quote(PriceRequest("bath_russian", SAT, NOON, 3, 6), kb)
    assert step3_raw.total == money(10500)          # engine is stateless, as designed

    step3 = apply_dialog_floor(step3_raw, state)
    assert step3.total == money(7500)
    assert RATCHET_WARNING in step3.warnings


def test_r9_gate_is_a_noop_without_a_prior_floor(kb, bath_quote):
    state = DialogConcessionState(base_price_quoted=True)
    assert apply_dialog_floor(bath_quote, state) is bath_quote


def test_r9_gate_does_not_raise_a_cheaper_quote(kb):
    """Clamping is one-directional: a genuinely cheaper booking stays cheap."""
    cheap = quote(PriceRequest("bath_russian", THU, NOON, 3, 6), kb)   # 7500
    state = DialogConcessionState(base_price_quoted=True, floor_reached=money(9000))
    assert apply_dialog_floor(cheap, state).total == cheap.total


def test_r9_gate_ignores_non_ok_quotes(kb):
    # 14.2: бронь после 23:00 теперь единственный надёжный источник blocked.
    blocked = quote(PriceRequest("bath_russian", SAT, time(21, 0), 4, 6), kb)
    state = DialogConcessionState(base_price_quoted=True, floor_reached=money(1000))
    out = apply_dialog_floor(blocked, state)
    assert out.status == "blocked"
    assert out.total is None


def test_r9_gate_drops_the_stale_hourly_rate(kb, bath_quote):
    state = DialogConcessionState(base_price_quoted=True, floor_reached=money(7500))
    clamped = apply_dialog_floor(bath_quote, state)
    assert clamped.base_rate is None, "the old rate no longer explains the total"


# ==========================================================================
# R10 — limits
# ==========================================================================

def test_r10_daily_limit_denies(kb, bath_quote):
    d = decide(req(bath_quote, concessions_today=5), kb)
    assert not d.allowed
    assert "R10" in d.denial_reason


def test_r10_under_daily_limit_allows(kb, bath_quote):
    assert decide(req(bath_quote, concessions_today=4), kb).allowed


def test_r10_non_price_tiers_do_not_spend_the_dialog_limit(kb, bath_quote):
    """Question 13.5: an offer to move to a weekday costs no margin."""
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE), kb)
    assert d.allowed, "four non_price tiers must not exhaust a limit of 2"


def test_r10_two_price_tiers_exhaust_the_dialog_limit(kb, bath_quote):
    d = decide(req(bath_quote, already_used_tiers=(1, 2, 3, 4, 5, 6)), kb)
    assert not d.allowed
    assert "R10" in d.denial_reason


# ==========================================================================
# R11 — exchange is structural, validated at load time
# ==========================================================================

def test_r11_every_template_declares_a_valid_exchange(kb):
    policy = kb.concessions.policy
    for tier_id, template in policy.offer_templates.items():
        assert template.exchange in policy.exchange_options, tier_id
        assert template.exchange in policy.exchange_clauses, tier_id
        assert "{exchange_clause}" in template.text, tier_id


def test_r11_rendered_offer_contains_the_clause_text(kb, bath_quote):
    d = decide(req(bath_quote), kb)
    clause = kb.concessions.policy.exchange_clauses[d.exchange_required]
    assert clause in d.offer_template
    assert "{exchange_clause}" not in d.offer_template


def test_r11_template_without_placeholder_fails_at_load(kb):
    raw = copy.deepcopy(_raw())
    raw["concessions"]["policy"]["offer_templates"]["smaller_zone"]["text"] = "Могу сделать дешевле."
    with pytest.raises(Exception, match="exchange_clause"):
        KnowledgeBase.model_validate(raw)


def test_r11_exchange_outside_options_fails_at_load(kb):
    raw = copy.deepcopy(_raw())
    raw["concessions"]["policy"]["offer_templates"]["smaller_zone"]["exchange"] = "free_beer"
    with pytest.raises(Exception, match="exchange_options"):
        KnowledgeBase.model_validate(raw)


def test_r11_empty_exchange_fails_at_load(kb):
    raw = copy.deepcopy(_raw())
    raw["concessions"]["policy"]["offer_templates"]["smaller_zone"]["exchange"] = ""
    with pytest.raises(Exception):
        KnowledgeBase.model_validate(raw)


def test_r11_a_wording_only_check_would_have_passed_this(kb):
    """Regression guard for the old substring test: this sentence contains
    «если» and carries no exchange whatsoever."""
    raw = copy.deepcopy(_raw())
    raw["concessions"]["policy"]["offer_templates"]["smaller_zone"]["text"] = (
        "Если хотите, расскажу про купола."
    )
    with pytest.raises(Exception, match="exchange_clause"):
        KnowledgeBase.model_validate(raw)


# ==========================================================================
# R12 — logging
# ==========================================================================

def test_r12_denial_is_logged(kb, bath_quote, caplog):
    with caplog.at_level("INFO", logger="parmangal.concessions"):
        decide(req(bath_quote, observed_triggers=()), kb)
    assert any(r.name == "parmangal.concessions" for r in caplog.records)


def test_r12_grant_is_logged(kb, bath_quote, caplog):
    with caplog.at_level("INFO", logger="parmangal.concessions"):
        decide(req(bath_quote), kb)
    assert any(r.name == "parmangal.concessions" for r in caplog.records)


def test_r12_log_carries_every_declared_field(kb, bath_quote, caplog):
    with caplog.at_level("INFO", logger="parmangal.concessions"):
        decide(req(bath_quote), kb)
    record = next(r for r in caplog.records if r.name == "parmangal.concessions")
    for field_name in kb.concessions.policy.logging["fields"]:
        assert hasattr(record, field_name), f"log record is missing {field_name}"


def test_r12_log_records_provisional_and_basis(kb, bath_quote, caplog):
    with caplog.at_level("INFO", logger="parmangal.concessions"):
        decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE), kb)
    record = next(r for r in caplog.records if r.name == "parmangal.concessions")
    assert record.provisional_policy is True
    assert record.revenue_delta_basis in ("base_rate", "policy_minimum")


# ==========================================================================
# revenue_delta accounting
# ==========================================================================

def test_min_hours_concession_has_non_zero_revenue_delta(kb):
    """Dropping 3 hours to 2 costs one hour at the going rate. Recording
    zero would understate the loss and the concession-rate alert would never
    fire on this tier."""
    short = quote(PriceRequest("bath_russian", SAT, NOON, 2, 6), kb)
    assert short.requires_concession_tier == 5
    d = decide(req(short, already_used_tiers=ALL_NON_PRICE), kb)
    assert d.allowed
    assert d.tier == 5
    assert d.revenue_delta == money(-3500)      # 1 hour × 3500
    assert d.revenue_delta_basis == "policy_minimum"


def test_min_hours_offer_names_both_numbers(kb):
    short = quote(PriceRequest("bath_russian", SAT, NOON, 2, 6), kb)
    d = decide(req(short, already_used_tiers=ALL_NON_PRICE), kb)
    assert "3" in d.offer_template and "2" in d.offer_template


def test_non_price_tier_has_zero_revenue_delta_and_no_basis(kb, bath_quote):
    d = decide(req(bath_quote), kb)
    assert d.kind == "non_price"
    assert d.revenue_delta == Decimal("0")
    assert d.revenue_delta_basis is None


def test_rate_concession_delta_is_negative(kb, bath_quote):
    d = decide(req(bath_quote, already_used_tiers=ALL_NON_PRICE), kb)
    assert d.revenue_delta < 0


# ==========================================================================
# Applicability predicates
# ==========================================================================

def test_applicability_smaller_zone_true_when_a_cheaper_one_fits(kb, bath_quote):
    """Баня на 6 гостей в выходной стоит 3500/час; купол на 7-10 мест —
    1500/час, значит дешевле и вмещает. Ступень применима."""
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "smaller_zone")
    assert is_applicable(tier, req(bath_quote), kb)


def test_applicability_smaller_zone_false_for_the_cheapest_zone(kb):
    """У самого дешёвого варианта предлагать нечего."""
    dome = quote(PriceRequest("dome_bags", SAT, NOON, 3, 6), kb)
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "smaller_zone")
    assert not is_applicable(tier, req(dome), kb)


def test_applicability_day_package_requires_a_package(kb, bath_quote):
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "day_package")
    assert not is_applicable(tier, req(bath_quote), kb)


def test_applicability_day_package_true_when_cheaper(kb, dome_monday_7h):
    """7 hours bills as 6 (5+1 promo) = 6000, so the 5000 package wins."""
    assert dome_monday_7h.total == money(6000)
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "day_package")
    assert is_applicable(tier, req(dome_monday_7h, booking_date=MON), kb)


def test_applicability_day_package_false_when_hourly_is_cheaper(kb):
    """3 hours on a Monday is 3000 — cheaper than the 5000 package, so
    offering the package would be a worse deal, not a concession."""
    short = quote(PriceRequest("dome_bags", MON, time(10, 0), 3, 6), kb)
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "day_package")
    assert not is_applicable(tier, req(short, booking_date=MON), kb)


def test_applicability_promo_tier_false_when_promo_already_applied(kb):
    with_promo = quote(PriceRequest("dome_bags", SAT, time(10, 0), 6, 6), kb)
    assert with_promo.applied_promo == "sixth_hour_free"
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "apply_promo")
    assert not is_applicable(tier, req(with_promo), kb)


def test_applicability_promo_tier_true_for_bath_now(kb, bath_quote):
    """1.1/1.2: у бань появились акции, значит ступень «предложить акцию»
    снова применима — раньше предлагать было нечего."""
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "apply_promo")
    assert is_applicable(tier, req(bath_quote), kb)


def test_applicability_shift_to_weekday_false_on_a_weekday(kb):
    weekday_quote = quote(PriceRequest("bath_russian", THU, NOON, 3, 6), kb)
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "shift_to_weekday")
    assert not is_applicable(tier, req(weekday_quote, booking_date=THU), kb)


def test_unknown_predicate_raises(kb, bath_quote):
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "shift_to_weekday")
    broken = tier.model_copy(deep=True)
    broken.applicability.requires = ["no_such_predicate"]
    with pytest.raises(ValueError, match="unknown applicability predicate"):
        is_applicable(broken, req(bath_quote), kb)


# ==========================================================================
# Cross-cutting
# ==========================================================================

def test_concession_refused_on_non_ok_quote(kb):
    blocked = quote(PriceRequest("bath_russian", SAT, time(21, 0), 4, 6), kb)
    assert blocked.status == "blocked"
    assert not decide(req(blocked), kb).allowed


def test_dialog_state_defaults_are_conservative():
    state = DialogConcessionState()
    assert state.base_price_quoted is False
    assert state.used_tiers == frozenset()
    assert state.floor_reached is None


def test_request_has_no_is_holiday_field():
    """Правка 4: the gate must not accept its own bypass switch."""
    assert "is_holiday" not in ConcessionRequest.__dataclass_fields__


def test_decision_is_deterministic(kb, bath_quote):
    first = decide(req(bath_quote), kb)
    second = decide(req(bath_quote), kb)
    assert (first.allowed, first.tier, first.offer_template) == (
        second.allowed, second.tier, second.offer_template,
    )


# ==========================================================================
# Ступень 7 — суточная ставка домика (ответ 2.1)
# ==========================================================================

@pytest.fixture(scope="module")
def house_weekday_quote(kb):
    """Домик в будни считается (7000). Выходной тариф пока блокируется 14.1."""
    return quote(PriceRequest("house_relax", THU, None, None, 8), kb)


def test_house_daily_concession_drops_to_the_floor(kb_generous, house_weekday_quote):
    """2.1: пол 9500. Здесь база 7000 и уже ниже пола, значит уступать
    нечего — движок обязан отказать, а не «поднять» цену до 9500."""
    d = decide(
        req(house_weekday_quote, booking_date=THU,
            already_used_tiers=(1, 2, 3, 4, 5, 6), occupancy_ratio=0.2),
        kb_generous,
    )
    assert not d.allowed
    assert "уступать нечего" in d.denial_reason


def test_house_daily_tier_is_reachable_and_logs_the_delta(kb_generous, kb):
    """Проверяем саму механику ступени на цене выше пола."""
    from dataclasses import replace as dc_replace

    inflated = dc_replace(
        quote(PriceRequest("house_relax", THU, None, None, 8), kb),
        total=money(15000),
    )
    d = decide(
        req(inflated, booking_date=THU,
            already_used_tiers=(1, 2, 3, 4, 5, 6), occupancy_ratio=0.2),
        kb_generous,
    )
    assert d.allowed
    assert d.tier == 7
    assert d.new_quote.total == money(9500)
    # Анкор 15000 → пол 9500: самая крупная уступка в проекте.
    assert d.revenue_delta == money(-5500)
    assert d.revenue_delta_basis == "base_rate"
    assert d.provisional_policy is True      # порог загрузки пока наш


def test_house_daily_offer_names_both_numbers(kb_generous, kb):
    from dataclasses import replace as dc_replace

    inflated = dc_replace(
        quote(PriceRequest("house_relax", THU, None, None, 8), kb), total=money(15000)
    )
    d = decide(
        req(inflated, booking_date=THU,
            already_used_tiers=(1, 2, 3, 4, 5, 6), occupancy_ratio=0.2),
        kb_generous,
    )
    assert "9500" in d.offer_template
    assert "15000" in d.offer_template
    assert "если" in d.offer_template.lower()


def test_daily_tier_not_applicable_to_hourly_zone(kb, bath_quote):
    tier = next(t for t in kb.concessions.policy.ladder if t.id == "reduce_daily_rate")
    assert not is_applicable(tier, req(bath_quote), kb)


def test_grill_house_min_hours_floor_is_confirmed(kb):
    """4.2: гриль-домик тоже может уйти на 2 часа при низкой загрузке."""
    short = quote(PriceRequest("grill_house", SAT, NOON, 2, 6), kb)
    assert short.requires_concession_tier == 5
    d = decide(req(short, already_used_tiers=ALL_NON_PRICE, occupancy_ratio=0.2), kb)
    assert d.allowed
    assert d.tier == 5


def test_bath_weekday_min_hours_floor_now_confirmed(kb):
    """1.3: заказчик не ограничивал день недели, значит будни тоже."""
    short = quote(PriceRequest("bath_russian", THU, NOON, 2, 6), kb)
    d = decide(
        req(short, booking_date=THU, already_used_tiers=ALL_NON_PRICE, occupancy_ratio=0.2),
        kb,
    )
    assert d.allowed
    assert d.tier == 5
