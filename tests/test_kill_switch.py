"""app/channels/kill_switch.py — аварийный рубильник в Redis, не в env.

Повод: во время инцидента 2026-08-28 `railway variables --set
POLLER_ENABLED=false` не подействовал сразу — редеплой задержался, и
контейнер ещё несколько минут слал сообщения со старым значением. Рубильник
должен читаться из Redis на каждом проходе и работать без редеплоя.
"""

from __future__ import annotations

import pytest

from app.channels import kill_switch


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


class BrokenRedis:
    async def get(self, key):
        raise ConnectionError("Redis недоступен")

    async def set(self, key, value, **kwargs):
        raise ConnectionError("Redis недоступен")

    async def delete(self, key):
        raise ConnectionError("Redis недоступен")


# --------------------------------------------------------------------------
# Базовое поведение
# --------------------------------------------------------------------------

async def test_off_by_default():
    redis = FakeRedis()
    assert await kill_switch.is_stopped(redis) is False


async def test_stop_then_is_stopped():
    redis = FakeRedis()
    await kill_switch.stop(redis, by=111, reason="инцидент")
    assert await kill_switch.is_stopped(redis) is True


async def test_resume_clears_it():
    redis = FakeRedis()
    await kill_switch.stop(redis, by=111)
    await kill_switch.resume(redis, by=111)
    assert await kill_switch.is_stopped(redis) is False


async def test_status_reports_who_and_when():
    redis = FakeRedis()
    await kill_switch.stop(redis, by=42, reason="проверка лимита")
    status = await kill_switch.get_status(redis)
    assert status.stopped is True
    assert status.by == 42
    assert status.reason == "проверка лимита"
    assert status.at  # непустая метка времени


async def test_status_when_not_stopped():
    redis = FakeRedis()
    status = await kill_switch.get_status(redis)
    assert status.stopped is False
    assert status.by is None


# --------------------------------------------------------------------------
# Fail closed — сбой Redis трактуется как «стоп»
# --------------------------------------------------------------------------

async def test_read_failure_blocks_rather_than_allows():
    """Сбой инфраструктуры не должен молча выглядеть как «рубильник
    выключен» — иначе сбойный Redis не защищает вообще ни от чего."""
    assert await kill_switch.is_stopped(BrokenRedis()) is True


async def test_status_read_failure_reports_stopped():
    status = await kill_switch.get_status(BrokenRedis())
    assert status.stopped is True


# --------------------------------------------------------------------------
# Нет Redis совсем — только тесты/локальный стенд, не прод
# --------------------------------------------------------------------------

async def test_no_redis_is_not_stopped():
    assert await kill_switch.is_stopped(None) is False


async def test_stop_without_redis_raises():
    with pytest.raises(RuntimeError):
        await kill_switch.stop(None, by=1)


async def test_resume_without_redis_raises():
    with pytest.raises(RuntimeError):
        await kill_switch.resume(None, by=1)
