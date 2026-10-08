"""Зона по заголовку нового объявления (заголовки — живые, с Авито 2026-10)."""

import pytest

from app.kb.title_zone import zone_from_title


@pytest.mark.parametrize("title, expected", [
    ("Теплые беседки с мангалом в Ватутинках", (None, "dome")),
    ("Беседка с мангалом, весь день - Троицк", (None, "dome")),
    ("Купольная беседка с мангалом в Троицке", (None, "dome")),
    ("Теплая Беседка - Гриль-домик - мясо, отдых, беседы", ("grill_house", None)),
    ("Теплый гриль-домик на весь день по будням", ("grill_house", None)),
    ("Аренда бани в стиле Гараж", ("bath_garage", None)),
    ("Мужская баня-гараж на дровах Своя мангальная зона", ("bath_garage", None)),
    ("Баня на дровах «Замок рыцаря». Атмосферный отдых", ("bath_knight", None)),
    ("Баня на дровах в средневековом стиле", ("bath_knight", None)),
    ("Русская баня на дровах в лучших традициях", ("bath_russian", None)),
    ("Сертификат Баня на юбилей", (None, "bath")),
    ("Теплый шатер30 человек Троицке База отдыха Чайка", ("tent", None)),
    ("Кемпинг с комфортом", ("yurt", None)),
    ("Теплый домик с мангалом, Троицк - весь день", (None, None)),
    ("2-к. апартаменты, 36,6 м², 1/2 эт.", (None, None)),
    (None, (None, None)),
])
def test_zone_from_title(title, expected):
    assert zone_from_title(title) == expected


def test_every_rule_zone_exists_in_catalog():
    from app.kb.loader import load_catalog
    from app.kb.title_zone import _RULES

    kb = load_catalog()
    zones = {z.id for z in kb.catalog.zones}
    cats = {z.category.value for z in kb.catalog.zones}
    for _, zone_id, category in _RULES:
        assert zone_id is None or zone_id in zones, zone_id
        assert category is None or category in cats, category
