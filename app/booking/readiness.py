"""Готово ли автобронирование — и что мешает, по пунктам.

ЗАЧЕМ. До этого модуля переключатель автобронирования был двумя флагами в
двух местах — `AUTO_BOOKING_ENABLED` в переменных Railway и
`payment.handoff_on_payment_step` в базе знаний, — а условие, при котором
его можно включать, жило только в комментарии к конфигу: «включать обратно
только после того, как перед постановкой появится гейт на факт оплаты».
Комментарий ничего не блокировал. Флаг `true` плюс `handoff: false` —
и агент ставил бы в YCLIENTS настоящие брони без единой проверки оплаты
(аудит 2026-08-28, app/booking/yclients.py:create_booking).

Теперь условие — код. `auto_booking_blockers` перечисляет всё, чего не
хватает, с пометкой, чьё это решение, а `ToolExecutor._tool_create_booking`
при непустом списке не доходит до YCLIENTS, даже если флаг включён, и пишет
в лог, почему. Включение переключателя, когда пункты закрыты, — одна
переменная; включение раньше — ничего не делает и объясняет почему.

ЧТО СЧИТАЕТСЯ ГОТОВНОСТЬЮ. Всё перечисленное ниже закрыто. Ни одного
пункта нельзя закрыть переменной окружения: либо правкой базы знаний по
решению заказчика, либо кодом — и тогда меняется константа здесь, вместе с
тестом на новый код.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.booking import yclients_endpoints as ep

# Проверка факта предоплаты перед постановкой брони. Её в коде нет: нечем
# проверять, пока не решено, КАК берётся предоплата (см. блокер link_type).
# Сменить на True можно только вместе с самой проверкой и тестом на неё.
PAYMENT_GATE_IMPLEMENTED = False

CUSTOMER = "заказчик"
DEVELOPMENT = "разработка"


@dataclass(frozen=True)
class Blocker:
    code: str
    owner: str     # CUSTOMER | DEVELOPMENT — чьё решение или чья работа
    text: str


def auto_booking_blockers(kb: Any) -> list[Blocker]:
    """Что мешает агенту ставить брони самому. Пустой список — можно."""
    payment = kb.payment.payment
    blockers: list[Blocker] = []

    if payment.handoff_on_payment_step:
        blockers.append(Blocker(
            "handoff", CUSTOMER,
            "этап оплаты передаётся человеку (payment.handoff_on_payment_step: "
            "true в app/kb/payment.yaml) — пока так, бронь в календаре ставит "
            "оператор",
        ))
    if not payment.link_type.is_resolved():
        blockers.append(Blocker(
            "link_type", CUSTOMER,
            "не решено, какая ссылка на оплату: одна на зону или своя на "
            "каждую бронь (вопрос 9.4)",
        ))
    if not ep.PAYMENT_LINK_SUPPORTED:
        blockers.append(Blocker(
            "payment_link", DEVELOPMENT,
            "создание ссылки на оплату в YCLIENTS не подтверждено "
            "(PAYMENT_LINK_SUPPORTED=False в app/booking/yclients_endpoints.py)",
        ))
    if not PAYMENT_GATE_IMPLEMENTED:
        blockers.append(Blocker(
            "payment_gate", DEVELOPMENT,
            "нет проверки, что предоплата пришла, перед постановкой брони",
        ))
    return blockers


def auto_booking_effective(settings: Any, kb: Any) -> bool:
    """Ставит ли агент брони сам — флаг включён И блокеров нет."""
    return bool(getattr(settings, "auto_booking_enabled", False)) and not auto_booking_blockers(kb)


def describe_blockers(blockers: list[Blocker]) -> str:
    return "; ".join(f"[{b.owner}] {b.text}" for b in blockers)
