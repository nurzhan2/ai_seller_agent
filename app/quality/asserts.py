"""Жёсткие проверки ответов агента.

Это НЕ оценки модели. Каждая проверка — детерминированное правило, которое
либо нарушено, либо нет. Оценки судьи (judge.py) идут отдельно и никогда не
могут «перевесить» FAIL здесь.

Каждое правило закрывает конкретный провал, найденный в разборе реальных
переписок (docs/analysis/) или прямо запрещённый системным промтом.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from app.agent.loop import PRICE_LIKE_WITHOUT_TOOL_CALL as _MONEY
from app.agent.loop import invented_amounts

MAX_REPLY_LENGTH = 700
MAX_QUESTION_MARKS = 1
# Любое число из 3-6 цифр — используется там, где запрещена вообще любая сумма
_BARE_NUMBER = re.compile(r"\b\d{3,6}\b")

_PHONE = re.compile(r"(?:\+?7|8)[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}\b")
_CARD = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")

_BANKS = (
    "озон банк", "озонбанк", "т-банк", "тинькофф", "сбербанк", "сбер",
    "альфа-банк", "альфабанк", "втб", "райффайзен", "газпромбанк",
)

# «забронировал», «бронь подтверждена» — обещание, которого агент давать не может
_BOOKING_CLAIMS = (
    "забронировал", "забронировала", "бронь подтверждена", "бронь закреплена",
    "место за вами", "вы забронировали", "бронирую",
)
_MANAGER_MENTION = ("менеджер", "администратор", "свяж", "подтвердит", "уточню", "придержу")

_DISCOUNT_WORDS = ("скидк", "дешевле", "уступ", "снижу", "снизить цену", "в подарок")

# «1 250 ₽ в час», «по 1666 рублей в час»
_DERIVED_RATE = re.compile(r"\d[\d\s]*\s*(?:₽|руб\w*)\s*(?:в|за)\s*час", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str


@dataclass(frozen=True)
class TurnUnderTest:
    """Один ход агента вместе с тем, что он при этом делал."""

    text: str
    tool_calls: Sequence[str] = ()
    quote_statuses: Sequence[str] = ()
    # Числа, которые вернули инструменты за этот ход (AgentLoop отдаёт их в
    # TurnResult.tool_amounts). Раньше здесь лежал только `total` расчёта, и
    # правило ниже считало выдумкой предоплату и цену шампуров — обе честные
    # и обе из инструментов.
    tool_amounts: Sequence[str] = ()
    concession_granted: bool = False
    known_zone_ids: Sequence[str] = ()
    applied_promo: Optional[str] = None


def _mentions_money(text: str) -> bool:
    return bool(_MONEY.search(text))


def check_turn(turn: TurnUnderTest) -> list[Violation]:
    violations: list[Violation] = []
    text = turn.text or ""
    lowered = text.lower()

    # 1. Цена только через инструмент.
    if _mentions_money(text) and "calculate_price" not in turn.tool_calls:
        violations.append(
            Violation("price_without_tool", "в тексте есть сумма, но calculate_price не вызывался")
        )

    # 2. Названные суммы — из ответов инструментов, а не из головы.
    #
    # Проверка живёт в app/agent/loop.py и работает в проде как рубеж перед
    # отправкой; здесь она ТОЛЬКО вызывается. Своя копия правила разошлась бы
    # с рантаймом молча — ровно та же причина, по которой отсюда же берётся
    # и денежный паттерн.
    if turn.tool_amounts:
        for amount in invented_amounts(text, set(turn.tool_amounts)):
            violations.append(
                Violation("price_mismatch", f"сумма {amount} не из ответа инструмента")
            )

    # 3. При blocked в тексте не должно быть суммы вообще.
    if "blocked" in turn.quote_statuses:
        if _mentions_money(text) or _BARE_NUMBER.search(text):
            violations.append(
                Violation("amount_after_blocked", "статус blocked, но в тексте есть число")
            )

    # 4. Никаких обещаний брони без упоминания менеджера.
    for claim in _BOOKING_CLAIMS:
        if claim in lowered and not any(m in lowered for m in _MANAGER_MENTION):
            violations.append(Violation("booking_promise", f"«{claim}» без упоминания менеджера"))
            break

    # 5. Реквизиты.
    if _PHONE.search(text):
        violations.append(Violation("phone_leak", "в тексте телефонный номер"))
    if _CARD.search(text):
        violations.append(Violation("card_leak", "в тексте похоже на номер карты"))
    for bank in _BANKS:
        if bank in lowered:
            violations.append(Violation("bank_leak", f"упомянут банк: {bank}"))
            break

    # 6. При активной акции нет производной ставки за час.
    if turn.applied_promo and _DERIVED_RATE.search(text):
        violations.append(
            Violation("derived_rate", "при акции показана ставка за час — клиент запомнит её")
        )

    # 7. Скидка только после request_concession.
    if any(word in lowered for word in _DISCOUNT_WORDS) and not turn.concession_granted:
        violations.append(
            Violation("unauthorised_discount", "речь о скидке без разрешённой уступки")
        )

    # 8. Длина.
    if len(text) > MAX_REPLY_LENGTH:
        violations.append(Violation("too_long", f"{len(text)} символов, лимит {MAX_REPLY_LENGTH}"))

    # 9. Не больше одного вопроса за сообщение.
    if text.count("?") > MAX_QUESTION_MARKS:
        violations.append(
            Violation("too_many_questions", f"{text.count('?')} вопросов — получается анкета")
        )

    # 10. Только зоны из каталога.
    if turn.known_zone_ids:
        for invented in _invented_zones(lowered, turn.known_zone_ids):
            violations.append(Violation("unknown_zone", f"зона вне каталога: {invented}"))

    return violations


_ZONE_WORDS = {
    "баня": ("bath_russian", "bath_garage", "bath_knight"),
    "домик для отдыха": ("house_relax",),
    "купол": ("dome_bags", "dome_blue_chairs", "dome_chairs"),
    "сфера": ("dome_bags", "dome_blue_chairs", "dome_chairs"),
    "гриль-домик": ("grill_house",),
    "шатёр": ("tent",),
    "шатер": ("tent",),
    "юрта": ("yurt",),
}

# То, чего у комплекса нет вовсе — если агент это упомянул, он выдумал услугу.
_NON_EXISTENT = (
    "бассейн", "сауна-хамам", "хамам", "джакузи", "банкетный зал",
    "гостиница", "отель", "коттедж", "вип-зона",
)


def _invented_zones(lowered: str, known: Iterable[str]) -> list[str]:
    return [word for word in _NON_EXISTENT if word in lowered]


def summarize(violations: Sequence[Violation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for violation in violations:
        counts[violation.rule] = counts.get(violation.rule, 0) + 1
    return counts
