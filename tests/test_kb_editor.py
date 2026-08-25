"""app/kb/editor.py — правка каталога с реальной валидацией и реальным
пересчётом цены. `InMemoryOverrideStore`: правила проверяются здесь, а что
они переживают перезапуск — в tests/test_sql_stores.py на настоящем
Postgres.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.kb.editable import field_by_key
from app.kb.editor import CatalogEditor, human_value, price_example
from app.kb.override_store import InMemoryOverrideStore
from app.kb.overrides import OverrideError

USER = 111
OTHER_USER = 222


@pytest.fixture
def editor():
    return CatalogEditor(InMemoryOverrideStore())


# --------------------------------------------------------------------------
# Наложение / чтение текущего значения
# --------------------------------------------------------------------------

async def test_current_value_before_any_edit_matches_yaml(editor):
    field = field_by_key("we_hour")
    value = await editor.current_value(field, "dome_bags")
    assert value["value"] == 1500


async def test_current_kb_without_overrides_equals_plain_load_catalog(editor):
    from app.kb.loader import load_catalog

    kb = await editor.current_kb()
    plain = load_catalog()
    assert kb.catalog.zones[0].pricing == plain.catalog.zones[0].pricing


# --------------------------------------------------------------------------
# Правка сохраняется и применяется
# --------------------------------------------------------------------------

async def test_preview_shows_was_and_will_be(editor):
    field = field_by_key("we_hour")
    preview = await editor.preview("we_hour", "1800", USER, "dome_bags")
    assert preview.previous_human == "1500"
    assert preview.new_human == "1800"
    assert preview.field is field


async def test_apply_persists_the_edit(editor):
    preview = await editor.preview("we_hour", "1800", USER, "dome_bags")
    await editor.apply(preview, USER)

    value = await editor.current_value(field_by_key("we_hour"), "dome_bags")
    assert value["value"] == 1800


async def test_apply_marks_resolved_from_with_the_operator(editor):
    """Пункт 7: правка спорного (и обычного) поля помечает происхождение
    user_id-ом того, кто её внёс."""
    preview = await editor.preview("we_hour", "1800", USER, "dome_bags")
    await editor.apply(preview, USER)

    value = await editor.current_value(field_by_key("we_hour"), "dome_bags")
    assert f"user_id={USER}" in value["resolved_from"]
    assert "disputed" not in value


async def test_second_edit_of_the_same_field_wins(editor):
    p1 = await editor.preview("we_hour", "1800", USER, "dome_bags")
    await editor.apply(p1, USER)
    p2 = await editor.preview("we_hour", "2000", USER, "dome_bags")
    await editor.apply(p2, USER)

    value = await editor.current_value(field_by_key("we_hour"), "dome_bags")
    assert value["value"] == 2000


async def test_edit_survives_a_new_editor_instance(editor):
    """То же хранилище, «новый процесс» — эквивалент рестарта контейнера."""
    preview = await editor.preview("we_hour", "1800", USER, "dome_bags")
    await editor.apply(preview, USER)

    fresh = CatalogEditor(editor.store)
    value = await fresh.current_value(field_by_key("we_hour"), "dome_bags")
    assert value["value"] == 1800


# --------------------------------------------------------------------------
# Невалидное значение отклоняется
# --------------------------------------------------------------------------

async def test_negative_price_is_rejected(editor):
    with pytest.raises(OverrideError, match="отрицательной"):
        await editor.preview("we_hour", "-500", USER, "dome_bags")


async def test_non_numeric_price_is_rejected(editor):
    with pytest.raises(OverrideError, match="не похоже на цену"):
        await editor.preview("we_hour", "недорого", USER, "dome_bags")


async def test_absurd_price_is_rejected_as_a_typo():
    with pytest.raises(OverrideError, match="опечатк"):
        field_by_key("we_hour").parse("99999999")


async def test_min_hours_cannot_exceed_the_domain_maximum(editor):
    with pytest.raises(OverrideError, match="больше"):
        await editor.preview("min_h", "48", USER, "bath_russian")


async def test_min_hours_cannot_be_zero(editor):
    with pytest.raises(OverrideError, match="меньше 1"):
        await editor.preview("min_h", "0", USER, "bath_russian")


async def test_bad_time_format_is_rejected(editor):
    with pytest.raises(OverrideError, match="ЧЧ:ММ"):
        await editor.preview("work_from", "25:00", USER)


async def test_bad_date_format_is_rejected(editor):
    with pytest.raises(OverrideError, match="ММ-ДД"):
        await editor.preview("holidays", "13-45", USER)


async def test_rejected_edit_is_not_saved(editor):
    """Отклонённое значение не должно молча осесть в хранилище."""
    with pytest.raises(OverrideError):
        await editor.preview("we_hour", "-500", USER, "dome_bags")

    assert await editor.store.list_active() == []
    value = await editor.current_value(field_by_key("we_hour"), "dome_bags")
    assert value["value"] == 1500     # исходное значение из YAML


async def test_preview_reruns_full_kb_validation_not_just_the_field_parser(editor, monkeypatch):
    """Пункт 2 задачи буквально: «любая правка проходит ТУ ЖЕ валидацию,
    что и YAML при старте», а не только проверку одного поля. Ни одно
    настоящее поле сегодня не может подсунуть структурно ломающее значение
    (парсеры сами этого не допускают), поэтому здесь — искусственное поле,
    которое возвращает то, что реальный парсер никогда не вернёт (число
    вместо строки времени), чтобы доказать: полная валидация документа
    ДЕЙСТВИТЕЛЬНО выполняется на каждой правке, а не только объявлена."""
    import dataclasses

    import app.kb.editor as editor_module
    from app.kb.editable import field_by_key

    broken_field = dataclasses.replace(field_by_key("work_from"), parse=lambda text: 12345)
    monkeypatch.setattr(editor_module, "field_by_key", lambda key: broken_field)

    with pytest.raises(OverrideError, match="проверку базы знаний"):
        await editor.preview("work_from", "anything", USER)


async def test_unknown_field_key_is_rejected(editor):
    with pytest.raises(OverrideError, match="Неизвестное поле"):
        await editor.preview("does_not_exist", "1", USER, "dome_bags")


async def test_zone_field_without_zone_id_is_rejected(editor):
    with pytest.raises(OverrideError, match="требует указания зоны"):
        await editor.preview("we_hour", "1800", USER, zone_id=None)


# --------------------------------------------------------------------------
# Откат
# --------------------------------------------------------------------------

async def test_revert_restores_the_previous_value(editor):
    preview = await editor.preview("we_hour", "1800", USER, "dome_bags")
    await editor.apply(preview, USER)

    result = await editor.revert_last(USER)

    assert result is not None
    value = await editor.current_value(field_by_key("we_hour"), "dome_bags")
    assert value["value"] == 1500


async def test_revert_with_nothing_to_revert_returns_none(editor):
    assert await editor.revert_last(USER) is None


async def test_revert_only_undoes_the_most_recent_edit(editor):
    p1 = await editor.preview("we_hour", "1800", USER, "dome_bags")
    r1 = await editor.apply(p1, USER)
    p2 = await editor.preview("wd_hour", "1200", USER, "dome_bags")
    await editor.apply(p2, USER)

    await editor.revert_last(USER)

    # Последняя (будни) откачена, предпоследняя (выходные) остаётся в силе.
    we = await editor.current_value(field_by_key("we_hour"), "dome_bags")
    wd = await editor.current_value(field_by_key("wd_hour"), "dome_bags")
    assert we["value"] == 1800
    assert wd["value"] == 1000        # вернулось к исходному (dome_bags.weekday_per_hour)


async def test_reverted_row_is_not_reverted_again(editor):
    preview = await editor.preview("we_hour", "1800", USER, "dome_bags")
    await editor.apply(preview, USER)
    await editor.revert_last(USER)

    assert await editor.revert_last(USER) is None


async def test_journal_keeps_both_the_edit_and_the_revert(editor):
    """История не удаляется — откат добавляет факт, а не стирает прошлое."""
    preview = await editor.preview("we_hour", "1800", USER, "dome_bags")
    await editor.apply(preview, USER)
    await editor.revert_last(USER)

    journal = await editor.store.list_journal()
    assert len(journal) == 1
    assert journal[0].is_active is False
    assert journal[0].reverted_by == USER


# --------------------------------------------------------------------------
# Пересчёт примера цены (пункт 4)
# --------------------------------------------------------------------------

async def test_apply_returns_a_recalculated_price_example(editor):
    preview = await editor.preview("we_hour", "1000", USER, "dome_bags")
    result = await editor.apply(preview, USER)

    assert result.price_example is not None
    assert "4000" in result.price_example          # 1000 ₽/ч × 4 ч
    assert "суббот" in result.price_example.lower()


async def test_price_example_reflects_the_new_value_not_the_old(editor):
    before = price_example(await editor.current_kb(), "dome_bags")
    preview = await editor.preview("we_hour", "3000", USER, "dome_bags")
    result = await editor.apply(preview, USER)

    assert before != result.price_example
    assert "12000" in result.price_example          # 3000 ₽/ч × 4 ч


async def test_revert_also_recalculates_the_price_example(editor):
    preview = await editor.preview("we_hour", "1000", USER, "dome_bags")
    await editor.apply(preview, USER)

    result = await editor.revert_last(USER)

    assert result.price_example is not None
    assert "6000" in result.price_example           # обратно к исходным 1500 × 4


async def test_price_example_is_none_for_a_schedule_field(editor):
    """График не привязан к зоне — пересчитывать пример цены нечего."""
    preview = await editor.preview("work_from", "10:00", USER, zone_id=None)
    result = await editor.apply(preview, USER)

    assert result.price_example is None


def test_human_value_renders_disputed_value_leaf():
    assert human_value({"value": 1500}) == "1500"


def test_human_value_renders_unset_disputed_leaf():
    assert "спорное" in human_value({"disputed": {"question_id": "1.1"}})


def test_human_value_renders_a_list():
    assert human_value(["01-01", "05-09"]) == "01-01, 05-09"
