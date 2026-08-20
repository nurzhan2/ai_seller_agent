"""Тесты app/main.py: lifespan и устойчивость фоновой задачи бота
(промт №13, 3.5)."""

from __future__ import annotations

import asyncio

import pytest

from app.main import supervised_bot_polling


class _FakeDispatcherOk:
    async def start_polling(self, bot):
        await asyncio.sleep(0)   # ведёт себя как реальный опрос — просто не падает


class _FakeDispatcherRunsForever:
    async def start_polling(self, bot):
        await asyncio.Event().wait()   # как реальный опрос — висит, пока не отменят


class _FakeDispatcherCrashes:
    async def start_polling(self, bot):
        raise RuntimeError("Telegram server says - Unauthorized")


async def test_supervised_polling_swallows_crash_and_logs(caplog):
    """Сбой бота (неверный токен и т.п.) не должен всплывать наружу —
    иначе он роняет весь lifespan приложения на shutdown, а не только бота."""
    with caplog.at_level("ERROR", logger="parmangal"):
        await supervised_bot_polling(_FakeDispatcherCrashes(), bot=None)
    assert "polling crashed" in caplog.text


async def test_supervised_polling_propagates_cancellation():
    """Отмена задачи при остановке приложения должна проходить насквозь —
    иначе фоновая задача не остановится вместе с приложением."""
    task = asyncio.create_task(supervised_bot_polling(_FakeDispatcherRunsForever(), bot=None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_supervised_polling_returns_normally_when_dispatcher_succeeds():
    await supervised_bot_polling(_FakeDispatcherOk(), bot=None)   # не должно бросить
