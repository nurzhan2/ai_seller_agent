"""Base price calculation for ПарМангал.

Pure function: no network, no DB, no LLM. Every rule is read from
app/kb/*.yaml — nothing about prices, minimums, capacities or promos is
hardcoded here.

THE CARDINAL RULE
    If any field required for a calculation is marked `disputed` in the KB
    (i.e. `value: null`), this module returns status="blocked", total=None
    and the question_ids that are missing. It never substitutes the price
    list value, never averages variants, never prefers the "more official"
    source. A blocked quote sends the agent to a human.

FOUR STATUSES — they are not interchangeable, and the agent behaves
differently for each:
    ok           — priced. `warnings` may still be non-empty.
    blocked      — input was fine, the KB has a hole → escalate to a manager.
    invalid      — the request itself is impossible (unknown zone, 40 guests
                   in a 10-person dome) → tell the client why, offer an
                   alternative.
    needs_input  — the client simply hasn't told us something yet (start
                   time, guest count for the tent) → ask the client.

Readiness is a property of the REQUEST, not of the zone: bath_knight prices
fine for 4 weekday hours and blocks at 5 (the special-rate calculation mode
is unresolved). Never cache readiness by zone_id.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date as DateType, time as TimeType, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional, Sequence

from app.kb.loader import KnowledgeBase, Zone

Money = Decimal
_RUBLE = Decimal("1")

Status = Literal["ok", "blocked", "invalid", "needs_input"]
DayType = Literal["weekday", "weekend"]

# Weekday index (Mon=0) -> the token used in catalog.yaml constants.
_DOW_TOKENS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def money(value: int | str | Decimal) -> Money:
    """Round to whole rubles, ROUND_HALF_UP. Decimal in, Decimal out —
    there is deliberately no float anywhere in this module."""
    return Decimal(value).quantize(_RUBLE, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Request / response
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceRequest:
    zone_id: str
    date: DateType
    start_time: Optional[TimeType] = None
    hours: Optional[int] = None          # None = day-package request
    guests: Optional[int] = None
    extras: tuple[tuple[str, int], ...] = ()
    promo_hint: Optional[str] = None     # "день рождения" etc.


@dataclass(frozen=True)
class PriceLine:
    code: str
    description: str
    qty: Decimal
    unit_price: Money
    amount: Money
    source_field: str      # provenance: which KB field produced this line


@dataclass(frozen=True)
class PriceQuote:
    status: Status
    total: Optional[Money] = None
    lines: tuple[PriceLine, ...] = ()
    applied_promo: Optional[str] = None
    alternative_promos: tuple[str, ...] = ()
    prepayment: Optional[Money] = None
    blocked_reason: Optional[str] = None
    blocking_question_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    human_readable: str = ""
    # --- extensions beyond the original spec -----------------------------
    day_type: Optional[DayType] = None
    zone_id: Optional[str] = None
    # Set when the arithmetic is fine but SELLING it needs a concession
    # (e.g. fewer hours than the zone minimum). The concession engine, not
    # this module, decides whether that is allowed.
    requires_concession_tier: Optional[int] = None
    # Questions that do not block the calculation but inform the decision.
    # `blocking_question_ids` stays exclusively for status="blocked".
    advisory_question_ids: tuple[str, ...] = ()
    # status="needs_input" only: what the client still has to tell us.
    missing_fields: tuple[str, ...] = ()
    # status="invalid" only: zones that could host this party instead.
    suggested_alternatives: tuple[str, ...] = ()
    # Base hourly rate actually used, for the concession engine's ratchet.
    base_rate: Optional[Money] = None
    billable_hours: Optional[int] = None
    occupied_hours: Optional[int] = None
    # Echoed from the request so downstream rules (e.g. "is there a cheaper
    # zone that still fits this party?") do not need the original request.
    guests: Optional[int] = None


# --------------------------------------------------------------------------
# KB field access
# --------------------------------------------------------------------------

class _Missing:
    """A KB field that exists but is unresolved (`value: null`)."""

    __slots__ = ("question_id", "path")

    def __init__(self, question_id: Optional[str], path: str):
        self.question_id = question_id
        self.path = path


def _read(node: Any, path: str) -> Any | _Missing | None:
    """Read a DisputedValue-shaped node. Returns the value, a _Missing if it
    is disputed, or None if the node is absent entirely."""
    if node is None:
        return None
    if not isinstance(node, dict):
        return node
    if node.get("value") is not None:
        return node["value"]
    disputed = node.get("disputed")
    if isinstance(disputed, dict):
        return _Missing(disputed.get("question_id"), path)
    return None


@dataclass
class _Ctx:
    """Accumulates everything a single quote run discovers."""

    blocking: list[str]
    advisory: list[str]
    warnings: list[str]
    lines: list[PriceLine]

    def block(self, m: _Missing) -> None:
        if m.question_id and m.question_id not in self.blocking:
            self.blocking.append(m.question_id)

    def advise(self, question_id: Optional[str]) -> None:
        if question_id and question_id not in self.advisory:
            self.advisory.append(question_id)

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)


def _blocked(ctx: _Ctx, reason: str, **extra: Any) -> PriceQuote:
    human_readable = extra.pop("human_readable", "Уточню у менеджера и вернусь с ответом.")
    return PriceQuote(
        status="blocked",
        blocked_reason=reason,
        blocking_question_ids=tuple(ctx.blocking),
        warnings=tuple(ctx.warnings),
        human_readable=human_readable,
        **extra,
    )


# --------------------------------------------------------------------------
# Day type — a function of (date, zone), never of date alone
# --------------------------------------------------------------------------

def resolve_day_type(kb: KnowledgeBase, zone: Zone, d: DateType) -> DayType | _Missing:
    """Тип дня одинаков для всех зон (ответ 2.3: пятница — выходной).

    Зонозависимость, введённая ради домика, снята: constants.weekend_days
    применяется единообразно. Сигнатура с `zone` сохранена намеренно — если
    заказчик когда-нибудь заведёт зону со своим календарём, менять придётся
    одну функцию, а не всех её вызывающих.

    Праздники считаются по тарифу выходного дня (ответ 7.2).
    """
    if kb.catalog.constants.holidays.contains(d):
        return "weekend"
    token = _DOW_TOKENS[d.weekday()]
    if token in kb.catalog.constants.weekday_days:
        return "weekday"
    return "weekend"


def is_provisional_holiday(kb: KnowledgeBase, d: DateType) -> bool:
    return kb.catalog.constants.holidays.contains(d)


# --------------------------------------------------------------------------
# Extras
# --------------------------------------------------------------------------

def resolve_extra_price(
    kb: KnowledgeBase, zone: Zone, extra_id: str, day_type: DayType
) -> tuple[Optional[Money], Optional[str], Optional[_Missing]]:
    """One resolver for both catalogue-wide extras and zone-scoped services.

    Returns (price, description, missing). Exactly one of price/missing is
    set when the extra exists; both are None when the id is unknown.
    """
    services = zone.extra_services or {}
    if extra_id in services:
        node = services[extra_id]
        for key in ("per_hour", "per_stay", "price"):
            if key in node:
                got = _read(node[key], f"{zone.id}.extra_services.{extra_id}.{key}")
                if isinstance(got, _Missing):
                    return None, None, got
                if got is not None:
                    return money(got), f"{extra_id} ({zone.name})", None
        return None, None, None

    for item in kb.catalog.extras:
        if item.id != extra_id:
            continue
        holder = item.price
        if holder is None:
            holder = item.weekday_price if day_type == "weekday" else item.weekend_price
        if holder is None:
            return None, None, None
        if not holder.is_resolved():
            return None, None, _Missing(holder.disputed.question_id, f"extras.{extra_id}")
        return money(holder.value), item.name, None

    return None, None, None


# --------------------------------------------------------------------------
# Promos
# --------------------------------------------------------------------------

_BIRTHDAY_MARKERS = ("день рожд", "рождени", "именин", "birthday", "др ")


def _looks_like_birthday(hint: Optional[str]) -> bool:
    if not hint:
        return False
    lowered = hint.lower()
    return any(marker in lowered for marker in _BIRTHDAY_MARKERS)


def _free_hours(occupied: int, min_paid: int, repeatable: bool) -> int:
    """`min_paid` paid hours earn one free hour. Repeatable means the offer
    resets, so a long booking earns several."""
    block = min_paid + 1
    if occupied < block:
        return 0
    return occupied // block if repeatable else 1


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def _hhmm_to_minutes(value: str) -> int:
    """«09:00» -> 540. Формат чинит валидация базы знаний (app/kb/editable.py),
    здесь он уже гарантирован."""
    hours, minutes = (int(part) for part in value.split(":"))
    return hours * 60 + minutes


def quote(req: PriceRequest, kb: KnowledgeBase) -> PriceQuote:
    ctx = _Ctx(blocking=[], advisory=[], warnings=[], lines=[])

    # ---- zone lookup -----------------------------------------------------
    zone = next((z for z in kb.catalog.zones if z.id == req.zone_id), None)
    if zone is None:
        return PriceQuote(
            status="invalid",
            blocked_reason=f"Зона {req.zone_id!r} не найдена в каталоге",
            human_readable="Такой зоны у нас нет.",
        )

    if req.hours is not None and req.hours <= 0:
        return PriceQuote(
            status="invalid",
            zone_id=zone.id,
            blocked_reason="hours должно быть положительным",
            human_readable="Уточните, пожалуйста, на сколько часов планируете.",
        )
    if req.guests is not None and req.guests <= 0:
        return PriceQuote(
            status="invalid",
            zone_id=zone.id,
            blocked_reason="guests должно быть положительным",
            human_readable="Уточните, пожалуйста, количество гостей.",
        )

    mode = zone.pricing.get("mode")

    # ---- needs_input: what the CLIENT still has to tell us ---------------
    missing_fields: list[str] = []
    if zone.pricing.get("guests_required") and req.guests is None:
        missing_fields.append("guests")
    if mode == "hourly" and req.hours is not None and req.start_time is None:
        # Without a start time we cannot check the closing-hour boundary.
        # This mirrors what managers actually ask: «Со скольки до скольки
        # планируете отдых?»
        missing_fields.append("start_time")
    if mode == "hourly" and req.hours is None and not zone.day_package:
        missing_fields.append("hours")
    if missing_fields:
        return PriceQuote(
            status="needs_input",
            zone_id=zone.id,
            missing_fields=tuple(missing_fields),
            human_readable=_ask_for(missing_fields),
        )

    # Праздники больше не блокируют расчёт: заказчик подтвердил, что они
    # считаются по тарифу выходного дня (ответ 7.2). Логика — в resolve_day_type.
    holidays = kb.catalog.constants.holidays
    if holidays.provisional and holidays.contains(req.date):
        ctx.block(_Missing(holidays.question_id, "constants.holidays"))
        return _blocked(
            ctx,
            "Дата попадает в предварительный список праздничных дней, тарифный статус не подтверждён",
            zone_id=zone.id,
        )

    # ---- day type --------------------------------------------------------
    day_type = resolve_day_type(kb, zone, req.date)
    if isinstance(day_type, _Missing):
        ctx.block(day_type)
        return _blocked(ctx, "Не определён тип дня (будни/выходные) для этой зоны", zone_id=zone.id)

    # ---- capacity --------------------------------------------------------
    if zone.capacity.is_resolved():
        if req.guests is not None and req.guests > zone.capacity.value:
            return PriceQuote(
                status="invalid",
                zone_id=zone.id,
                day_type=day_type,
                blocked_reason=f"Гостей {req.guests}, вместимость зоны {zone.capacity.value}",
                suggested_alternatives=_alternatives_for(kb, req.guests, zone.id),
                human_readable=(
                    f"На {req.guests} гостей эта зона маловата — вмещает до {zone.capacity.value}. "
                    "Могу предложить вариант побольше."
                ),
            )
    else:
        ctx.advise(zone.capacity.disputed.question_id)
        ctx.warn("Вместимость зоны не подтверждена — не называть клиенту точную цифру.")

    # ---- рабочее окно: и заезд, и выезд -----------------------------------
    # 8.1/14.2: рабочее окно (9:00-23:00) подтверждено. Бронь, выходящая за
    # него, — не дыра в базе знаний (нет question_id, никого не спрашиваем):
    # это решённое операционное правило — такая бронь ВСЕГДА эскалируется на
    # подтверждение менеджера, а не считается по какому-то особому тарифу.
    # Заодно закрывает 8.3: перехода через полночь с расчётом по двум
    # тарифам в системе больше не бывает.
    #
    # ПРОВЕРЯЮТСЯ ОБА КРАЯ. Раньше — только закрывающий час, и заезд в 7:00
    # проходил насквозь: территория закрыта, а котировка выдавалась как
    # обычная. Ранний заезд ничем не отличается от позднего выезда — это то
    # же «нужно, чтобы кто-то открыл и был на месте», и решает это человек.
    #
    # У зоны без своего `booking_window` берётся ОБЩЕЕ окно комплекса
    # (constants.working_window), а не «проверки нет»: отсутствие поля у
    # юрты значит «отдельного окна нет», а не «работает круглосуточно».
    # Пропуск проверки на этом основании — ровно тот молчаливый обход,
    # из-за которого правило и не срабатывало на половине случаев.
    window = zone.booking_window or kb.catalog.constants.working_window
    if req.start_time is not None and window is not None:
        open_minutes = _hhmm_to_minutes(window.from_)
        close_minutes = _hhmm_to_minutes(window.to)
        start_minutes = req.start_time.hour * 60 + req.start_time.minute
        end_minutes = start_minutes + (req.hours or 0) * 60
        if start_minutes < open_minutes:
            return _blocked(
                ctx,
                f"Заезд раньше {window.from_} — требуется подтверждение "
                "менеджера, что зону откроют к этому времени",
                zone_id=zone.id,
                day_type=day_type,
                human_readable=(
                    f"Мы работаем с {window.from_}. Уточню у менеджера, можно ли "
                    "заехать раньше, и вернусь с ответом."
                ),
            )
        if req.hours is not None and end_minutes > close_minutes:
            return _blocked(
                ctx,
                f"Бронь заканчивается позже {window.to} — "
                "требуется подтверждение менеджера на возможность продления",
                zone_id=zone.id,
                day_type=day_type,
                human_readable="Уточню у менеджера, можно ли продлить, и вернусь с ответом.",
            )

    # ---- price the booking ----------------------------------------------
    # Branch on the declared pricing mode, not on which fields happen to be
    # present (decision 8). For a daily zone `hours` is simply meaningless
    # and is ignored rather than treated as a day-package request.
    if mode == "daily":
        result = _price_daily(req, kb, zone, day_type, ctx)
    elif req.hours is None:
        result = _price_day_package(req, kb, zone, day_type, ctx)
    else:
        result = _price_hourly(req, kb, zone, day_type, ctx)

    if isinstance(result, PriceQuote):        # blocked/invalid short-circuit
        return result

    subtotal, applied_promo, alternatives, occupied, billable, base_rate = result

    # ---- extras ----------------------------------------------------------
    for extra_id, qty in req.extras:
        price, description, miss = resolve_extra_price(kb, zone, extra_id, day_type)
        if miss is not None:
            ctx.block(miss)
            return _blocked(
                ctx, f"Цена дополнительной услуги {extra_id!r} не подтверждена",
                zone_id=zone.id, day_type=day_type,
            )
        if price is None:
            return PriceQuote(
                status="invalid",
                zone_id=zone.id,
                day_type=day_type,
                blocked_reason=f"Дополнительная услуга {extra_id!r} не найдена",
                human_readable="Такой дополнительной услуги у нас нет.",
            )
        amount = money(price * Decimal(qty))
        ctx.lines.append(
            PriceLine(
                code=f"extra:{extra_id}",
                description=description or extra_id,
                qty=Decimal(qty),
                unit_price=price,
                amount=amount,
                source_field=f"extras.{extra_id}",
            )
        )
        subtotal = money(subtotal + amount)

    # ---- prepayment ------------------------------------------------------
    # 9.1: почасовые зоны — предоплата = стоимость ПЕРВОГО ЧАСА.
    # 14.4: суточные зоны и пакеты «весь день» (часа нет) — фиксированные
    # 3000 ₽, цифра из official_pricing.md.
    prepay_rule = kb.payment.payment.prepayment_rule
    prepayment: Optional[Money] = None
    if not prepay_rule.is_resolved():
        ctx.advise(prepay_rule.disputed.question_id)
        ctx.warn("Единая формула предоплаты не утверждена — сумму предоплаты не называть.")
    elif prepay_rule.value == "first_hour_price":
        if base_rate is not None:
            prepayment = money(base_rate)
        else:
            daily_rule = kb.payment.payment.daily_and_package_prepayment
            if daily_rule is not None and daily_rule.is_resolved():
                prepayment = money(daily_rule.value)
            else:
                ctx.advise("14.4")
                ctx.warn(
                    "Предоплата равна стоимости первого часа, но у этой брони часа нет "
                    "(сутки или пакет) — сумму предоплаты не называть."
                )

    # ---- minimum hours (arithmetic is fine; the RIGHT to sell may not be)
    requires_tier: Optional[int] = None
    if occupied is not None:
        min_hours_node = zone.pricing.get("min_hours")
        min_hours = _read(min_hours_node, f"{zone.id}.min_hours")
        if isinstance(min_hours, _Missing):
            # Unresolved minimum: assume the strictest known variant so we
            # never silently sell below a minimum that may turn out to apply.
            variants = (min_hours_node or {}).get("disputed", {}).get("variants") or []
            strictest = max((v for v in variants if isinstance(v, int)), default=None)
            ctx.advise(min_hours.question_id)
            if strictest is not None and occupied < strictest:
                requires_tier = _tier_id(kb, "reduce_min_hours")
                ctx.warn(
                    f"Часов меньше возможного минимума ({occupied} < {strictest}), "
                    "и сам минимум не подтверждён."
                )
        elif min_hours is not None and occupied < min_hours:
            requires_tier = _tier_id(kb, "reduce_min_hours")
            ctx.warn(f"Часов меньше минимума зоны ({occupied} < {min_hours}).")

    assert subtotal > 0, "total must be positive for status='ok'"

    return PriceQuote(
        status="ok",
        total=subtotal,
        lines=tuple(ctx.lines),
        applied_promo=applied_promo,
        alternative_promos=tuple(alternatives),
        prepayment=prepayment,
        blocking_question_ids=(),      # invariant: empty unless status="blocked"
        warnings=tuple(ctx.warnings),
        human_readable=_render(ctx.lines, subtotal, occupied, applied_promo),
        day_type=day_type,
        zone_id=zone.id,
        requires_concession_tier=requires_tier,
        advisory_question_ids=tuple(ctx.advisory),
        base_rate=base_rate,
        billable_hours=billable,
        occupied_hours=occupied,
        guests=req.guests,
    )


# --------------------------------------------------------------------------
# Pricing modes
# --------------------------------------------------------------------------

def _price_hourly(req, kb, zone, day_type, ctx):
    hours = req.hours

    # --- base rate --------------------------------------------------------
    if zone.pricing.get("rate_depends_on_guests"):
        rate = _tent_rate(req, zone, day_type, ctx)
        if isinstance(rate, PriceQuote):
            return rate
    else:
        key = "weekday_per_hour" if day_type == "weekday" else "weekend_per_hour"
        got = _read(zone.pricing.get(key), f"{zone.id}.pricing.{key}")
        if isinstance(got, _Missing):
            ctx.block(got)
            return _blocked(ctx, f"Ставка {key} для этой зоны не подтверждена",
                            zone_id=zone.id, day_type=day_type)
        if got is None:
            return PriceQuote(
                status="invalid", zone_id=zone.id, day_type=day_type,
                blocked_reason=f"У зоны нет тарифа {key}",
                human_readable="Эта зона так не сдаётся.",
            )
        rate = money(got)

    rate_field = f"{zone.id}.pricing"

    # --- Knight special weekday rate --------------------------------------
    special = zone.pricing.get("weekday_special")
    if special and day_type == "weekday":
        special_min = _read(special.get("min_hours"), f"{zone.id}.weekday_special.min_hours")
        if isinstance(special_min, int) and hours >= special_min:
            special_rate = _read(special.get("per_hour"), f"{zone.id}.weekday_special.per_hour")
            calc_mode = _read(special.get("calculation_mode"), f"{zone.id}.weekday_special.calculation_mode")
            if isinstance(special_rate, _Missing):
                ctx.block(special_rate)
            if isinstance(calc_mode, _Missing):
                ctx.block(calc_mode)
            if ctx.blocking:
                return _blocked(
                    ctx,
                    f"При {hours} ч в будни должна применяться спеццена, "
                    "но её размер и/или способ расчёта не подтверждены",
                    zone_id=zone.id, day_type=day_type,
                )

    # --- promos -----------------------------------------------------------
    promo_result = _select_promo(req, kb, zone, day_type, hours, rate, ctx)
    if isinstance(promo_result, PriceQuote):
        return promo_result
    applied, alternatives, billable, discount = promo_result

    ctx.lines.append(
        PriceLine(
            code="base_hourly",
            description=f"Аренда «{zone.name}»",
            qty=Decimal(billable),
            unit_price=rate,
            amount=money(rate * Decimal(billable)),
            source_field=rate_field,
        )
    )
    subtotal = money(rate * Decimal(billable))

    if billable < hours:
        ctx.lines.append(
            PriceLine(
                code="promo:free_hours",
                description=f"{hours - billable} ч в подарок",
                qty=Decimal(hours - billable),
                unit_price=money(0),
                amount=money(0),
                source_field="promos.sixth_hour_free",
            )
        )

    if discount is not None:
        ctx.lines.append(discount)
        subtotal = money(subtotal + discount.amount)

    return subtotal, applied, alternatives, hours, billable, rate


def _price_daily(req, kb, zone, day_type, ctx):
    key = "weekday_per_day" if day_type == "weekday" else "weekend_per_day"
    node = zone.pricing.get(key) or zone.pricing.get("per_day")
    got = _read(node, f"{zone.id}.pricing.{key}")
    if isinstance(got, _Missing):
        ctx.block(got)

    if day_type == "weekend":
        available = _read(zone.pricing.get("available_on_weekend"), f"{zone.id}.available_on_weekend")
        if isinstance(available, _Missing):
            ctx.block(available)
        elif available is False:
            return PriceQuote(
                status="invalid", zone_id=zone.id, day_type=day_type,
                blocked_reason="Зона не сдаётся в выходные",
                human_readable="В выходные эта зона не сдаётся.",
            )

    if ctx.blocking:
        return _blocked(ctx, "Суточная цена этой зоны не подтверждена",
                        zone_id=zone.id, day_type=day_type)

    # Advisory-only unknowns that do not change the amount (e.g. yurt check-in).
    for advisory_key in ("checkin",):
        maybe = _read(zone.pricing.get(advisory_key), f"{zone.id}.{advisory_key}")
        if isinstance(maybe, _Missing):
            ctx.advise(maybe.question_id)
            ctx.warn("Время заезда не подтверждено — не называть клиенту точный час.")

    rate = money(got)
    ctx.lines.append(
        PriceLine(
            code="base_daily",
            description=f"«{zone.name}» — сутки",
            qty=Decimal(1),
            unit_price=rate,
            amount=rate,
            source_field=f"{zone.id}.pricing.{key}",
        )
    )
    # hours is meaningless for a daily zone — ignored on purpose, not an error.
    return rate, None, [], None, None, None


def _price_day_package(req, kb, zone, day_type, ctx):
    package = zone.day_package
    if not package:
        return PriceQuote(
            status="invalid", zone_id=zone.id, day_type=day_type,
            blocked_reason="У зоны нет пакета «весь день»",
            human_readable="Пакета на весь день для этой зоны нет.",
        )

    allowed_days = package.get("days")
    if allowed_days:
        token = _DOW_TOKENS[req.date.weekday()]
        if token not in allowed_days:
            return PriceQuote(
                status="invalid", zone_id=zone.id, day_type=day_type,
                blocked_reason=f"Пакет «весь день» действует только {allowed_days}",
                blocking_question_ids=(),
                human_readable=(
                    "Пакет на весь день действует только с понедельника по четверг. "
                    "На эту дату могу посчитать почасовую аренду."
                ),
            )

    got = _read(package.get("price"), f"{zone.id}.day_package.price")
    if isinstance(got, _Missing):
        ctx.block(got)
        return _blocked(ctx, "Цена пакета «весь день» не подтверждена",
                        zone_id=zone.id, day_type=day_type)

    price = money(got)
    ctx.lines.append(
        PriceLine(
            code="day_package",
            description=f"«{zone.name}» — весь день",
            qty=Decimal(1),
            unit_price=price,
            amount=price,
            source_field=f"{zone.id}.day_package.price",
        )
    )
    if req.promo_hint:
        ctx.warn("На пакеты «весь день» акции не распространяются.")
    return price, None, [], None, None, None


# --------------------------------------------------------------------------
# Tent: the one zone whose rate depends on guest count
# --------------------------------------------------------------------------

def _tent_rate(req, zone, day_type, ctx):
    """Единственная зона, где ставка зависит от числа гостей.

    Ответ 5.1: разницы между буднями и выходными у шатра НЕТ, тариф одинаков
    всю неделю. Ответ 5.2: граница «до 20» включительно, ровно 20 гостей идут
    по нижнему тарифу 2500 ₽/час.
    """
    tiers = zone.pricing.get("guest_tiers") or []

    for tier in tiers:
        if req.guests <= tier["max_guests"]:
            got = _read(tier.get("per_hour"), f"{zone.id}.weekend_guest_tiers.per_hour")
            if isinstance(got, _Missing):
                ctx.block(got)
                return _blocked(ctx, "Тариф шатра для этой компании не подтверждён",
                                zone_id=zone.id, day_type=day_type)
            return money(got)

    return _blocked(ctx, "Не найден тариф шатра для этого числа гостей",
                    zone_id=zone.id, day_type=day_type)


# --------------------------------------------------------------------------
# Promo selection
# --------------------------------------------------------------------------

def _select_promo(req, kb, zone, day_type, hours, rate, ctx):
    """Returns (applied_promo_id, alternatives, billable_hours, discount_line)
    or a PriceQuote when blocked."""
    candidates: list[tuple[str, Money, int, Optional[PriceLine]]] = []
    alternatives: list[str] = []

    for promo in kb.promos.promos:
        if promo.type == "day_package":
            continue

        disputed_here = bool(promo.disputed_zones and zone.id in promo.disputed_zones.value)

        # The client explicitly asked for something this promo covers, but
        # whether it applies to THIS zone is unresolved. Quoting the full
        # price in silence is the worst outcome — that is exactly how the
        # manager lost ground in d20. Escalate instead.
        if disputed_here and promo.type == "percent_discount" and _looks_like_birthday(req.promo_hint):
            ctx.block(_Missing(promo.disputed_zones.question_id, f"promos.{promo.id}.disputed_zones"))
            return _blocked(
                ctx,
                f"Клиент просит скидку по акции {promo.id!r}, "
                "а её действие на эту зону не подтверждено",
                zone_id=zone.id, day_type=day_type,
            )

        if disputed_here or zone.id not in promo.applies_to_zones:
            continue

        if promo.type == "free_hour":
            min_paid = int(promo.conditions.get("min_paid_hours", 0))
            repeatable_dv = promo.repeatable
            if repeatable_dv is not None and not repeatable_dv.is_resolved():
                # Only ambiguous when the two readings actually differ.
                if _free_hours(hours, min_paid, True) != _free_hours(hours, min_paid, False):
                    ctx.block(_Missing(repeatable_dv.disputed.question_id, f"promos.{promo.id}.repeatable"))
                    return _blocked(
                        ctx,
                        f"При {hours} ч неясно, повторяется ли акция «{promo.name}»",
                        zone_id=zone.id, day_type=day_type,
                    )
                free = _free_hours(hours, min_paid, False)
            else:
                free = _free_hours(hours, min_paid, bool(repeatable_dv and repeatable_dv.value))
            if free > 0:
                billable = hours - free
                candidates.append((promo.id, money(rate * Decimal(billable)), billable, None))

        elif promo.type == "percent_discount":
            if not _looks_like_birthday(req.promo_hint):
                continue
            if promo.percent is None or not promo.percent.is_resolved():
                continue
            gross = money(rate * Decimal(hours))
            pct = Decimal(promo.percent.value) / Decimal(100)
            discount_amount = money(-(gross * pct))
            line = PriceLine(
                code=f"promo:{promo.id}",
                description=f"{promo.name} (−{promo.percent.value}%)",
                qty=Decimal(1),
                unit_price=discount_amount,
                amount=discount_amount,
                source_field=f"promos.{promo.id}",
            )
            candidates.append((promo.id, money(gross + discount_amount), hours, line))

    if not candidates:
        return None, [], hours, None

    # Promos never stack: pick the one that is cheapest for the client.
    candidates.sort(key=lambda c: c[1])
    best = candidates[0]
    alternatives = [c[0] for c in candidates[1:]]
    return best[0], alternatives, best[2], best[3]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _tier_id(kb: KnowledgeBase, tier_name: str) -> Optional[int]:
    for tier in kb.concessions.policy.ladder:
        if tier.id == tier_name:
            return tier.tier
    return None


def _alternatives_for(kb: KnowledgeBase, guests: int, exclude: str) -> tuple[str, ...]:
    """Only zones with a CONFIRMED capacity — we must not promise a zone
    whose capacity is itself an open question."""
    return tuple(
        z.id for z in kb.catalog.zones
        if z.id != exclude and z.capacity.is_resolved() and z.capacity.value >= guests
    )


def _ask_for(fields: Sequence[str]) -> str:
    questions = {
        "guests": "сколько будет гостей",
        "start_time": "со скольки планируете",
        "hours": "на сколько часов",
    }
    parts = [questions[f] for f in fields if f in questions]
    return "Подскажите, пожалуйста, " + " и ".join(parts) + "?"


def _render(lines: Sequence[PriceLine], total: Money, occupied: Optional[int],
            applied_promo: Optional[str]) -> str:
    """Client-facing text, no greetings.

    INVARIANT: never print a derived per-hour rate when a promo is applied.
    Writing «выходит 1 666 ₽ в час» teaches the client a number that is not
    our tariff, and they will demand it next time. Show the lines instead.
    """
    out: list[str] = []
    for line in lines:
        if line.code == "base_hourly":
            out.append(
                f"{int(line.qty)} ч × {line.unit_price} ₽ = {line.amount} ₽"
            )
        elif line.code == "promo:free_hours":
            out.append(f"{int(line.qty)} ч — в подарок")
        elif line.amount < 0:
            out.append(f"{line.description}: {line.amount} ₽")
        else:
            out.append(f"{line.description}: {line.amount} ₽")

    if occupied is not None:
        out.append(f"Итого {total} ₽ за {occupied} ч")
    else:
        out.append(f"Итого {total} ₽")
    return "\n".join(out)
