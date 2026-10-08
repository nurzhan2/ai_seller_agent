"""Зона по заголовку объявления — когда в item_zone_map строки нет.

Заказчик регулярно заводит новые объявления (2026-10: «Баня на дровах
«Замок рыцаря»», «Аренда бани в стиле Гараж», сертификаты), а ручное
сопоставление отстаёт. Без зоны бот отвечал «обзором всех зон» и на
вопрос про беседку-купол рассказывал про гриль-домик (жалоба 08.10).

Возвращает (zone_id, category) — ровно одно из двух непустое, либо
(None, None), если заголовок ничего не говорит. Ручная строка в
item_zone_map ВСЕГДА главнее: эта функция зовётся только без неё.

«Беседка» без слова «гриль» — купол: у заказчика гриль-домик всегда
называется гриль-домиком, а «беседки с мангалом» по 1000 ₽/ч и «весь
день» за 4999 ₽ — это цены куполов.
"""

from __future__ import annotations

import re
from typing import Optional

_RULES: list[tuple[str, Optional[str], Optional[str]]] = [
    # порядок важен: специфичное раньше общего
    (r"сертификат", None, "bath"),
    (r"рыцар|средневеков|замок", "bath_knight", None),
    (r"гараж", "bath_garage", None),
    (r"русск\w* бан", "bath_russian", None),
    (r"купол|полусфер", None, "dome"),
    (r"гриль", "grill_house", None),
    (r"шат[её]р|шатр", "tent", None),
    (r"юрт|кемпинг", "yurt", None),
    (r"беседк", None, "dome"),
    (r"бан[яиюе]\b|бань", None, "bath"),
]


def zone_from_title(title: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not title:
        return None, None
    text = title.lower().replace("ё", "е")
    for pattern, zone_id, category in _RULES:
        if re.search(pattern.replace("ё", "е"), text):
            return zone_id, category
    return None, None
