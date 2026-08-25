"""app/kb/overrides.py — разбор пути и наложение поверх сырого документа.

Без БД и без загрузчика: только структура. Наложение поверх реального
catalog.yaml, с полной доменной валидацией — в tests/test_kb_editor.py.
"""

from __future__ import annotations

import pytest

from app.kb.overrides import (
    IdSelector,
    Override,
    OverrideError,
    apply_overrides,
    get_at,
    parse_path,
    set_at,
)


def test_parse_path_keys_and_index():
    assert parse_path("$.a.b[0].c") == ["a", "b", 0, "c"]


def test_parse_path_id_selector():
    assert parse_path("$.zones[id=dome_bags].pricing") == [
        "zones", IdSelector("dome_bags"), "pricing",
    ]


def test_parse_path_requires_dollar_prefix():
    with pytest.raises(OverrideError, match=r"начинаться с '\$'"):
        parse_path("a.b")


def test_parse_path_rejects_empty():
    with pytest.raises(OverrideError, match="пустой путь"):
        parse_path("$")


def test_parse_path_rejects_garbage():
    with pytest.raises(OverrideError, match="непонятный фрагмент"):
        parse_path("$.a..b")


def test_get_at_by_key():
    doc = {"a": {"b": 5}}
    assert get_at(doc, "$.a.b") == 5


def test_get_at_by_index():
    doc = {"a": [10, 20, 30]}
    assert get_at(doc, "$.a[1]") == 20


def test_get_at_by_id_selector():
    doc = {"zones": [{"id": "x", "v": 1}, {"id": "y", "v": 2}]}
    assert get_at(doc, "$.zones[id=y].v") == 2


def test_get_at_missing_key_raises():
    with pytest.raises(OverrideError, match="нет пути"):
        get_at({"a": 1}, "$.b")


def test_get_at_missing_id_raises():
    with pytest.raises(OverrideError, match="нет пути"):
        get_at({"zones": [{"id": "x"}]}, "$.zones[id=missing]")


def test_set_at_replaces_the_leaf():
    doc = {"a": {"b": {"value": 100}}}
    result = set_at(doc, "$.a.b", {"value": 200})
    assert result == {"a": {"b": {"value": 200}}}


def test_set_at_does_not_mutate_the_original():
    """Правка возвращает НОВЫЙ документ — исходный остаётся исходным,
    даже если валидация результата провалится, старый рабочий каталог не
    должен оказаться повреждён."""
    doc = {"a": {"b": 1}}
    result = set_at(doc, "$.a.b", 2)
    assert doc == {"a": {"b": 1}}
    assert result == {"a": {"b": 2}}


def test_set_at_by_id_selector_replaces_the_whole_item():
    doc = {"zones": [{"id": "x", "v": 1}, {"id": "y", "v": 2}]}
    result = set_at(doc, "$.zones[id=y].v", 99)
    assert result == {"zones": [{"id": "x", "v": 1}, {"id": "y", "v": 99}]}


def test_set_at_unknown_path_raises_instead_of_creating_a_key():
    """Опечатка в пути обязана падать понятной ошибкой, а не создавать
    новый ключ рядом — иначе правка выглядела бы сохранённой и ни на что
    не влияла бы."""
    doc = {"a": {"b": 1}}
    with pytest.raises(OverrideError, match="нет пути"):
        set_at(doc, "$.a.typo", 2)


def test_apply_overrides_in_order_last_wins():
    doc = {"a": 1}
    result = apply_overrides(doc, [
        Override(path="$.a", value=2),
        Override(path="$.a", value=3),
    ])
    assert result == {"a": 3}


def test_apply_overrides_empty_list_is_a_noop():
    doc = {"a": 1}
    assert apply_overrides(doc, []) == doc


def test_apply_overrides_across_different_paths():
    doc = {"a": 1, "b": {"c": 2}}
    result = apply_overrides(doc, [
        Override(path="$.a", value=10),
        Override(path="$.b.c", value=20),
    ])
    assert result == {"a": 10, "b": {"c": 20}}
