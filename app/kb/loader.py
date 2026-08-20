"""Loader and validation for the ПарМангал knowledge base (app/kb/*.yaml).

Design rule threaded through every model here: a price/rule the audit found
disputed must never resolve itself to a guess. `DisputedValue[T]` enforces
that in code — a field either carries a resolved `value`, or an open
`disputed` block with a `question_id` the operator still needs to answer.
`value: None` is a hard stop for any pricing/decision logic built on top of
this module: it means "escalate to a human", not "pick something reasonable".

Usage:
    from app.kb.loader import load_catalog, audit_readiness

    kb = load_catalog()
    for row in audit_readiness(kb):
        print(row)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date as DateType
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Literal, Optional, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger("parmangal.kb")

KB_DIR = Path(__file__).resolve().parent

T = TypeVar("T")


# --------------------------------------------------------------------------
# Core disputed-value machinery
# --------------------------------------------------------------------------

class DisputedBlock(BaseModel):
    variants: list[Any] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    question_id: str
    note: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class DisputedValue(BaseModel, Generic[T]):
    """Either a resolved `value`, or an open `disputed` block — never both.

    `value is None` means the agent has NO RIGHT to state this fact and must
    escalate ("уточню у менеджера"). No defaults, no averaging, no falling
    back to official_pricing.md unless that fallback is explicit via
    `resolved_from` (used only for the LOW-severity findings the audit
    allowed us to resolve ourselves).
    """

    value: Optional[T] = None
    disputed: Optional[DisputedBlock] = None
    resolved_from: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_exclusive(self) -> "DisputedValue[T]":
        if self.value is not None and self.disputed is not None:
            raise ValueError("DisputedValue cannot have both `value` and `disputed` set")
        if self.value is None and self.disputed is None:
            raise ValueError("DisputedValue must have either `value` or `disputed` set")
        if self.resolved_from is not None and self.value is None:
            raise ValueError("`resolved_from` requires a resolved `value`")
        return self

    def is_resolved(self) -> bool:
        return self.value is not None


class ZonesFinding(BaseModel):
    """A CONFIRMED fact about which zones something happened to in the real
    dialogs (e.g. a promo applied where it shouldn't have been), paired with
    an open policy question about whether to allow it going forward. This is
    deliberately not a DisputedValue: we are not unsure of the fact, we are
    waiting on a policy decision about it.
    """

    value: list[str]
    note: Optional[str] = None
    question_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# catalog.yaml
# --------------------------------------------------------------------------

class Meta(BaseModel):
    version: int
    updated_at: str


class WorkingWindow(BaseModel):
    from_: str = Field(alias="from")
    to: str
    disputed: bool = False
    resolved_from: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class Holidays(BaseModel):
    """Recurring MM-DD dates. `provisional` means the list itself is a
    well-known public fact, but its *tariff status* is unconfirmed — the
    pricing engine blocks on these dates rather than guessing."""

    provisional: bool
    question_id: Optional[str] = None
    resolved_from: Optional[str] = None
    dates: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def contains(self, d: "DateType") -> bool:
        return f"{d.month:02d}-{d.day:02d}" in set(self.dates)


class Constants(BaseModel):
    weekday_days: list[str]
    weekend_days: list[str]
    holidays: Holidays
    working_window: WorkingWindow


class Venue(BaseModel):
    id: str
    name: str
    address: Optional[str] = None


class ZoneCategory(str, Enum):
    bath = "bath"
    house = "house"
    dome = "dome"
    grill = "grill"
    tent = "tent"
    yurt = "yurt"


class BookingWindow(BaseModel):
    from_: str = Field(alias="from")
    to: str

    model_config = ConfigDict(populate_by_name=True)


class Zone(BaseModel):
    """`pricing`, `day_package` and `extra_services` are intentionally loose
    (`dict[str, Any]`) because their shape genuinely differs by zone category
    (hourly bath vs. daily house vs. tiered-by-guest-count tent). Every
    numeric leaf inside them still follows the DisputedValue shape
    (`{value: ...}` or `{value: null, disputed: {...}}`) — that is enforced
    generically by `validate_no_orphan_disputed` over the raw document,
    rather than by a dedicated pydantic model per zone category.
    """

    id: str
    venue: str
    name: str
    # Заказчик иногда называет зону иначе, чем она записана в каталоге
    # (промт №13: «Баня Замок Рыцаря» на Авито vs «Рыцарская» здесь).
    # zone_id намеренно не переименовывается под это — слишком много кода
    # завязано на текущие id. Вместо этого агент знает оба имени и отвечает
    # словами клиента, а не поправляет его.
    display_name_alt: Optional[str] = None
    category: ZoneCategory
    capacity: DisputedValue[int]
    description: str
    includes: list[str] = Field(default_factory=list)
    photos: list[str] = Field(default_factory=list)
    booking_window: Optional[BookingWindow] = None
    pricing: dict[str, Any]
    day_package: Optional[dict[str, Any]] = None
    extra_services: Optional[dict[str, Any]] = None
    promos_applicable: list[str] = Field(default_factory=list)
    # Темы, по которым заказчик велел не называть цену, а сразу передавать
    # диалог администратору (ответы 6.2-6.5). Это правила, а не прайс.
    escalation_topics: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ExtraItem(BaseModel):
    id: str
    name: str
    price: Optional[DisputedValue[int]] = None
    weekday_price: Optional[DisputedValue[int]] = None
    weekend_price: Optional[DisputedValue[int]] = None

    model_config = ConfigDict(extra="forbid")


class KnowledgeEntry(BaseModel):
    question_topic: str
    answer: Optional[str] = None
    source: Optional[str] = None
    confidence: Literal["confirmed", "from_dialogs", "unknown"]
    question_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class OpenQuestion(BaseModel):
    id: str
    section: Optional[str] = None
    text: str
    blocking: bool
    status: Literal["open", "answered"] = "open"
    note: Optional[str] = None
    # Set when WE picked a working default instead of waiting for the client.
    # The question stays open — the default is ours to defend, not theirs.
    resolved_by: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class Catalog(BaseModel):
    meta: Meta
    constants: Constants
    venues: list[Venue]
    zones: list[Zone]
    extras: list[ExtraItem]
    knowledge: list[KnowledgeEntry]
    open_questions: list[OpenQuestion]
    # image_id по категориям территории, не привязанным к одной зоне
    # (детская площадка, санузел, общий вид) — заполняется scripts/import_photos.py.
    site_photos: dict[str, list[str]] = Field(default_factory=dict)
    # 14.7: ссылка на сайт комплекса для зон, которых нет в объявлениях Авито.
    # Ни в одном источнике проекта такая ссылка не встречалась — не выдумываем.
    site_url: Optional[DisputedValue[str]] = None

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# promos.yaml
# --------------------------------------------------------------------------

class Promo(BaseModel):
    id: str
    name: str
    type: str
    applies_to_zones: list[str] = Field(default_factory=list)
    applies_to_modes: list[str] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)
    percent: Optional[DisputedValue[int]] = None
    per_hour: Optional[DisputedValue[int]] = None
    price: Optional[DisputedValue[int]] = None
    window: Optional[dict[str, Any]] = None
    days: Optional[list[str]] = None
    repeatable: Optional[DisputedValue[bool]] = None
    calculation_mode: Optional[DisputedValue[str]] = None
    usable_in_parts: Optional[DisputedValue[bool]] = None
    stackable: bool = True
    mutually_exclusive_with: list[str] = Field(default_factory=list)
    disputed_zones: Optional[ZonesFinding] = None
    resolved_from: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class PromosFile(BaseModel):
    promos: list[Promo]

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# concessions.yaml
# --------------------------------------------------------------------------

class Applicability(BaseModel):
    """When a ladder tier is *meaningless* for the situation, as opposed to
    already used. Such a tier is marked `skipped` and does not stall the
    ladder — see concessions.py."""

    blocked_by_constraints: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class LadderTier(BaseModel):
    tier: int
    id: str
    type: Literal["non_price", "price"]
    description: str
    applicability: Applicability = Field(default_factory=Applicability)
    effect: Optional[dict[str, Any]] = None
    floor: Optional[dict[str, Any]] = None
    confirmed_overrides: Optional[list[dict[str, Any]]] = None

    model_config = ConfigDict(extra="forbid")


class Trigger(BaseModel):
    id: str
    description: str

    model_config = ConfigDict(extra="forbid")


class OfferTemplate(BaseModel):
    text: str
    exchange: str

    model_config = ConfigDict(extra="forbid")


class ConcessionPolicy(BaseModel):
    never_open_with_concession: bool
    max_concessions_per_dialog: int
    max_concessions_per_day: int
    require_exchange: bool
    limits_count_only_price_tiers: bool = True
    max_non_price_attempts_before_price: int = 2
    ladder: list[LadderTier]
    triggers: list[Trigger]
    conditions: dict[str, Any]
    exchange_options: list[str]
    exchange_clauses: dict[str, str]
    offer_templates: dict[str, OfferTemplate]
    logging: dict[str, Any]

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _ladder_is_sane(self) -> "ConcessionPolicy":
        tiers = [t.tier for t in self.ladder]
        if tiers != sorted(tiers):
            raise ValueError("ladder must be sorted by tier ascending")
        if tiers != list(range(1, len(tiers) + 1)):
            raise ValueError("ladder tiers must be contiguous starting at 1, no gaps/duplicates")
        seen_price = False
        for t in self.ladder:
            if t.type == "price":
                seen_price = True
            elif t.type == "non_price" and seen_price:
                raise ValueError(
                    f"tier {t.tier} ({t.id}) is non_price but comes after a price tier — "
                    "all non_price tiers must precede price tiers"
                )
        missing = {t.id for t in self.ladder} - set(self.offer_templates)
        if missing:
            raise ValueError(f"ladder tiers without an offer_template: {sorted(missing)}")
        return self

    @model_validator(mode="after")
    def _exchange_is_structurally_valid(self) -> "ConcessionPolicy":
        """R11, checked structurally at LOAD time rather than by hunting for
        the word «если» at runtime. A substring check both over- and
        under-fires: «при условии, что бронируем сегодня» carries a real
        exchange and would fail it, while «если хотите, расскажу про купола»
        carries none and would pass."""
        if not self.require_exchange:
            return self

        allowed = set(self.exchange_options)
        for tier_id, template in self.offer_templates.items():
            if not template.exchange:
                raise ValueError(f"offer_template {tier_id!r} has an empty `exchange`")
            if template.exchange not in allowed:
                raise ValueError(
                    f"offer_template {tier_id!r} uses exchange {template.exchange!r} "
                    f"which is not in exchange_options {sorted(allowed)}"
                )
            if template.exchange not in self.exchange_clauses:
                raise ValueError(
                    f"exchange {template.exchange!r} has no wording in exchange_clauses"
                )
            if "{exchange_clause}" not in template.text:
                raise ValueError(
                    f"offer_template {tier_id!r} is missing the {{exchange_clause}} placeholder"
                )
        return self


class ConcessionsFile(BaseModel):
    policy: ConcessionPolicy

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# payment.yaml
# --------------------------------------------------------------------------

class Payment(BaseModel):
    method: str
    ai_may_send_payment_link: bool
    ai_may_send_bank_details: Literal[False]
    ai_may_send_phone_number: Literal[False]
    handoff_on_payment_step: bool
    link_type: DisputedValue[str]
    prepayment_rule: DisputedValue[str]
    # 14.4: фиксированная предоплата для суточных зон и пакетов «весь день».
    daily_and_package_prepayment: Optional[DisputedValue[int]] = None
    refund_rule: dict[str, Any]
    no_credentials_policy: dict[str, Any]

    model_config = ConfigDict(extra="forbid")


class PaymentFile(BaseModel):
    payment: Payment

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Combined knowledge base
# --------------------------------------------------------------------------

class KnowledgeBase(BaseModel):
    catalog: Catalog
    promos: PromosFile
    concessions: ConcessionsFile
    payment: PaymentFile

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Generic structural checks over the RAW (pre-pydantic) documents.
#
# `pricing`, `day_package`, `extra_services` and the concession `ladder`
# floors are intentionally loosely typed (see Zone docstring) — this walk
# enforces the DisputedValue contract (never both `value` and `disputed`,
# every `disputed` block cites a real `question_id`) across those free-form
# subtrees the same way pydantic enforces it for the strictly-typed fields.
# --------------------------------------------------------------------------

def _looks_like_disputed_value(node: dict) -> bool:
    """True for `{value: ...}` / `{value: null, disputed: {...}}` shaped
    dicts. Deliberately excludes the simpler `{..., disputed: true}` flag
    used on `constants.working_window` — that's a plain boolean annotation,
    not a DisputedValue block, and has no `question_id` to check."""
    if "value" in node:
        return True
    disputed = node.get("disputed")
    return isinstance(disputed, dict)


def iter_disputed_leaves(node: Any, path: str = "$") -> list[tuple[str, dict]]:
    """Find every dict in `node` that looks like a DisputedValue leaf
    (has a `value` key, and/or a dict-shaped `disputed` key)."""
    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if _looks_like_disputed_value(node):
            found.append((path, node))
        for k, v in node.items():
            found.extend(iter_disputed_leaves(v, f"{path}.{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(iter_disputed_leaves(v, f"{path}[{i}]"))
    return found


def validate_no_orphan_disputed(raw: Any) -> list[str]:
    """Return a list of error strings (empty = valid)."""
    errors: list[str] = []
    for path, node in iter_disputed_leaves(raw):
        has_value = node.get("value") is not None
        has_disputed = node.get("disputed") is not None
        if has_value and has_disputed:
            errors.append(f"{path}: has both `value` and `disputed` set")
        if not has_value and not has_disputed:
            errors.append(f"{path}: has neither `value` nor `disputed` set")
        if has_disputed and not node["disputed"].get("question_id"):
            errors.append(f"{path}.disputed: missing `question_id`")
    return errors


def collect_question_ids(node: Any) -> set[str]:
    """Collect every `question_id` value referenced anywhere in the document
    (inside `disputed` blocks, `ZonesFinding`-shaped fields, knowledge
    entries, etc.) — deliberately shape-agnostic."""
    found: set[str] = set()
    if isinstance(node, dict):
        qid = node.get("question_id")
        if isinstance(qid, str):
            found.add(qid)
        for v in node.values():
            found |= collect_question_ids(v)
    elif isinstance(node, list):
        for v in node:
            found |= collect_question_ids(v)
    return found


# --------------------------------------------------------------------------
# Credential leak guard
# --------------------------------------------------------------------------

# Matches Russian mobile numbers in the various formats seen in real_dialogs.md:
# "+7926 000-00-00", "89260000000", "+7(926) 000 00 00", "8916 0000000", etc.
PHONE_RE = re.compile(
    r"(?:\+?7|8)[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}\b"
)

BANK_KEYWORDS = [
    "озон банк", "озонбанк", "т-банк", "тинькофф", "сбербанк", "сбер банк",
    "альфа-банк", "альфабанк", "втб", "райффайзен", "газпромбанк", "почта банк",
]


def scan_for_credentials(text: str) -> list[str]:
    """Return a list of human-readable issues if `text` looks like it
    contains a phone number or a bank name. Used by tests to fail loudly if
    anyone ever pastes real payment details into the knowledge base."""
    issues: list[str] = []
    for m in PHONE_RE.finditer(text):
        issues.append(f"looks like a phone number: {m.group()!r}")
    lowered = text.lower()
    for kw in BANK_KEYWORDS:
        if kw in lowered:
            issues.append(f"looks like a bank name: {kw!r}")
    return issues


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_catalog(kb_dir: Optional[Path] = None) -> KnowledgeBase:
    """Load and fully validate the four KB files. Raises on anything invalid
    — an app built on top of this must not start with a broken knowledge
    base. Logs a WARNING listing every unresolved *blocking* question."""
    base = kb_dir or KB_DIR

    raw = {
        "catalog": _read_yaml(base / "catalog.yaml"),
        "promos": _read_yaml(base / "promos.yaml"),
        "concessions": _read_yaml(base / "concessions.yaml"),
        "payment": _read_yaml(base / "payment.yaml"),
    }

    structural_errors = validate_no_orphan_disputed(raw)

    open_question_ids = {q["id"] for q in raw["catalog"]["open_questions"]}
    used_question_ids = collect_question_ids(raw)
    missing_ids = used_question_ids - open_question_ids
    if missing_ids:
        structural_errors.append(
            f"question_id referenced but not declared in open_questions: {sorted(missing_ids)}"
        )

    if structural_errors:
        raise ValueError("Invalid knowledge base:\n" + "\n".join(f"- {e}" for e in structural_errors))

    kb = KnowledgeBase.model_validate(raw)

    blocking_open = [q for q in kb.catalog.open_questions if q.blocking and q.status == "open"]
    if blocking_open:
        logger.warning(
            "KB loaded with %d unresolved BLOCKING question(s): %s",
            len(blocking_open),
            ", ".join(q.id for q in blocking_open),
        )

    return kb


# --------------------------------------------------------------------------
# Readiness audit
# --------------------------------------------------------------------------

@dataclass
class ZoneReadiness:
    """Two *independent* readiness verdicts per zone.

    `ready_for_pricing` — can the agent state a base price at all? This looks
    only at the zone's own price-bearing fields (rates, min_hours, packages,
    paid extras). Capacity is deliberately excluded: an unknown capacity is a
    conversational gap, not a pricing one (the tent is the one exception,
    where the rate is a function of guest count — that dependency lives in
    the tent's own pricing block and so is already counted).

    `ready_for_dialog` — can the agent handle the *typical* questions about
    this zone end to end? Stricter: it additionally requires a known capacity
    and unambiguous promo rules for the zone (a promo that lists this zone in
    `disputed_zones`, or whose own mechanics are unresolved, blocks it).

    Deliberately out of scope for both: KB-wide unknowns that block every
    zone equally (address 10.1, prepayment formula 9.1, closing hour 8.1,
    holiday list 7.2). Folding those in would make every row read `no` and
    hide the per-zone signal — they are reported separately.
    """

    zone_id: str
    zone_name: str
    pricing_fields_total: int
    pricing_fields_disputed: int
    ready_for_pricing: bool
    dialog_blockers: list[str]          # question_ids blocking full dialog
    ready_for_dialog: bool


# Zone subtrees that carry money. Capacity lives outside this set on purpose.
_PRICING_KEYS = ("pricing", "day_package", "extra_services")


def _promo_blockers_for_zone(kb: KnowledgeBase, zone_id: str) -> list[str]:
    """question_ids that leave this zone's promo situation ambiguous."""
    blockers: list[str] = []
    for promo in kb.promos.promos:
        mentions_zone = zone_id in promo.applies_to_zones
        disputed_here = bool(promo.disputed_zones and zone_id in promo.disputed_zones.value)

        # A promo that *might* apply to this zone but nobody has confirmed it.
        if disputed_here and promo.disputed_zones.question_id:
            blockers.append(promo.disputed_zones.question_id)

        # A promo that definitely applies, but whose own mechanics are unresolved.
        if mentions_zone:
            for mechanic in (promo.repeatable, promo.calculation_mode, promo.usable_in_parts):
                if mechanic is not None and not mechanic.is_resolved():
                    blockers.append(mechanic.disputed.question_id)
    return blockers


def audit_readiness(kb: KnowledgeBase) -> list[ZoneReadiness]:
    """One row per zone, with the two verdicts described on ZoneReadiness."""
    report: list[ZoneReadiness] = []

    for zone in kb.catalog.zones:
        dumped = zone.model_dump(by_alias=True)

        # --- pricing verdict -------------------------------------------------
        pricing_leaves: list[tuple[str, dict]] = []
        for key in _PRICING_KEYS:
            subtree = dumped.get(key)
            if subtree:
                pricing_leaves.extend(iter_disputed_leaves(subtree, f"${key}"))
        pricing_disputed = [n for _, n in pricing_leaves if n.get("disputed") is not None]
        ready_for_pricing = not pricing_disputed

        # --- dialog verdict --------------------------------------------------
        blockers: list[str] = [
            n["disputed"]["question_id"] for n in pricing_disputed
        ]
        capacity = dumped.get("capacity") or {}
        if capacity.get("disputed"):
            blockers.append(capacity["disputed"]["question_id"])
        blockers.extend(_promo_blockers_for_zone(kb, zone.id))

        deduped = sorted(set(blockers), key=lambda q: [int(p) for p in q.split(".")])

        report.append(
            ZoneReadiness(
                zone_id=zone.id,
                zone_name=zone.name,
                pricing_fields_total=len(pricing_leaves),
                pricing_fields_disputed=len(pricing_disputed),
                ready_for_pricing=ready_for_pricing,
                dialog_blockers=deduped,
                ready_for_dialog=not deduped,
            )
        )
    return report


def global_blockers(kb: KnowledgeBase) -> list[OpenQuestion]:
    """Blocking questions that are not tied to any single zone — they gate
    every conversation equally (address, prepayment formula, closing hour,
    holiday calendar, photos, Avito mapping, concession floors)."""
    zone_scoped: set[str] = set()
    for row in audit_readiness(kb):
        zone_scoped |= set(row.dialog_blockers)
    return [
        q for q in kb.catalog.open_questions
        if q.blocking and q.status == "open" and q.id not in zone_scoped
    ]


def format_readiness_table(report: list[ZoneReadiness]) -> str:
    header = (
        f"{'zone':<18} {'price flds':>10} {'disputed':>9} "
        f"{'pricing?':>9} {'dialog?':>8}  blockers"
    )
    lines = [header, "-" * (len(header) + 12)]
    for row in report:
        lines.append(
            f"{row.zone_id:<18} {row.pricing_fields_total:>10} "
            f"{row.pricing_fields_disputed:>9} "
            f"{('yes' if row.ready_for_pricing else 'NO'):>9} "
            f"{('yes' if row.ready_for_dialog else 'NO'):>8}  "
            f"{', '.join(row.dialog_blockers) or '—'}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    # The KB is Russian-language and contains ₽; a cp1251 Windows console
    # would otherwise crash on print rather than on anything substantive.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO)
    kb_ = load_catalog()
    print(format_readiness_table(audit_readiness(kb_)))
    print()
    print("KB-wide blockers (gate every zone equally):")
    for q_ in global_blockers(kb_):
        print(f"  {q_.id:<6} {q_.text[:88]}")
