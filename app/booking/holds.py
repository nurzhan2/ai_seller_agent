"""Предварительные брони внутри бота — пока бронь не внесена в YCLIENTS.

ЗАЧЕМ. Бот только ЧИТАЕТ журнал YCLIENTS: у токена интеграции нет прав на
записи, бронь ставит менеджер руками после предоплаты. Между «клиенту
назвали время и цену» и «менеджер внёс бронь» проходят часы, и всё это
время YCLIENTS показывает слот свободным — второму клиенту бот пообещал
бы то же самое. Это и есть накладки, на которые жаловался заказчик
(15.10.2026: «Авито никак не стыкуется с YCLIENTS? У нас накладки»).

КАК. Когда клиенту посчитана цена на конкретное время, слот держится за
его чатом коротко (QUOTE_TTL); когда данные переданы менеджеру — дольше
(HANDOFF_TTL). Для ВСЕХ ОСТАЛЬНЫХ чатов это время занято. Свой чат свою
бронь не видит: клиент может пересчитать цену на то же время.

ХРАНЕНИЕ — Redis (уже есть у процесса), без миграции: ключ на зону и
дату, внутри — по записи на чат. Срок каждой записи проверяется при
чтении; у ключа TTL на максимальный срок, чтобы мусор не копился.
Недоступный Redis не роняет ход: без хранилища бот работает как раньше.
"""

from __future__ import annotations

import json
import logging
import time as time_mod
from dataclasses import dataclass
from datetime import date as DateType
from datetime import time as TimeType
from typing import Any, Optional

logger = logging.getLogger("parmangal.holds")

QUOTE_TTL = 3 * 3600
HANDOFF_TTL = 24 * 3600
KEY = "hold:{zone_id}:{date}"


@dataclass(frozen=True)
class Hold:
    owner: str
    start: int   # минуты от полуночи
    end: int
    expires_at: float

    def label(self) -> str:
        return f"{self.start // 60:02d}:{self.start % 60:02d}–{self.end // 60:02d}:{self.end % 60:02d}"


def _minutes(t: TimeType) -> int:
    return t.hour * 60 + t.minute


class RedisHoldStore:
    def __init__(self, redis: Any, now_fn=time_mod.time):
        self.redis = redis
        self.now_fn = now_fn

    async def add(
        self, zone_id: str, date: DateType, start: TimeType, hours: int,
        owner: str, ttl: int,
    ) -> None:
        key = KEY.format(zone_id=zone_id, date=date.isoformat())
        begin = _minutes(start)
        expires = self.now_fn() + ttl
        current = await self._get(key, owner)
        if current is not None and current.expires_at > expires and (
            current.start, current.end) == (begin, begin + hours * 60):
            return  # не укорачиваем более длинную бронь того же слота
        value = json.dumps({"s": begin, "e": begin + hours * 60, "x": expires})
        try:
            await self.redis.hset(key, owner, value)
            await self.redis.expire(key, HANDOFF_TTL + 3600)
        except Exception:
            logger.exception("holds: не удалось записать", extra={"key": key})

    async def others(self, zone_id: str, date: DateType, owner: str) -> list[Hold]:
        key = KEY.format(zone_id=zone_id, date=date.isoformat())
        try:
            raw = await self.redis.hgetall(key) or {}
        except Exception:
            logger.exception("holds: не удалось прочитать", extra={"key": key})
            return []
        now = self.now_fn()
        out = []
        for who, value in raw.items():
            who = who.decode() if isinstance(who, bytes) else who
            if who == owner:
                continue
            hold = _parse(who, value)
            if hold is not None and hold.expires_at > now:
                out.append(hold)
        return sorted(out, key=lambda h: h.start)

    async def conflicts(
        self, zone_id: str, date: DateType, start: TimeType, hours: Optional[int],
        owner: str,
    ) -> list[Hold]:
        begin = _minutes(start)
        end = begin + (hours or 1) * 60
        return [
            h for h in await self.others(zone_id, date, owner)
            if h.start < end and begin < h.end
        ]

    async def _get(self, key: str, owner: str) -> Optional[Hold]:
        try:
            value = await self.redis.hget(key, owner)
        except Exception:
            return None
        return _parse(owner, value) if value else None


def _parse(owner: str, value: Any) -> Optional[Hold]:
    try:
        data = json.loads(value.decode() if isinstance(value, bytes) else value)
        return Hold(owner=owner, start=int(data["s"]), end=int(data["e"]),
                    expires_at=float(data["x"]))
    except Exception:
        return None


class InMemoryHoldStore(RedisHoldStore):
    """Для тестов: тот же код поверх словаря вместо Redis."""

    class _FakeRedis:
        def __init__(self):
            self.data: dict[str, dict[str, str]] = {}

        async def hset(self, key, field, value):
            self.data.setdefault(key, {})[field] = value

        async def hget(self, key, field):
            return self.data.get(key, {}).get(field)

        async def hgetall(self, key):
            return dict(self.data.get(key, {}))

        async def expire(self, key, seconds):
            return True

    def __init__(self, now_fn=time_mod.time):
        super().__init__(self._FakeRedis(), now_fn=now_fn)
