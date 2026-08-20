"""Concession engine — decides whether the agent may bend a price or a rule.

The model does NOT decide this. The model only reports what it observed
(«клиент сказал, что дорого»), and this code decides. Every rule below is
read from app/kb/concessions.yaml; the offer wording comes from templates in
that file, never from the model.

Rules implemented (R1-R12):
    R1  no trigger fired            -> denied, always
    R2  base price not yet quoted   -> denied (first price is always the base one)
    R3  tiers ascend, no skipping; an inapplicable tier is SKIPPED rather
        than blocking the ladder behind it
    R4  a price tier unlocks once every APPLICABLE non_price tier has been
        used or skipped — or once max_non_price_attempts_before_price
        real attempts have been made
    R5  one tier is granted at most once per dialog
    R6  all conditions must hold (slot free, not a holiday, date near enough)
    R7  price tiers are gated by OCCUPANCY, not by proximity of the date
        (client answer 2.1). Unknown occupancy does not forbid the
        concession — it routes the decision to a human operator.
    R7-legacy  (superseded) price tiers were forbidden when days_until_date was
        absent or disputed-without-provisional; a provisional value permits
        them but stamps every grant with provisional_policy=True
    R8  price never drops below a confirmed floor; a null floor forbids the tier
    R9  ratchet — a price already lowered in this dialog never goes back up
        (see also app/pricing/quote_gate.py, which enforces this for every
        quote, not just at the moment of granting)
    R10 per-dialog and per-day limits
    R11 require_exchange -> the offer text always carries a condition,
        checked structurally at KB load time (loader.py)
    R12 every decision, allowing or denying, is logged
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date as DateType
from decimal import Decimal
from typing import Any, Literal, Optional

from app.kb.loader import KnowledgeBase, LadderTier
from app.pricing.engine import Money, PriceQuote, money

logger = logging.getLogger("parmangal.concessions")

Kind = Literal["non_price", "price"]
DeltaBasis = Literal["policy_minimum", "base_rate"]


@dataclass(frozen=True)
class DialogConcessionState:
    """Dialog-scoped memory. Not in the original spec, but R2, R3, R5 and R9
    are unimplementable without it — a concession decision depends on what
    already happened in this conversation, not just on this request."""

    base_price_quoted: bool = False
    used_tiers: frozenset[int] = frozenset()
    # R9 ratchet: the lowest total already promised in this dialog.
    floor_reached: Optional[Money] = None


@dataclass(frozen=True)
class ConcessionRequest:
    """Provenance of each field matters — this is a gate, and a gate whose
    inputs are controlled by the party it restrains is not a gate:

    observed_triggers, client_constraints
        Come from the model via the orchestrator. That is fine: they only
        OPEN a check, they never decide its outcome.

    slot_confirmed_free
        MUST come from the booking provider (YCLIENTS). Never from the
        model — a hallucinated free slot would hand out real discounts.

    booking_date
        Comes from the orchestrator but is verified here: holiday status is
        derived from the knowledge base, not accepted as a caller-supplied
        boolean. (An earlier version took `is_holiday: bool`, which let the
        caller switch the rule off by passing False.)
    """

    dialog_id: str
    quote: PriceQuote
    observed_triggers: tuple[str, ...]
    days_until_date: int
    slot_confirmed_free: bool
    booking_date: DateType
    # Доля занятых зон на эту дату, 0.0-1.0. Источник — провайдер броней,
    # НИКОГДА не модель: выдуманная загрузка раздавала бы реальные скидки.
    # None означает «посчитать неоткуда» (в YCLIENTS сейчас 0 зон из 10) —
    # тогда ценовая уступка уходит на одобрение оператору, а не запрещается.
    occupancy_ratio: Optional[float] = None
    already_used_tiers: tuple[int, ...] = ()
    concessions_today: int = 0
    client_constraints: frozenset[str] = frozenset()
    base_price_quoted: bool = True
    floor_reached: Optional[Money] = None


@dataclass(frozen=True)
class ConcessionDecision:
    allowed: bool
    tier: Optional[int] = None
    kind: Optional[Kind] = None
    new_quote: Optional[PriceQuote] = None
    exchange_required: str = ""
    offer_template: str = ""
    revenue_delta: Money = Decimal("0")
    revenue_delta_basis: Optional[DeltaBasis] = None
    denial_reason: Optional[str] = None
    blocking_question_ids: tuple[str, ...] = ()
    skipped_tiers: tuple[int, ...] = ()
    # True when the grant rests on a threshold WE set provisionally rather
    # than one the client confirmed.
    provisional_policy: bool = False
    # True когда загрузку посчитать неоткуда: решение принимает оператор
    # кнопкой в Telegram, а не движок. Временный режим — исчезнет, как только
    # каталог YCLIENTS заполнится и occupancy_ratio начнёт приходить.
    requires_operator_approval: bool = False


# --------------------------------------------------------------------------
# Logging (R12)
# --------------------------------------------------------------------------

def _needs_operator(
    req: ConcessionRequest,
    tier: LadderTier,
    reason: str,
    *,
    provisional: bool = False,
    skipped: tuple[int, ...] = (),
) -> ConcessionDecision:
    """Скидку нельзя ни выдать автоматически, ни отклонить — решает человек.

    Отличается от `_deny` принципиально: отказ означает «скидки не будет»,
    а это — «пока не знаем, спросите оператора». Агент в этот момент НЕ
    предлагает скидку клиенту и не отказывает, а продолжает разговор.
    """
    decision = ConcessionDecision(
        allowed=False,
        tier=tier.tier,
        kind=tier.type,
        denial_reason=reason,
        requires_operator_approval=True,
        provisional_policy=provisional,
        skipped_tiers=skipped,
    )
    _log(req, decision)
    return decision


def _log(req: ConcessionRequest, decision: ConcessionDecision) -> None:
    logger.info(
        "concession decision",
        extra={
            "dialog_id": req.dialog_id,
            "zone": req.quote.zone_id,
            "tier": decision.tier,
            "trigger": ",".join(req.observed_triggers) or None,
            "base_price": str(req.quote.total) if req.quote.total is not None else None,
            "final_price": (
                str(decision.new_quote.total)
                if decision.new_quote is not None and decision.new_quote.total is not None
                else None
            ),
            "revenue_delta": str(decision.revenue_delta),
            "revenue_delta_basis": decision.revenue_delta_basis,
            "exchange_given": decision.exchange_required or None,
            "allowed": decision.allowed,
            "requires_operator_approval": decision.requires_operator_approval,
            "occupancy_ratio": req.occupancy_ratio,
            "denial_reason": decision.denial_reason,
            "skipped_tiers": list(decision.skipped_tiers),
            "provisional_policy": decision.provisional_policy,
        },
    )


def _deny(req: ConcessionRequest, reason: str, tier: Optional[int] = None,
          question_ids: tuple[str, ...] = (), skipped: tuple[int, ...] = ()) -> ConcessionDecision:
    decision = ConcessionDecision(
        allowed=False,
        tier=tier,
        denial_reason=reason,
        blocking_question_ids=question_ids,
        skipped_tiers=skipped,
    )
    _log(req, decision)
    return decision


# --------------------------------------------------------------------------
# Applicability predicates
# --------------------------------------------------------------------------

def _zone(kb: KnowledgeBase, zone_id: Optional[str]):
    return next((z for z in kb.catalog.zones if z.id == zone_id), None)


def _zone_category(kb: KnowledgeBase, zone_id: Optional[str]) -> Optional[str]:
    zone = _zone(kb, zone_id)
    return zone.category.value if zone else None


def _hourly_rate_of(zone, day_type: Optional[str]) -> Optional[Money]:
    """A zone's plain hourly rate, or None when it is absent or disputed."""
    key = "weekday_per_hour" if day_type == "weekday" else "weekend_per_hour"
    node = zone.pricing.get(key)
    if not isinstance(node, dict) or node.get("value") is None:
        return None
    return money(node["value"])


def _check_day_type_is_weekend(req: ConcessionRequest, kb: KnowledgeBase) -> bool:
    return req.quote.day_type == "weekend"


def _check_cheaper_zone_exists(req: ConcessionRequest, kb: KnowledgeBase) -> bool:
    """Is there another zone that fits the party AND costs less? Zones whose
    capacity or rate is itself an open question do not count — offering one
    would just move the guess downstream."""
    current_rate = req.quote.base_rate
    if current_rate is None:
        return False
    guests = req.quote.guests
    for zone in kb.catalog.zones:
        if zone.id == req.quote.zone_id:
            continue
        if not zone.capacity.is_resolved():
            continue
        if guests is not None and zone.capacity.value < guests:
            continue
        rate = _hourly_rate_of(zone, req.quote.day_type)
        if rate is not None and rate < current_rate:
            return True
    return False


def _check_day_package_beneficial(req: ConcessionRequest, kb: KnowledgeBase) -> bool:
    zone = _zone(kb, req.quote.zone_id)
    if zone is None or not zone.day_package:
        return False
    days = zone.day_package.get("days")
    if days:
        token = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[req.booking_date.weekday()]
        if token not in days:
            return False
    price_node = zone.day_package.get("price")
    if not isinstance(price_node, dict) or price_node.get("value") is None:
        return False
    if req.quote.total is None:
        return False
    return money(price_node["value"]) < req.quote.total


def _check_unused_promo_exists(req: ConcessionRequest, kb: KnowledgeBase) -> bool:
    if req.quote.applied_promo is not None:
        return False
    for promo in kb.promos.promos:
        if promo.type == "day_package":
            continue
        if req.quote.zone_id not in promo.applies_to_zones:
            continue
        if promo.disputed_zones and req.quote.zone_id in promo.disputed_zones.value:
            continue
        return True
    return False


def _check_hours_below_policy_minimum(req: ConcessionRequest, kb: KnowledgeBase) -> bool:
    return req.quote.requires_concession_tier is not None


def _check_hourly_booking(req: ConcessionRequest, kb: KnowledgeBase) -> bool:
    return req.quote.base_rate is not None and req.quote.billable_hours is not None


def _check_daily_booking(req: ConcessionRequest, kb: KnowledgeBase) -> bool:
    """Суточная зона: ставки за час нет, есть цена за день."""
    return _zone_category(kb, req.quote.zone_id) in ("house", "yurt")


_PREDICATES = {
    "day_type_is_weekend": _check_day_type_is_weekend,
    "cheaper_zone_exists": _check_cheaper_zone_exists,
    "day_package_beneficial": _check_day_package_beneficial,
    "unused_promo_exists": _check_unused_promo_exists,
    "hours_below_policy_minimum": _check_hours_below_policy_minimum,
    "hourly_booking": _check_hourly_booking,
    "daily_booking": _check_daily_booking,
}


def is_applicable(tier: LadderTier, req: ConcessionRequest, kb: KnowledgeBase) -> bool:
    """A tier is applicable unless a client constraint rules it out or one of
    its named predicates fails."""
    if set(tier.applicability.blocked_by_constraints) & req.client_constraints:
        return False
    for name in tier.applicability.requires:
        predicate = _PREDICATES.get(name)
        if predicate is None:
            raise ValueError(f"unknown applicability predicate {name!r} in tier {tier.id!r}")
        if not predicate(req, kb):
            return False
    return True


# --------------------------------------------------------------------------
# Floors
# --------------------------------------------------------------------------

def _resolve_floor(kb: KnowledgeBase, tier: LadderTier, zone_id: Optional[str],
                   day_type: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    category = _zone_category(kb, zone_id)
    for override in tier.confirmed_overrides or []:
        scope = override.get("scope", {})
        if scope.get("zone_category") != category:
            continue
        # Отсутствие day_type в области действия означает «любой день».
        # Так заказчик и ответил про минимум часов (1.3, 4.2): день недели он
        # не ограничивал. Требовать точного совпадения с None означало бы
        # никогда не находить такой пол.
        scoped_day = scope.get("day_type")
        if scoped_day is not None and scoped_day != day_type:
            continue
        return override.get("floor"), None

    generic = tier.floor or {}
    if generic.get("value") is not None:
        return generic, None
    question_id = (generic.get("disputed") or {}).get("question_id")
    return None, question_id


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def decide(req: ConcessionRequest, kb: KnowledgeBase) -> ConcessionDecision:
    policy = kb.concessions.policy

    # R1 — nothing to react to.
    valid_triggers = {t.id for t in policy.triggers}
    if not [t for t in req.observed_triggers if t in valid_triggers]:
        return _deny(req, "R1: ни один триггер не сработал")

    # R2 — the first price a client hears is always the base price.
    if policy.never_open_with_concession and not req.base_price_quoted:
        return _deny(req, "R2: базовая цена ещё не называлась в диалоге")

    # A concession only makes sense on top of a real quote.
    if req.quote.status != "ok" or req.quote.total is None:
        return _deny(req, f"Базовый расчёт не в статусе ok (status={req.quote.status})")

    # R10 — limits. Scope note (question 13.5): counting EVERY granted tier
    # against max_concessions_per_dialog=2 would make tiers 5-6 unreachable,
    # since R4 demands the non_price tiers first. Non-price offers do not
    # spend margin, so by default only price tiers count.
    used = set(req.already_used_tiers)
    price_tiers = {t.tier for t in policy.ladder if t.type == "price"}
    non_price_tiers = {t.tier for t in policy.ladder if t.type == "non_price"}
    counted = used & price_tiers if policy.limits_count_only_price_tiers else used
    if len(counted) >= policy.max_concessions_per_dialog:
        return _deny(req, f"R10: исчерпан лимит уступок за диалог ({policy.max_concessions_per_dialog})")
    if req.concessions_today >= policy.max_concessions_per_day:
        return _deny(req, f"R10: исчерпан дневной лимит уступок ({policy.max_concessions_per_day})")

    # R6 — conditions that gate every tier. Holiday status is DERIVED here,
    # never taken from the caller.
    if not req.slot_confirmed_free:
        return _deny(req, "R6: слот не подтверждён свободным")
    if policy.conditions.get("not_holiday") and kb.catalog.constants.holidays.contains(req.booking_date):
        return _deny(req, "R6: праздничная дата (определено движком по constants.holidays)")

    # ---- tier selection: used | skipped | candidate ----------------------
    attempts_made = len(used & non_price_tiers)
    attempts_exhausted = attempts_made >= policy.max_non_price_attempts_before_price

    skipped: list[int] = []
    candidate: Optional[LadderTier] = None

    for tier in sorted(policy.ladder, key=lambda t: t.tier):
        if tier.tier in used:
            continue
        if tier.type == "non_price" and attempts_exhausted:
            # Enough real attempts to steer away from a discount have been
            # made; further counter-offers just annoy the client.
            skipped.append(tier.tier)
            continue
        if not is_applicable(tier, req, kb):
            skipped.append(tier.tier)
            continue
        candidate = tier
        break

    if candidate is None:
        return _deny(
            req,
            "R3/R5: не осталось применимых неиспользованных ступеней",
            skipped=tuple(skipped),
        )

    if candidate.type == "non_price":
        return _grant_non_price(req, kb, candidate, tuple(skipped))

    # ---- price tiers -----------------------------------------------------

    # R4 — every applicable non_price tier must be used or skipped by now.
    # The ordered loop above guarantees it; this is the explicit guard.
    unresolved_non_price = [
        t.tier for t in policy.ladder
        if t.type == "non_price" and t.tier not in used and t.tier not in skipped
    ]
    if unresolved_non_price:
        return _deny(
            req,
            f"R4: не исчерпаны применимые неценовые ступени {sorted(unresolved_non_price)}",
            tier=candidate.tier,
            skipped=tuple(skipped),
        )

    # R7 — ЗАГРУЗКА, а не близость даты.
    #
    # Заказчик описал условие так: «если за 2 дня всё свободно — сдаём за
    # 9 500; если за два дня одна банька осталась — сдаём за 15 тыс.». То есть
    # решает не «дата близко», а «дата близко И свободно». Разница проявляется
    # ровно в том случае, который приносит деньги: одна свободная зона из
    # десяти скидку давать не должна.
    threshold = policy.conditions.get("max_occupancy_ratio", {})
    provisional = bool(threshold.get("provisional"))
    limit = threshold.get("value")
    if limit is None:
        question_id = (threshold.get("disputed") or {}).get("question_id") or threshold.get("question_id")
        return _deny(
            req,
            "R7: порог загрузки не задан — ценовые ступени запрещены",
            tier=candidate.tier,
            question_ids=(question_id,) if question_id else (),
            skipped=tuple(skipped),
        )

    if req.occupancy_ratio is None:
        # Загрузку посчитать неоткуда (в YCLIENTS сейчас 0 зон из 10).
        # Это НЕ отказ: решение уходит оператору кнопкой в Telegram.
        # Режим временный — исчезнет, когда каталог заполнится.
        return _needs_operator(
            req,
            candidate,
            "Загрузка на эту дату неизвестна — нужно ваше решение по скидке",
            provisional=provisional,
            skipped=tuple(skipped),
        )

    if req.occupancy_ratio > limit:
        return _deny(
            req,
            f"R7: загрузка {req.occupancy_ratio:.0%} выше порога {limit:.0%} — "
            "дата и так продаётся, скидка не нужна",
            tier=candidate.tier,
            skipped=tuple(skipped),
        )

    # R8 — floor.
    floor, floor_question = _resolve_floor(kb, candidate, req.quote.zone_id, req.quote.day_type)
    if floor is None:
        return _deny(
            req,
            f"R8: пол для ступени {candidate.id} в этой зоне и дне не подтверждён",
            tier=candidate.tier,
            question_ids=(floor_question,) if floor_question else (),
            skipped=tuple(skipped),
        )

    return _grant_price(req, kb, candidate, floor, tuple(skipped), provisional)


# --------------------------------------------------------------------------
# Granting
# --------------------------------------------------------------------------

def _render_template(kb: KnowledgeBase, tier: LadderTier, **values: Any) -> tuple[str, str]:
    """Fill a template. The exchange clause comes from the KB's own
    vocabulary, so the promised condition can never drift from the list of
    exchanges the policy allows."""
    template = kb.concessions.policy.offer_templates[tier.id]
    values = dict(values)
    values["exchange_clause"] = kb.concessions.policy.exchange_clauses[template.exchange]
    text = template.text
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text, template.exchange


def _grant_non_price(req: ConcessionRequest, kb: KnowledgeBase, tier: LadderTier,
                     skipped: tuple[int, ...]) -> ConcessionDecision:
    text, exchange = _render_template(kb, tier)
    decision = ConcessionDecision(
        allowed=True,
        tier=tier.tier,
        kind="non_price",
        new_quote=None,          # an offer to change the booking, not a recalc
        exchange_required=exchange,
        offer_template=text,
        revenue_delta=Decimal("0"),
        revenue_delta_basis=None,
        skipped_tiers=skipped,
    )
    _log(req, decision)
    return decision


def _policy_min_hours(kb: KnowledgeBase, zone_id: Optional[str]) -> Optional[int]:
    """The strictest minimum the zone might have. When the minimum itself is
    disputed we take the largest variant, so the recorded loss is never
    understated."""
    zone = _zone(kb, zone_id)
    if zone is None:
        return None
    node = zone.pricing.get("min_hours")
    if not isinstance(node, dict):
        return None
    if node.get("value") is not None:
        return int(node["value"])
    variants = (node.get("disputed") or {}).get("variants") or []
    ints = [v for v in variants if isinstance(v, int)]
    return max(ints) if ints else None


def _grant_price(req: ConcessionRequest, kb: KnowledgeBase, tier: LadderTier,
                 floor: dict, skipped: tuple[int, ...],
                 provisional: bool) -> ConcessionDecision:
    quote = req.quote

    if tier.id == "reduce_hourly_rate":
        floor_rate = money(floor["weekend_per_hour"]["value"])
        if quote.base_rate is None or quote.billable_hours is None:
            return _deny(req, "Нет базовой ставки в расчёте", tier=tier.tier, skipped=skipped)
        if floor_rate >= quote.base_rate:
            return _deny(req, "R8: пол не ниже действующей ставки — уступать нечего",
                         tier=tier.tier, skipped=skipped)

        new_total = money(floor_rate * Decimal(quote.billable_hours))

        # R9 — ratchet at the moment of granting. (quote_gate.py enforces the
        # same floor on every subsequent quote.)
        if req.floor_reached is not None and new_total > req.floor_reached:
            return _deny(
                req,
                f"R9: храповик — цена уже опускалась до {req.floor_reached}, поднимать обратно нельзя",
                tier=tier.tier, skipped=skipped,
            )

        from dataclasses import replace as _replace

        new_quote = _replace(
            quote,
            total=new_total,
            warnings=quote.warnings + ("Цена снижена по ступени уступки.",),
            human_readable=f"{quote.billable_hours} ч × {floor_rate} ₽ = {new_total} ₽",
            base_rate=floor_rate,
        )
        text, exchange = _render_template(
            kb, tier, new_rate=floor_rate, base_rate=quote.base_rate
        )
        decision = ConcessionDecision(
            allowed=True,
            tier=tier.tier,
            kind="price",
            new_quote=new_quote,
            exchange_required=exchange,
            offer_template=text,
            revenue_delta=money(new_total - quote.total),
            revenue_delta_basis="base_rate",
            skipped_tiers=skipped,
            provisional_policy=provisional,
        )
        _log(req, decision)
        return decision

    if tier.id == "reduce_min_hours":
        granted_min = int(floor["min_hours"]["value"])
        policy_min = _policy_min_hours(kb, quote.zone_id)

        # Dropping the minimum from 3 hours to 2 costs one hour at the going
        # rate. Recording zero here would understate the loss and the
        # alert_if_concession_rate_above threshold would never fire on this
        # tier. The counterfactual is arguable (the client might not have
        # bought three hours at all), but the accounting must be uniform or
        # the tiers are not comparable.
        delta = Decimal("0")
        if policy_min is not None and quote.base_rate is not None and policy_min > granted_min:
            delta = money(-(quote.base_rate * Decimal(policy_min - granted_min)))

        text, exchange = _render_template(
            kb, tier,
            new_min_hours=granted_min,
            base_min_hours=policy_min if policy_min is not None else granted_min,
        )
        decision = ConcessionDecision(
            allowed=True,
            tier=tier.tier,
            kind="price",
            new_quote=None,       # the rate is unchanged; only the minimum bends
            exchange_required=exchange,
            offer_template=text,
            revenue_delta=delta,
            revenue_delta_basis="policy_minimum",
            skipped_tiers=skipped,
            provisional_policy=provisional,
        )
        _log(req, decision)
        return decision

    if tier.id == "reduce_daily_rate":
        # Самая крупная уступка в проекте: анкор 15 000 → пол 9 500, дельта
        # до −5 500 ₽. Обязана логироваться и попадать в дневную сводку
        # оператору отдельной строкой.
        floor_rate = money(floor["per_day"]["value"])
        anchor_node = _anchor_for(kb, tier, quote.zone_id, quote.day_type)
        anchor = money(anchor_node) if anchor_node is not None else quote.total

        if quote.total is None:
            return _deny(req, "Нет базовой суммы в расчёте", tier=tier.tier, skipped=skipped)
        if floor_rate >= quote.total:
            return _deny(
                req, "R8: пол не ниже действующей цены — уступать нечего",
                tier=tier.tier, skipped=skipped,
            )

        # R9 — храповик.
        if req.floor_reached is not None and floor_rate > req.floor_reached:
            return _deny(
                req,
                f"R9: храповик — цена уже опускалась до {req.floor_reached}",
                tier=tier.tier, skipped=skipped,
            )

        new_quote = replace(
            quote,
            total=floor_rate,
            warnings=quote.warnings + ("Суточная ставка снижена по ступени уступки.",),
            human_readable=f"Домик на сутки — {floor_rate} ₽",
        )
        text, exchange = _render_template(
            kb, tier, new_rate=floor_rate, base_rate=anchor
        )
        decision = ConcessionDecision(
            allowed=True,
            tier=tier.tier,
            kind="price",
            new_quote=new_quote,
            exchange_required=exchange,
            offer_template=text,
            revenue_delta=money(floor_rate - anchor),
            revenue_delta_basis="base_rate",
            skipped_tiers=skipped,
            provisional_policy=provisional,
        )
        _log(req, decision)
        return decision

    return _deny(req, f"Неизвестная ценовая ступень {tier.id!r}", tier=tier.tier, skipped=skipped)


def _anchor_for(kb: KnowledgeBase, tier: LadderTier, zone_id: Optional[str],
                day_type: Optional[str]) -> Optional[int]:
    """Анкор — цена, которую агент называет ПЕРВОЙ (ответ 2.1: 15 000)."""
    category = _zone_category(kb, zone_id)
    for override in tier.confirmed_overrides or []:
        scope = override.get("scope", {})
        if scope.get("zone_category") != category:
            continue
        if scope.get("day_type") not in (None, day_type):
            continue
        anchor = override.get("anchor") or {}
        node = anchor.get("per_day") or {}
        if node.get("value") is not None:
            return int(node["value"])
    return None
