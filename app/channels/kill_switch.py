"""Аварийный рубильник исходящих — читается из Redis на каждом проходе.

ПОЧЕМУ НЕ ПЕРЕМЕННАЯ ОКРУЖЕНИЯ. Инцидент 2026-08-28: во время сбоя
`railway variables --set POLLER_ENABLED=false` не подействовал — редеплой,
который подтягивает новые переменные окружения, не приехал сразу, контейнер
продолжал работать со старым значением ещё несколько минут, и за это время
часть сообщений всё равно ушла клиентам. Переменная окружения читается один
раз при старте процесса; чтобы остановить отправку БЕЗ РЕДЕПЛОЯ, рубильник
обязан жить там, куда можно писать прямо сейчас и откуда каждый процесс
читает состояние на лету — то есть в Redis, а не в `Settings`.

ГДЕ ПРОВЕРЯЕТСЯ. Только в `OutboundGate._require_allowed` (см. докстринг
app/channels/outbound_gate.py) — единственной точке, через которую проходят
все четыре пути отправки. Внутрь `OutboundGate.is_allowed` рубильник
СОЗНАТЕЛЬНО не входит: `is_allowed` — это ещё и `can_send` воркера
отложенных касаний (app/ops/touch_scheduler.py), который на `False`
ГАСИТ ТАЙМЕР НАВСЕГДА (чат вне белого списка объявлений — решение
постоянное). Рубильник же временный, снимается через /resume за минуты;
если бы он туда входил, один инцидент насовсем стёр бы все запланированные
напоминания, которые случились быть due в эти минуты.

FAIL CLOSED. Тот же принцип, что у `manual_hold_lookup` в OutboundGate:
не смогли прочитать Redis — трактуем как «стоп», а не как «рубильник
выключен». Молча продолжить слать во время сбоя инфраструктуры хуже, чем
один лишний час простоя, который заметят и снимут руками.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("parmangal.kill_switch")

KEY = "outbound:kill_switch"


@dataclass
class KillSwitchStatus:
    stopped: bool
    by: Optional[int] = None
    at: Optional[str] = None
    reason: str = ""


async def is_stopped(redis: Any) -> bool:
    """True — вся отправка должна быть заблокирована.

    Нет Redis совсем (локальный стенд без него) — рубильник считается
    выключенным: в проде Redis уже обязателен для дедупа входящих и токена
    Авито, так что этот случай — только тесты и разработка без инфры.
    """
    if redis is None:
        return False
    try:
        raw = await redis.get(KEY)
    except Exception:
        logger.exception(
            "kill switch: не удалось прочитать Redis — отправка заблокирована"
        )
        return True
    return raw is not None


async def get_status(redis: Any) -> KillSwitchStatus:
    """Для подтверждающих сообщений в Telegram — кто и когда остановил."""
    if redis is None:
        return KillSwitchStatus(stopped=False)
    try:
        raw = await redis.get(KEY)
    except Exception:
        logger.exception("kill switch: не удалось прочитать статус")
        return KillSwitchStatus(stopped=True)
    if raw is None:
        return KillSwitchStatus(stopped=False)
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        meta = {}
    return KillSwitchStatus(
        stopped=True,
        by=meta.get("by"),
        at=meta.get("at"),
        reason=meta.get("reason", ""),
    )


async def stop(redis: Any, by: int, reason: str = "") -> None:
    if redis is None:
        raise RuntimeError("Redis недоступен — рубильнику негде храниться")
    payload = json.dumps(
        {"by": by, "at": datetime.now(timezone.utc).isoformat(), "reason": reason}
    )
    await redis.set(KEY, payload)
    logger.warning(
        "kill switch: отправка ОСТАНОВЛЕНА", extra={"by": by, "reason": reason}
    )


async def resume(redis: Any, by: int) -> None:
    if redis is None:
        raise RuntimeError("Redis недоступен — рубильнику негде храниться")
    await redis.delete(KEY)
    logger.warning("kill switch: отправка ВОЗОБНОВЛЕНА", extra={"by": by})
