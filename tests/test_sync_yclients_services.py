"""scripts/sync_yclients_services.py — сопоставление зон и услуг YCLIENTS.

Ошибка в диагностике, ради исправления которой скрипт появился: пустой
`zone_service_map` читался в /admin/booking как «у заказчика пуст каталог
YCLIENTS», хотя заказчик его заполнил — просто мы не связали услуги с
zone_id. Здесь проверяется именно связывание: подбор кандидатов, отказ
применять что-либо без явного подтверждения, и что живая проверка
занятости после записи действительно ловит нерабочую связку (пустой
staff_id → UNKNOWN, а не молчаливо «всё в порядке»).

Реальная сеть не трогается нигде — только фейковый провайдер с тем же
интерфейсом, что и YClientsProvider.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.booking.base import Availability, AvailabilityStatus, Service
from app.booking.mapping import InMemoryZoneMapping
from app.kb.loader import load_catalog
from scripts.sync_yclients_services import (
    Candidate,
    _duration,
    _money,
    _similarity,
    _stems,
    best_candidates,
    confirm_and_apply,
    propose_mapping,
    verify_live,
)

DATE = date(2026, 8, 20)


@pytest.fixture(scope="module")
def kb():
    return load_catalog()


def _service(sid, title, duration=None, price_min=None, price_max=None):
    return Service(service_id=sid, title=title, duration_seconds=duration,
                   price_min=price_min, price_max=price_max)


# --------------------------------------------------------------------------
# Сопоставление по названию — пример из задачи и вокруг него
# --------------------------------------------------------------------------

def test_similarity_matches_the_example_from_the_task():
    """Баня «Русский стиль» ↔ русская баня — буквально пример из задачи."""
    score = _similarity('Баня «Русский стиль»', "русская баня")
    assert score > 0.5


def test_similarity_handles_russian_declension_via_stems():
    """«русский»/«русская» — разные словоформы, но общая основа."""
    assert _stems("Русский") & _stems("русская")


def test_similarity_is_zero_for_unrelated_titles():
    assert _similarity("Купол с мешками", "Стрижка собак") == 0.0


def test_similarity_is_symmetric():
    a, b = 'Баня «Русский стиль»', "русская баня"
    assert _similarity(a, b) == _similarity(b, a)


def test_exact_title_match_scores_highest():
    assert _similarity('Баня «Гараж»', "Баня Гараж") == 1.0


def test_best_candidates_filters_below_the_threshold():
    services = [_service("1", "Стрижка собак"), _service("2", "Русская баня")]
    result = best_candidates(['Баня «Русский стиль»'], services)
    assert [c.service.service_id for c in result] == ["2"]


def test_best_candidates_sorts_by_score_descending():
    services = [
        _service("1", "Купол со стульями"),        # слабое совпадение
        _service("2", "Купол с мягкими мешками"),   # точное
    ]
    result = best_candidates(["Купол с мягкими мешками"], services)
    assert result[0].service.service_id == "2"
    assert result[0].score >= result[-1].score


def test_best_candidates_checks_display_name_alt_too():
    """Заказчик иногда называет зону иначе, чем она записана у нас
    (app/kb/loader.py:Zone.display_name_alt) — сравнение обязано проверять
    оба варианта, а не только основное имя."""
    services = [_service("1", "Баня Рыцарский замок")]
    # Основное имя совсем не похоже, альтернативное — почти точное совпадение.
    result = best_candidates(["Совершенно другое имя", "Баня Замок Рыцаря"], services)
    assert len(result) == 1


def test_best_candidates_respects_the_limit():
    services = [_service(str(i), "Русская баня") for i in range(10)]
    assert len(best_candidates(["Русская баня"], services, limit=3)) == 3


# --------------------------------------------------------------------------
# Предложение маппинга: не молча, пропускает уже связанные
# --------------------------------------------------------------------------

def test_propose_mapping_skips_zones_that_already_have_a_mapping(kb, capsys):
    services = [_service("1", "Русская баня")]
    existing = {"bath_russian": {"service_id": "already-set"}}

    proposals = propose_mapping(kb, services, existing)

    assert "bath_russian" not in proposals


def test_propose_mapping_finds_the_task_example(kb, capsys):
    services = [_service("1", "русская баня")]

    proposals = propose_mapping(kb, services, existing={})

    assert "bath_russian" in proposals
    assert proposals["bath_russian"].service.service_id == "1"


def test_propose_mapping_leaves_out_zones_with_no_candidate(kb, capsys):
    services = [_service("1", "Стрижка собак")]

    proposals = propose_mapping(kb, services, existing={})

    assert proposals == {}


def test_propose_mapping_prints_the_table_not_just_returns_it(kb, capsys):
    """Пункт 2 задачи: показывает предложенный маппинг — не только внутренний
    результат для кода дальше, а то, что реально увидит оператор."""
    services = [_service("1", "русская баня")]

    propose_mapping(kb, services, existing={})

    out = capsys.readouterr().out
    assert "bath_russian" in out
    assert "русская баня" in out


# --------------------------------------------------------------------------
# Подтверждение — ничего не применяется молча
# --------------------------------------------------------------------------

class _AsyncZoneMapping(InMemoryZoneMapping):
    """`InMemoryZoneMapping.set()` синхронный (пишет в dict в памяти) —
    сознательно, см. его докстринг. Скрипт же пишет в `SqlAlchemyZoneMapping`,
    чей `set()` асинхронный (настоящий поход в БД), и зовёт его через
    `await`. Тестовый двойник поэтому оборачивает `set()` в async — тестируем
    контракт, которого от скрипта ждёт прод, а не совпадение по случайности."""

    async def set(self, zone_id: str, **values) -> None:
        super().set(zone_id, **values)


@pytest.fixture
def zone_mapping():
    return _AsyncZoneMapping()


async def test_confirm_accepts_the_default_on_enter(kb, zone_mapping, monkeypatch):
    """Пустой ответ (просто Enter) принимает предложенное — самый частый
    путь, должен требовать минимум усилий от оператора."""
    answers = iter(["", "", ""])   # Enter на подтверждении, staff_id, company_id
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    proposals = {"bath_russian": Candidate(service=_service("42", "русская баня"), score=0.7)}

    await confirm_and_apply(kb, proposals, zone_mapping)

    assert zone_mapping.get("bath_russian")["service_id"] == "42"


async def test_confirm_accepts_a_manually_typed_service_id(kb, zone_mapping, monkeypatch):
    """Оператор видит другой, более подходящий service_id и вписывает его
    вместо предложенного — не обязан соглашаться с автоматикой."""
    answers = iter(["999", "", ""])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    proposals = {"bath_russian": Candidate(service=_service("42", "русская баня"), score=0.7)}

    await confirm_and_apply(kb, proposals, zone_mapping)

    assert zone_mapping.get("bath_russian")["service_id"] == "999"


async def test_confirm_skips_a_zone_on_n(kb, zone_mapping, monkeypatch):
    """«n» — явный отказ: ничего не пишется НИ ПО ЭТОЙ зоне, ни в её место
    не подставляется предложенное значение."""
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    proposals = {"bath_russian": Candidate(service=_service("42", "русская баня"), score=0.7)}

    await confirm_and_apply(kb, proposals, zone_mapping)

    assert zone_mapping.get("bath_russian") is None


async def test_confirm_writes_staff_id_and_company_id_when_given(kb, zone_mapping, monkeypatch):
    answers = iter(["", "555", "777"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    proposals = {"bath_russian": Candidate(service=_service("42", "русская баня"), score=0.7)}

    await confirm_and_apply(kb, proposals, zone_mapping)

    row = zone_mapping.get("bath_russian")
    assert row["staff_id"] == "555"
    assert row["company_id"] == "777"


async def test_confirm_processes_each_zone_independently(kb, zone_mapping, monkeypatch):
    """Одна зона пропущена, другая принята — решения не связаны между
    собой, оператор проходит их по очереди."""
    answers = iter(["n", "", "", ""])   # "n" на первой зоне, затем Enter x3 на второй
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    proposals = {
        "bath_russian": Candidate(service=_service("1", "a"), score=0.5),
        "yurt": Candidate(service=_service("2", "b"), score=0.5),
    }

    await confirm_and_apply(kb, proposals, zone_mapping)

    assert zone_mapping.get("bath_russian") is None
    assert zone_mapping.get("yurt")["service_id"] == "2"


async def test_missing_stdin_skips_rather_than_guesses(kb, zone_mapping, monkeypatch):
    """Нет интерактивного stdin — EOFError. Пропустить зону безопаснее, чем
    трактовать пустую строку как «да» и записать непроверенное значение."""
    def _raise(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    proposals = {"bath_russian": Candidate(service=_service("42", "русская баня"), score=0.7)}

    await confirm_and_apply(kb, proposals, zone_mapping)

    assert zone_mapping.get("bath_russian") is None


# --------------------------------------------------------------------------
# Живая проверка (пункт 4) — FREE/BUSY проходят, UNKNOWN явно предупреждает
# --------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, status: AvailabilityStatus, reason: str | None = None):
        self.status = status
        self.reason = reason
        self.calls: list[tuple[str, date]] = []

    async def check_availability(self, zone_id, date, start_time=None, hours=None):
        self.calls.append((zone_id, date))
        return Availability(status=self.status, reason=self.reason, free_slots=("10:00", "14:00"))


async def test_verify_live_reports_free(kb, zone_mapping, capsys):
    await zone_mapping.set("bath_russian", service_id="1", staff_id="10")
    provider = _FakeProvider(AvailabilityStatus.FREE)

    await verify_live(provider, kb, zone_mapping, DATE)

    out = capsys.readouterr().out
    assert "FREE" in out
    assert "UNKNOWN" not in out
    assert provider.calls == [("bath_russian", DATE)]


async def test_verify_live_reports_busy(kb, zone_mapping, capsys):
    await zone_mapping.set("bath_russian", service_id="1", staff_id="10")
    provider = _FakeProvider(AvailabilityStatus.BUSY)

    await verify_live(provider, kb, zone_mapping, DATE)

    assert "BUSY" in capsys.readouterr().out


async def test_verify_live_warns_loudly_on_unknown(kb, zone_mapping, capsys):
    """Именно ради этого пункт 4 существует: пустой/неверный staff_id даёт
    UNKNOWN, и это обязано выглядеть как предупреждение, а не как «готово»."""
    await zone_mapping.set("bath_russian", service_id="1")   # staff_id не задан
    provider = _FakeProvider(AvailabilityStatus.UNKNOWN, reason="зона не заведена")

    await verify_live(provider, kb, zone_mapping, DATE)

    out = capsys.readouterr().out
    assert "UNKNOWN" in out
    assert "⚠" in out


async def test_verify_live_with_nothing_mapped_does_not_call_the_provider(kb, zone_mapping, capsys):
    provider = _FakeProvider(AvailabilityStatus.FREE)

    await verify_live(provider, kb, zone_mapping, DATE)

    assert provider.calls == []
    assert "Нечего проверять" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Форматирование таблицы услуг
# --------------------------------------------------------------------------

def test_money_formats_a_range():
    assert _money(_service("1", "x", price_min=2500, price_max=3500)) == "2500–3500 ₽"


def test_money_formats_a_single_price():
    assert _money(_service("1", "x", price_min=2500, price_max=2500)) == "2500 ₽"


def test_money_handles_missing_price():
    assert "неизвестна" in _money(_service("1", "x"))


def test_duration_converts_seconds_to_a_readable_unit():
    assert _duration(_service("1", "x", duration=3600)) == "1 ч"
    assert _duration(_service("1", "x", duration=1800)) == "30 мин"


def test_duration_handles_missing_value():
    assert "неизвестна" in _duration(_service("1", "x"))
