"""Тесты app/main.py: lifespan и устойчивость фоновых задач — бота (промт
№13, 3.5) и воркера отложенных касаний (регламент скидок Максима)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.kb.loader import load_catalog
from app.main import build_touch_sender, supervised_bot_polling, supervised_touch_scheduler
from app.ops.bot import OpsService
from app.ops.state import InMemoryOpsStore


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


# --------------------------------------------------------------------------
# build_touch_sender — маршрутизация DRY_RUN vs реальная отправка
# --------------------------------------------------------------------------

class _FakeOpsBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class _FakeAvitoClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


async def test_dry_run_queues_for_approval_and_notifies_operator():
    settings = Settings(dry_run=True, telegram_ops_chat_id="-100")
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    ops_bot = _FakeOpsBot()
    avito = _FakeAvitoClient()
    send = build_touch_sender(settings, ops_service, ops_bot, avito)

    await send("chat-1", "Вы где-то затерялись?")

    pending = await ops_service.store.get_pending("chat-1")
    assert pending is not None and pending.text == "Вы где-то затерялись?"
    assert len(ops_bot.sent) == 1
    assert ops_bot.sent[0]["chat_id"] == "-100"
    assert avito.sent == []   # DRY_RUN — реальной отправки быть не должно


async def test_dry_run_without_bot_still_queues_for_approval():
    """Без TELEGRAM_BOT_TOKEN уведомления в Telegram не будет, но очередь
    на одобрение всё равно должна работать — иначе касание теряется молча."""
    settings = Settings(dry_run=True)
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    avito = _FakeAvitoClient()
    send = build_touch_sender(settings, ops_service, ops_bot=None, avito_client=avito)

    await send("chat-1", "Будете бронировать или нет?")

    pending = await ops_service.store.get_pending("chat-1")
    assert pending is not None
    assert avito.sent == []


async def test_live_mode_sends_directly_without_queueing():
    settings = Settings(dry_run=False)
    ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
    avito = _FakeAvitoClient()
    send = build_touch_sender(settings, ops_service, ops_bot=None, avito_client=avito)

    await send("chat-1", "Будете бронировать или нет?")

    assert avito.sent == [("chat-1", "Будете бронировать или нет?")]
    assert await ops_service.store.get_pending("chat-1") is None


# --------------------------------------------------------------------------
# supervised_touch_scheduler — устойчивость к сбою одного прохода
# --------------------------------------------------------------------------

class _FakeStoreAlwaysFails:
    async def list_due(self, now, max_count):
        raise RuntimeError("база недоступна")

    async def save(self, chat_id, state):
        raise AssertionError("не должно быть вызвано")


async def test_supervised_scheduler_survives_a_failing_pass_and_keeps_running(caplog):
    kb = load_catalog()

    async def send(chat_id, text):
        pass

    task = asyncio.create_task(
        supervised_touch_scheduler(
            _FakeStoreAlwaysFails(), kb, send,
            delay_minutes=30, max_count=3, interval_seconds=0,
            # Полдень — заведомо внутри рабочего окна 9:00-23:00, иначе тест
            # ловил бы флак в зависимости от того, во сколько его гоняют.
            now_fn=lambda: datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        )
    )
    with caplog.at_level("ERROR", logger="parmangal"):
        for _ in range(50):
            await asyncio.sleep(0.01)
            if "pass failed" in caplog.text:
                break

    assert "pass failed" in caplog.text
    assert not task.done()   # сбой одного прохода не должен останавливать цикл

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_supervised_scheduler_propagates_cancellation():
    kb = load_catalog()

    async def send(chat_id, text):
        pass

    class _EmptyStore:
        async def list_due(self, now, max_count):
            return []

        async def save(self, chat_id, state):
            pass

    task = asyncio.create_task(
        supervised_touch_scheduler(
            _EmptyStore(), kb, send, delay_minutes=30, max_count=3, interval_seconds=999,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------
# Проводка lifespan: что именно попадает в app.state
# --------------------------------------------------------------------------
#
# Эти тесты существуют из-за конкретного бага. Страница /admin/dialogs
# месяцами показывала «Источник диалогов не подключён», хотя конвейер писал
# Chat/Message в базу: маршруты читают request.app.state.dialog_provider, а
# в lifespan его никто не клал. Тесты админки при этом были зелёные — они
# собирают собственное голое FastAPI-приложение и подставляют фейки, то есть
# настоящую проводку не трогают вовсе. Поэтому проверка именно тут: поднять
# РЕАЛЬНОЕ приложение и посмотреть, что оказалось в state.

def _real_app_state():
    """Поднимает настоящее приложение через его lifespan и отдаёт state.

    Без работающих БД и Redis: lifespan к этому готов (zone_mapping.load()
    обёрнут в try/except, redis-клиент подключается лениво), и падать здесь
    нечему — это же свойство проверяется соседними тестами устойчивости.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app):
        return app.state


def test_lifespan_wires_all_four_admin_providers():
    """Ровно тот баг: без этих четырёх строк админка показывает
    «источник не подключён» на данных, которые уже лежат в базе."""
    from app.admin.queries import SqlAlchemyAdminQueries

    state = _real_app_state()

    for name in ("dialog_provider", "lead_provider", "concession_provider", "cost_provider"):
        provider = getattr(state, name, None)
        assert provider is not None, f"app.state.{name} не выставлен — страница админки будет пустой"
        assert isinstance(provider, SqlAlchemyAdminQueries)


def test_lifespan_uses_the_database_backed_ops_store():
    """Состояние модерации обязано переживать рестарт контейнера: на
    InMemoryOpsStore каждый редеплой Railway молча терял очередь одобрений."""
    from app.ops.state import SqlAlchemyOpsStore

    state = _real_app_state()

    assert isinstance(state.ops_service.store, SqlAlchemyOpsStore)


def test_lifespan_wires_the_incoming_pipeline_to_the_webhook():
    """handler=None означает, что вебхук принимает сообщения и молчит."""
    from app import webhooks

    _real_app_state()

    assert webhooks._handler is not None
