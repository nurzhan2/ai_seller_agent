"""FastAPI entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as redis_asyncio
from fastapi import FastAPI, Response
from sqlalchemy import text

from app import webhooks
from app.admin import routes as admin_routes
from app.channels import avito_endpoints as ep
from app.config import get_settings
from app.db.session import get_engine
from app.kb.loader import load_catalog
from app.logging_setup import configure_logging
from app.metrics import dry_run_gauge, render_metrics
from app.ops.bot import OpsService
from app.ops.handlers import build_dispatcher
from app.ops.state import InMemoryOpsStore

logger = logging.getLogger("parmangal")


async def supervised_bot_polling(dispatcher: Any, bot: Any) -> None:
    """Обёртка вокруг `dispatcher.start_polling` для фоновой задачи.

    Сбой бота (неверный токен, Telegram недоступен) не должен ронять
    приложение целиком: без бота пропадает только модерация, вебхук и
    остальной сервис продолжают работать. Без этой обёртки исключение
    молча висело в задаче до остановки процесса и всплывало только на
    shutdown — тогда, когда разбираться уже поздно (промт №13, 3.5).
    Вынесено отдельной функцией ради теста — closure внутри lifespan
    протестировать нельзя, не поднимая всё приложение целиком.
    """
    try:
        await dispatcher.start_polling(bot)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("telegram operator bot: polling crashed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Фильтр секретов ставится раньше всего: httpx логирует URL целиком, а у
    # Авито параметры токена идут в query string. См. app/logging_setup.py.
    configure_logging()

    # Load and validate the knowledge base at startup: an invalid KB must
    # stop the process, not surface as a wrong price mid-conversation.
    app.state.kb = load_catalog()
    dry_run_gauge.set(1 if settings.dry_run else 0)

    if settings.dry_run:
        logger.warning("DRY_RUN включён — сообщения клиентам в Авито не уходят")
    if not ep.SPEC_VERIFIED:
        logger.warning(
            "Схема Avito API не подтверждена — исходящие запросы к Авито заблокированы. "
            "См. app/channels/avito_endpoints.py"
        )
    if not settings.avito_webhook_secret.get_secret_value():
        logger.warning(
            "AVITO_WEBHOOK_SECRET не задан — вебхук будет отклонять все запросы. "
            "Сгенерируйте: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    # from_url сам распознаёт rediss:// и переключает соединение на TLS
    # (SSLConnection) — отдельного кода под Railway/managed Redis не нужно,
    # проверено (промт №13, 3.4).
    redis_client = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    app.state.redis = redis_client
    # handler=None: вебхук принимает и дедуплицирует сообщения Авито по-
    # настоящему, но не запускает по ним AgentLoop — полная цепочка
    # «сообщение → агент → ответ клиенту» ещё не собрана в один вызов
    # (отдельная задача, не в этом промте). Дедуп при этом рабочий и
    # проверяется тестами app/webhooks.py уже сейчас.
    webhooks.configure(redis=redis_client, handler=None)

    # Телеграм-бот оператора — фоновая asyncio-задача в этом же процессе, а
    # не отдельный сервис Railway (второй контейнер — лишние деньги, промт
    # №13, 3.5). InMemoryOpsStore: состояние модерации (кто что одобрил,
    # какие чаты у оператора) не переживает перезапуск процесса — известное
    # ограничение, БД-реализация OpsStore не входит в этот промт.
    bot_task: asyncio.Task | None = None
    ops_bot = None
    if settings.telegram_bot_token.get_secret_value():
        from aiogram import Bot

        ops_service = OpsService(store=InMemoryOpsStore(), settings=settings)
        app.state.ops_service = ops_service
        ops_bot = Bot(token=settings.telegram_bot_token.get_secret_value())
        dispatcher = build_dispatcher(ops_service)
        bot_task = asyncio.create_task(supervised_bot_polling(dispatcher, ops_bot))
        logger.info("telegram operator bot: polling started")
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN не задан — операторский бот не запущен, "
            "модерация ответов недоступна"
        )

    yield

    if bot_task is not None:
        bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot_task
    if ops_bot is not None:
        await ops_bot.session.close()
    await redis_client.aclose()
    await get_engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="ПарМангал — ИИ-агент", lifespan=lifespan)
    app.include_router(webhooks.router)
    app.include_router(admin_routes.router)

    @app.get("/metrics")
    async def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    @app.get("/health")
    async def health() -> dict:
        settings = get_settings()
        checks: dict[str, str] = {}

        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001 — health must never raise
            checks["database"] = f"error: {type(exc).__name__}"

        checks["avito_spec_verified"] = str(ep.SPEC_VERIFIED)
        healthy = checks.get("database") == "ok"

        return {
            "status": "ok" if healthy else "degraded",
            "env": settings.env,
            "dry_run": settings.dry_run,
            "checks": checks,
        }

    return app


app = create_app()
