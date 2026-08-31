"""Суточный лимит исходящих — считается и срабатывает в OutboundGate.

ПОЧЕМУ НЕ В КОНВЕЙЕРЕ. Если бы счётчик стоял в `app/pipeline.py:_deliver`,
как дневной лимит ЦЕНОВЫХ УСТУПОК (см. `daily_limit_exhausted` в
app/pricing/concessions.py — другая метрика, не эта), три остальных выхода
(запасной ответ по таймауту уступки, отложенное касание, ответ оператора)
считали бы себя свободными от него. Тот же класс ошибки, что уже стоил
белому списку объявлений отдельного инцидента — см. докстринг
app/channels/outbound_gate.py.

СЧИТАЕТСЯ ТОЛЬКО ТО, ЧТО РЕАЛЬНО УХОДИТ. Проверка стоит в
`OutboundGate._require_allowed` ПОСЛЕ kill switch и белого списка
объявлений: сообщение, заблокированное ими, клиенту не ушло и суточный
лимит не должно трогать.

СУТКИ — ПО МОСКВЕ, как и у лимита ценовых уступок
(`app/dialog_store.py:count_concessions_today`, `MOSCOW_TZ`): полночь по
UTC для заказчика в Москве — это 3 часа ночи по местному, разрыв дня в
неудобное для метрики время. Константа продублирована, а не импортирована
оттуда: у `app/channels/*` сознательно нет зависимости на `dialog_store`
(гейт получает всё через колбэки, а не через прямой импорт стора — см.
докстринг outbound_gate.py про `ItemIdLookup`/`ManualHoldLookup`).

FAIL CLOSED НА СБОЕ REDIS, ТАК ЖЕ, КАК KILL SWITCH — ЭТО ИЗМЕНЕНИЕ. Раньше
здесь стоял fail open («не смогли посчитать — отправляем как обычно»), и
логика была ровно наоборот: рассуждение «это счётчик объёма, а не проверка
доступа» смотрит на лимит с точки зрения обычного дня. Но лимит существует
ИМЕННО на случай, когда что-то уже пошло не так (утечка, баг, массовая
рассылка) — а значит его предохранитель обязан сработать и тогда, когда
инфраструктура, которой он посчитан, тоже отказала. Fail open в этой
ситуации означает «предохранителя нет именно в тот момент, когда он нужнее
всего» — то же рассуждение, которое уже привело kill switch к fail closed
(см. докстринг app/channels/kill_switch.py). Отличие от него только в одном:
`redis is None` (Redis не настроен вообще — локальный стенд, тесты) — это
НЕ авария, а отсутствие инфраструктуры по конфигурации, и здесь трактуется
так же, как в kill switch и в app/channels/inbound_dedup.py — прозрачно.
Авария — это когда Redis ЕСТЬ, но `incr`/`expire` бросили исключение;
только этот случай блокирует отправку и помечается `redis_unavailable=True`
(OutboundGate шлёт по нему алерт в Telegram — см. app/main.py:
build_daily_limit_alert).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("parmangal.outbound.daily_limit")

from app.clock import MOSCOW_TZ  # единый источник, см. app/clock.py
KEY_PREFIX = "outbound:daily_count:"
# С запасом на двое суток: TTL обновляется на каждый инкремент, ключ не
# накапливается годами, даже если инкременты идут секунда в секунду с
# границей полуночи.
KEY_TTL_SECONDS = 2 * 24 * 60 * 60


def _key_for(now: datetime) -> str:
    day = now.astimezone(MOSCOW_TZ).date().isoformat()
    return f"{KEY_PREFIX}{day}"


@dataclass
class DailyLimitResult:
    allowed: bool
    count: int
    limit: int
    # True РОВНО на (limit + 1)-м сообщении — момент, когда лимит только
    # что исчерпан и нужно послать алерт. На (limit + 2)-м и далее уже
    # False: алерт не должен повторяться на каждое заблокированное сообщение
    # до конца суток.
    just_exceeded: bool
    # True — не сам лимит исчерпан, а не удалось его проверить (Redis
    # настроен, но `incr`/`expire` упали). В отличие от `just_exceeded`
    # взводится на КАЖДОЙ такой попытке, пока авария не устранена: без
    # рабочего Redis нечем отличить первую заблокированную попытку от
    # сотой, а молчать после первого алерта в аварии, которая длится,
    # хуже, чем повторяться.
    redis_unavailable: bool = False


async def check_and_increment(
    redis: Any, limit: int, now: Optional[datetime] = None,
) -> DailyLimitResult:
    """Лимит `<= 0` — выключен, в Redis за ним даже не ходим."""
    if limit <= 0:
        return DailyLimitResult(allowed=True, count=0, limit=limit, just_exceeded=False)

    now = now or datetime.now(timezone.utc)
    key = _key_for(now)

    if redis is None:
        # Redis не настроен вообще (локальный стенд, тесты) — не авария, а
        # отсутствие инфраструктуры по конфигурации. Тот же принцип, что и
        # в kill_switch.is_stopped и в inbound_dedup.claim.
        return DailyLimitResult(allowed=True, count=0, limit=limit, just_exceeded=False)

    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, KEY_TTL_SECONDS)
    except Exception:
        logger.exception(
            "daily limit: не удалось прочитать/увеличить счётчик в Redis — "
            "отправка заблокирована (fail closed, как и kill switch)"
        )
        return DailyLimitResult(
            allowed=False, count=-1, limit=limit, just_exceeded=False, redis_unavailable=True,
        )

    return DailyLimitResult(
        allowed=count <= limit,
        count=count,
        limit=limit,
        just_exceeded=count == limit + 1,
    )
