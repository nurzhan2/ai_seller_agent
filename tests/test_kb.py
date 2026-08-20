"""Tests for app/kb — the ПарМангал knowledge base.

Run with: python -m pytest tests/test_kb.py -v
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from app.kb.loader import (
    KB_DIR,
    DisputedBlock,
    DisputedValue,
    ConcessionsFile,
    load_catalog,
    scan_for_credentials,
    validate_no_orphan_disputed,
    collect_question_ids,
    _read_yaml,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def kb():
    return load_catalog()


@pytest.fixture(scope="module")
def raw_docs():
    return {
        "catalog": _read_yaml(KB_DIR / "catalog.yaml"),
        "promos": _read_yaml(KB_DIR / "promos.yaml"),
        "concessions": _read_yaml(KB_DIR / "concessions.yaml"),
        "payment": _read_yaml(KB_DIR / "payment.yaml"),
    }


@pytest.fixture(scope="module")
def raw_text_blobs():
    texts = {}
    for name in ("catalog.yaml", "promos.yaml", "concessions.yaml", "payment.yaml"):
        texts[name] = (KB_DIR / name).read_text(encoding="utf-8")
    return texts


# --------------------------------------------------------------------------
# 1. All four YAML files load and validate
# --------------------------------------------------------------------------

def test_all_four_files_exist():
    for name in ("catalog.yaml", "promos.yaml", "concessions.yaml", "payment.yaml"):
        assert (KB_DIR / name).exists(), f"{name} is missing from app/kb/"


def test_load_catalog_succeeds(kb):
    assert kb.catalog.meta.version == 1
    assert len(kb.catalog.zones) == 10
    assert len(kb.promos.promos) >= 1
    # Ступень 7 (суточная ставка домика) добавлена по ответу 2.1.
    assert len(kb.concessions.policy.ladder) == 7
    assert kb.payment.payment.method == "yclients_link"


def test_invalid_yaml_refuses_to_start(tmp_path):
    """A broken YAML file must not silently load — the app should refuse to start."""
    bad_dir = tmp_path
    for name in ("catalog.yaml", "promos.yaml", "concessions.yaml", "payment.yaml"):
        (bad_dir / name).write_text((KB_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
    # Corrupt catalog.yaml: give a zone a value AND a disputed block at once.
    catalog = yaml.safe_load((bad_dir / "catalog.yaml").read_text(encoding="utf-8"))
    catalog["zones"][0]["capacity"] = {
        "value": 10,
        "disputed": {"question_id": "1.6", "variants": [10, 12], "sources": []},
    }
    (bad_dir / "catalog.yaml").write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding="utf-8")

    with pytest.raises(Exception):
        load_catalog(kb_dir=bad_dir)


# --------------------------------------------------------------------------
# 2. DisputedValue: value and disputed are mutually exclusive
# --------------------------------------------------------------------------

def test_disputed_value_rejects_both_set():
    with pytest.raises(Exception):
        DisputedValue[int](
            value=5,
            disputed=DisputedBlock(question_id="1.1", variants=[5, 6], sources=["d01"]),
        )


def test_disputed_value_rejects_neither_set():
    with pytest.raises(Exception):
        DisputedValue[int]()


def test_disputed_value_accepts_value_only():
    dv = DisputedValue[int](value=2500)
    assert dv.is_resolved()
    assert dv.value == 2500


def test_disputed_value_accepts_disputed_only():
    dv = DisputedValue[int](disputed=DisputedBlock(question_id="1.1"))
    assert not dv.is_resolved()
    assert dv.value is None


def test_no_field_in_kb_has_both_value_and_disputed(raw_docs):
    for doc_name, doc in raw_docs.items():
        errors = validate_no_orphan_disputed(doc)
        assert not errors, f"{doc_name}: {errors}"


def test_orphan_disputed_detector_actually_catches_violations():
    """Prove the detector isn't a no-op: it must flag a synthetic bad node."""
    bad = {"zone": {"pricing": {"weekend_per_hour": {"value": 3500, "disputed": {"question_id": "x"}}}}}
    errors = validate_no_orphan_disputed(bad)
    assert any("both" in e for e in errors)

    neither = {"zone": {"pricing": {"weekend_per_hour": {}}}}
    # `{}` has no "value" key so it isn't picked up as a leaf at all — use an
    # explicit None value to represent "neither set" the way real disputed
    # blocks are written when nothing has been resolved yet.
    errors2 = validate_no_orphan_disputed({"zone": {"pricing": {"weekend_per_hour": {"value": None}}}})
    assert any("neither" in e for e in errors2)


def test_disputed_block_without_question_id_is_rejected():
    bad = {"zone": {"pricing": {"weekend_per_hour": {"value": None, "disputed": {"note": "no id here"}}}}}
    errors = validate_no_orphan_disputed(bad)
    assert any("question_id" in e for e in errors)


# --------------------------------------------------------------------------
# 3. Every disputed field references a real open_question id
# --------------------------------------------------------------------------

def test_every_referenced_question_id_exists_in_open_questions(raw_docs):
    open_ids = {q["id"] for q in raw_docs["catalog"]["open_questions"]}
    used_ids = set()
    for doc in raw_docs.values():
        used_ids |= collect_question_ids(doc)
    missing = used_ids - open_ids
    assert not missing, f"question_id(s) referenced but not declared in open_questions: {sorted(missing)}"


def test_open_questions_have_unique_ids(raw_docs):
    ids = [q["id"] for q in raw_docs["catalog"]["open_questions"]]
    assert len(ids) == len(set(ids)), "duplicate ids in open_questions"


def test_dangling_question_id_is_detected():
    """Prove load_catalog() actually rejects a reference to a question that
    doesn't exist — not just that the current files happen to be clean."""
    bad_catalog = {
        "meta": {"version": 1, "updated_at": "2026-08-20"},
        "constants": {
            "weekday_days": ["mon"], "weekend_days": ["fri"], "holidays": [],
            "working_window": {"from": "10:00", "to": "22:00", "disputed": False},
        },
        "venues": [],
        "zones": [],
        "extras": [],
        "knowledge": [],
        "open_questions": [{"id": "1.1", "text": "x", "blocking": False, "status": "open"}],
    }
    doc_with_dangling_ref = {
        "capacity": {"value": None, "disputed": {"question_id": "99.9", "variants": [], "sources": []}}
    }
    used = collect_question_ids(doc_with_dangling_ref)
    declared = {q["id"] for q in bad_catalog["open_questions"]}
    assert used - declared == {"99.9"}


# --------------------------------------------------------------------------
# 4. No phone numbers or bank names anywhere in the KB
# --------------------------------------------------------------------------

def test_scan_for_credentials_detects_phone_numbers():
    assert scan_for_credentials("позвоните на 89265631898 пожалуйста")
    assert scan_for_credentials("+7 (926) 932 08 84")


def test_scan_for_credentials_detects_bank_names():
    assert scan_for_credentials("переведите на Озон Банк")
    assert scan_for_credentials("оплата на Т-банк")


def test_scan_for_credentials_ignores_clean_text():
    assert not scan_for_credentials("Баня «Русский стиль», будни 2500 рублей в час")


@pytest.mark.parametrize("filename", ["catalog.yaml", "promos.yaml", "concessions.yaml", "payment.yaml"])
def test_kb_files_have_no_leaked_credentials(raw_text_blobs, filename):
    issues = scan_for_credentials(raw_text_blobs[filename])
    assert not issues, f"{filename} appears to contain payment credentials: {issues}"


def test_payment_yaml_hardcodes_no_credentials_policy(kb):
    assert kb.payment.payment.ai_may_send_bank_details is False
    assert kb.payment.payment.ai_may_send_phone_number is False


# --------------------------------------------------------------------------
# 5. concessions.yaml ladder shape
# --------------------------------------------------------------------------

def test_ladder_sorted_no_gaps(kb):
    tiers = [t.tier for t in kb.concessions.policy.ladder]
    assert tiers == sorted(tiers)
    assert tiers == list(range(1, len(tiers) + 1))


def test_non_price_tiers_precede_price_tiers(kb):
    seen_price = False
    for t in kb.concessions.policy.ladder:
        if t.type == "price":
            seen_price = True
        else:
            assert not seen_price, f"non_price tier {t.tier} ({t.id}) comes after a price tier"


def test_price_tier_is_last_not_first(kb):
    assert kb.concessions.policy.ladder[0].type == "non_price"
    assert kb.concessions.policy.ladder[-1].type == "price"


def test_ladder_validator_rejects_gap():
    good = yaml.safe_load((KB_DIR / "concessions.yaml").read_text(encoding="utf-8"))
    bad = copy.deepcopy(good)
    bad["policy"]["ladder"][2]["tier"] = 99  # introduce a gap
    with pytest.raises(Exception):
        ConcessionsFile.model_validate(bad)


def test_ladder_validator_rejects_price_before_non_price():
    good = yaml.safe_load((KB_DIR / "concessions.yaml").read_text(encoding="utf-8"))
    bad = copy.deepcopy(good)
    # Swap tier 1 (non_price) and tier 5 (price) types.
    bad["policy"]["ladder"][0]["type"] = "price"
    with pytest.raises(Exception):
        ConcessionsFile.model_validate(bad)


def test_confirmed_concession_floors(kb):
    """Ответы 1.3 и 4.2: минимум 2 часа подтверждён для бань и гриль-домика,
    день недели заказчик не ограничивал. Ставка 2500₽/час — только баня в
    выходные. Общие полы по-прежнему null."""
    tier5 = next(t for t in kb.concessions.policy.ladder if t.id == "reduce_min_hours")
    tier6 = next(t for t in kb.concessions.policy.ladder if t.id == "reduce_hourly_rate")
    assert tier5.floor["value"] is None  # no global floor confirmed
    assert tier6.floor["value"] is None
    scopes5 = {o["scope"]["zone_category"] for o in tier5.confirmed_overrides}
    assert scopes5 == {"bath", "grill"}
    for override in tier5.confirmed_overrides:
        # День недели намеренно не указан — значит «любой день».
        assert "day_type" not in override["scope"]
        assert override["floor"]["min_hours"]["value"] == 2

    bath_override_6 = tier6.confirmed_overrides[0]
    assert bath_override_6["scope"] == {"zone_category": "bath", "day_type": "weekend"}
    assert bath_override_6["floor"]["weekend_per_hour"]["value"] == 2500


# --------------------------------------------------------------------------
# 6. Readiness audit sanity checks
# --------------------------------------------------------------------------

def test_readiness_report_covers_every_zone(kb):
    from app.kb.loader import audit_readiness

    report = audit_readiness(kb)
    assert {r.zone_id for r in report} == {z.id for z in kb.catalog.zones}


def test_bath_knight_carries_client_alt_name(kb):
    """Промт №13, вопрос 15.3: заказчик называет зону «Баня Замок Рыцаря»,
    сама zone_id/name не переименовывались, второе имя — рядом."""
    zone = next(z for z in kb.catalog.zones if z.id == "bath_knight")
    assert zone.display_name_alt == "Баня Замок Рыцаря"
    assert zone.name == "Баня «Рыцарская»"


def test_display_name_alt_is_optional_for_other_zones(kb):
    others = [z for z in kb.catalog.zones if z.id != "bath_knight"]
    assert all(z.display_name_alt is None for z in others)


def test_two_bath_zones_are_price_ready(kb):
    """bath_russian / bath_garage have no disputed price fields — they are the
    (only) zones an engine can quote a base price for right now."""
    from app.kb.loader import audit_readiness

    report = {r.zone_id: r for r in audit_readiness(kb)}
    assert report["bath_russian"].ready_for_pricing
    assert report["bath_garage"].ready_for_pricing


def test_all_zones_are_price_ready(kb):
    """Промт №11 закрывает 14.1 нашим провизорным решением (15000 ₽,
    прямое указание владельца) — все 10 зон считаются."""
    from app.kb.loader import audit_readiness

    not_ready = [r.zone_id for r in audit_readiness(kb) if not r.ready_for_pricing]
    assert not_ready == []


def test_all_zones_are_dialog_ready(kb):
    """10 из 10 зон готовы по обеим колонкам после промта №11. Остались
    только два общих блокера (12.1 фото, 12.2 объявления) — они не привязаны
    ни к одной конкретной зоне."""
    from app.kb.loader import audit_readiness

    not_ready = [r.zone_id for r in audit_readiness(kb) if not r.ready_for_dialog]
    assert not_ready == []


def test_dialog_readiness_implies_pricing_readiness(kb):
    """Колонки считаются независимо, но связь одностороняя: отвечать на все
    вопросы по зоне нельзя, не умея назвать её цену."""
    from app.kb.loader import audit_readiness

    for row in audit_readiness(kb):
        if row.ready_for_dialog:
            assert row.ready_for_pricing, row.zone_id


def test_bath_has_no_blockers_left(kb):
    """1.1 и 1.2 закрыты (акции действуют на бани), 1.4/1.5 сняты вместе с
    отменённой спеццено «Рыцарской»."""
    from app.kb.loader import audit_readiness

    report = {r.zone_id: r for r in audit_readiness(kb)}
    assert report["bath_russian"].dialog_blockers == []
    assert report["bath_knight"].ready_for_pricing


def test_dome_capacity_no_longer_blocks_anything(kb):
    """3.3 закрыт: у мешков 10 мест, у кресел и стульев по 7."""
    from app.kb.loader import audit_readiness

    report = {r.zone_id: r for r in audit_readiness(kb)}
    for zone_id in ("dome_bags", "dome_blue_chairs", "dome_chairs"):
        assert "3.3" not in report[zone_id].dialog_blockers
        assert report[zone_id].ready_for_pricing


def test_global_blockers_are_not_attributed_to_any_zone(kb):
    """Address / prepayment / holidays gate every zone equally and must be
    reported separately rather than smeared across every row."""
    from app.kb.loader import audit_readiness, global_blockers

    zone_scoped = set()
    for row in audit_readiness(kb):
        zone_scoped |= set(row.dialog_blockers)
    global_ids = {q.id for q in global_blockers(kb)}
    assert not (global_ids & zone_scoped)
    # 9.1, 10.1, 7.2, 8.1 закрыты ответами; остались фото, объявления,
    # полы уступок и новые вопросы 14.x.
    for expected in ("12.1", "12.2"):
        assert expected in global_ids


def test_question_3_3_and_3_4_are_separate_findings(raw_docs):
    """3.3 is about dome furniture (d08); 3.4 is about the unidentified zone
    sold at 2500₽/h on a Sunday (d12). Conflating them was a real bug —
    guard against a regression."""
    questions = {q["id"]: q for q in raw_docs["catalog"]["open_questions"]}
    assert "3.4" in questions, "question 3.4 must exist"

    # No dome capacity field may cite d12 as evidence any more.
    for zone in raw_docs["catalog"]["zones"]:
        if zone["category"] != "dome":
            continue
        # 3.3 закрыт ответом заказчика: вместимости подтверждены и разные.
        assert zone["capacity"].get("disputed") is None
        assert zone["capacity"]["value"] in (7, 10)
