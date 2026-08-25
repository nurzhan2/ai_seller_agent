"""FastAPI entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis_asyncio
from fastapi import FastAPI, Response
from sqlalchemy import text

from app import webhooks
from app.admin import routes as admin_routes
from app.agent.debounce import human_delay
from app.channels import avito_endpoints as ep
from app.config import Settings, get_settings
from app.db.session import get_engine, get_sessionmaker
from app.kb.loader import KnowledgeBase, load_catalog
from app.logging_setup import configure_logging
from app.metrics import dry_run_gauge, render_metrics
from app.ops.bot import OpsService
from app.ops.handlers import build_dispatcher
from app.ops.notifications import DialogCard, dialog_keyboard, render_dialog_card
from app.ops.state import SqlAlchemyOpsStore
from app.ops.touch_scheduler import SqlAlchemyTouchStore, run_scheduler_pass

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


def build_touch_sender(
    settings: Settings,
    ops_service: OpsService,
    ops_bot: Any,
    avito_client: Any,
):
    """Собирает функцию отправки одного касания.

    DRY_RUN: касание уходит через тот же путь модерации, что и обычные
    ответы агента — очередь на одобрение (`OpsService.queue_reply`) плюс
    карточка оператору в Telegram, если бот настроен. Без бота (нет
    TELEGRAM_BOT_TOKEN) касание всё равно встаёт в очередь — просто
    оператор не увидит уведомление сам, придётся смотреть /admin/dialogs.

    Не DRY_RUN: прямая отправка через AvitoClient — тот сам ещё раз
    проверяет DRY_RUN на своей стороне (защита от рассинхрона настроек),
    так что двойного отправления при гонке настроек не будет.
    """

    async def send(chat_id: str, text_: str) -> None:
        if settings.dry_run:
            await ops_service.queue_reply(chat_id, text_)
            if ops_bot is not None and settings.telegram_ops_chat_id:
                card = DialogCard(
                    chat_id=chat_id,
                    item_title=None,
                    buyer_name=None,
                    client_text="(автоматическое напоминание — клиент долго не отвечает)",
                    agent_text=text_,
                    dry_run=True,
                )
                await ops_bot.send_message(
                    chat_id=settings.telegram_ops_chat_id,
                    text=render_dialog_card(card),
                    reply_markup=dialog_keyboard(chat_id, dry_run=True, taken_over=False),
                )
        else:
            await avito_client.send_message(chat_id, text_)

    return send


async def supervised_touch_scheduler(
    store: SqlAlchemyTouchStore,
    kb: KnowledgeBase,
    send: Any,
    *,
    delay_minutes: int,
    max_count: int,
    interval_seconds: int,
    now_fn: Any = lambda: datetime.now(timezone.utc),
) -> None:
    """Периодический проход воркера отложенных касаний.

    Один сбой прохода (временная недоступность БД/Авито) не должен
    останавливать все последующие — та же логика изоляции, что и у
    `supervised_bot_polling`. `now_fn` — реальное время по умолчанию;
    параметризовано ради теста устойчивости к сбою, который иначе зависел бы
    от того, идёт ли прогон тестов внутри рабочего окна 9:00–23:00."""
    templates = kb.concessions.policy.touch_templates
    window = kb.catalog.constants.working_window
    while True:
        try:
            await run_scheduler_pass(
                store, templates, window, send, now_fn(),
                delay_minutes=delay_minutes, max_count=max_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("touch scheduler: pass failed")
        await asyncio.sleep(interval_seconds)


async def supervised_concession_timeout_scheduler(
    pipeline: Any,
    *,
    interval_seconds: int,
) -> None:
    """Периодический проход по просроченным запросам на скидку — тот же
    приём изоляции сбоя, что и у `supervised_touch_scheduler`: один плохой
    проход (БД/Авито недоступны) не должен останавливать все следующие."""
    while True:
        try:
            await pipeline.check_concession_timeouts()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("concession timeout scheduler: pass failed")
        await asyncio.sleep(interval_seconds)


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
    # Сам обработчик подключается ниже, после того как собраны все его части
    # (провайдер модели, операторский контур, клиент Авито) — см.
    # webhooks.configure(...) в конце этой функции.

    # YCLIENTS — спек подтверждён заказчиком (не наша догадка), см.
    # app/booking/yclients_endpoints.py. Каталог услуг у заказчика ещё
    # пустой (get_services() честно вернёт []). Маппинг зона→услуга —
    # SqlAlchemyZoneMapping поверх таблицы ZoneServiceMap, с кешем в памяти
    # (см. докстринг класса): правки в БД теперь доезжают до живого
    # провайдера, а не теряются в отдельном процессе. app.state.zone_mapping
    # — тот же объект, который уже читает /admin/catalog.
    from app.booking.mapping import SqlAlchemyZoneMapping
    from app.booking.yclients import YClientsProvider

    zone_mapping = SqlAlchemyZoneMapping(get_sessionmaker())
    try:
        await zone_mapping.load()
    except Exception:
        # БД временно недоступна на старте — не должно ронять весь процесс
        # (/health и так честно покажет database: error). Кеш остаётся
        # пустым — get_availability на любую зону вернёт unknown, безопасное
        # вырождение, а не крах старта; следующий рестарт подхватит данные.
        logger.exception("zone mapping: initial load failed, starting with an empty cache")
    app.state.zone_mapping = zone_mapping

    booking_provider = YClientsProvider(
        partner_token=settings.yclients_partner_token.get_secret_value(),
        user_token=settings.yclients_user_token.get_secret_value(),
        company_id=settings.yclients_company_id,
        mapping=zone_mapping,
        redis=redis_client,
    )
    app.state.booking_provider = booking_provider

    # Источники данных для админки. Страницы /admin/dialogs, /admin/leads,
    # /admin/concessions и /admin/costs написаны против этих интерфейсов, но
    # до сих пор ни одна реализация в app.state не клалась — страницы честно
    # писали «источник не подключён», пока показывать действительно было
    # нечего. С появлением конвейера надпись стала враньём: данные в БД есть.
    # Тот же get_sessionmaker(), что и у конвейера, — одна база, один
    # источник правды, никаких расхождений «пишем сюда, читаем оттуда».
    from app.admin.queries import SqlAlchemyAdminQueries

    admin_queries = SqlAlchemyAdminQueries(get_sessionmaker())
    app.state.dialog_provider = admin_queries
    app.state.lead_provider = admin_queries
    app.state.concession_provider = admin_queries
    app.state.cost_provider = admin_queries

    # Телеграм-бот оператора — фоновая asyncio-задача в этом же процессе, а
    # не отдельный сервис Railway (второй контейнер — лишние деньги, промт
    # №13, 3.5). SqlAlchemyOpsStore: состояние модерации (кто что одобрил,
    # какие чаты у оператора, что ждёт кнопки) живёт в БД и переживает
    # рестарт контейнера. Раньше здесь стоял InMemoryOpsStore, и каждый
    # редеплой на Railway молча терял очередь модерации вместе с процессом —
    # клиент при этом уже написал и ждал ответа, который для него готовили.
    # ops_service заводится всегда (не только при наличии токена бота) — он
    # же нужен воркеру отложенных касаний ниже для очереди на одобрение в
    # DRY_RUN, независимо от того, есть ли кому её показать в Telegram.
    ops_service = OpsService(store=SqlAlchemyOpsStore(get_sessionmaker()), settings=settings)
    app.state.ops_service = ops_service

    bot_task: asyncio.Task | None = None
    ops_bot = None
    if settings.telegram_bot_token.get_secret_value():
        from aiogram import Bot

        ops_bot = Bot(token=settings.telegram_bot_token.get_secret_value())
        # /stats теперь тоже с данными: до этого stats_provider не
        # передавался и команда отвечала «Статистика пока недоступна».
        dispatcher = build_dispatcher(
            ops_service,
            stats_provider=lambda: admin_queries.stats(ops_service.store),
        )
        bot_task = asyncio.create_task(supervised_bot_polling(dispatcher, ops_bot))
        logger.info("telegram operator bot: polling started")
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN не задан — операторский бот не запущен, "
            "модерация ответов недоступна"
        )

    # Воркер отложенных касаний (регламент скидок) — фоновой задачей в этом
    # же процессе, состояние в БД (DialogState.touch_*), а не в памяти:
    # рестарт контейнера не теряет запланированные касания, следующий проход
    # воркера сам подхватит просроченные (промт «отложенные сообщения»).
    from app.channels.avito import AvitoClient

    # Один клиент Авито на процесс: и воркеру касаний, и конвейеру входящих.
    # Два отдельных означали бы два пула соединений и два независимых кеша
    # access-токена — второй обновлял бы токен, не зная про первый.
    avito_client = AvitoClient(settings=settings, redis=redis_client)
    touch_store = SqlAlchemyTouchStore(get_sessionmaker())
    touch_sender = build_touch_sender(settings, ops_service, ops_bot, avito_client)
    touch_task = asyncio.create_task(
        supervised_touch_scheduler(
            touch_store, app.state.kb, touch_sender,
            delay_minutes=settings.touch_reminder_delay_minutes,
            max_count=settings.touch_max_count,
            interval_seconds=settings.touch_scheduler_interval_seconds,
        )
    )
    logger.info("touch scheduler: started")

    # Конвейер обработки входящих — последнее звено: вебхук → агент → ответ.
    # Собирается здесь, а не в webhooks.py, потому что ему нужны ВСЕ части
    # выше сразу (провайдер модели, операторский контур, клиент Авито, БД),
    # а вебхук обязан оставаться тонким: принял, дедуплицировал, ответил 200.
    from app.agent.loop import AgentLoop
    from app.agent.providers.factory import build_provider, resolve_models
    from app.dialog_store import SqlAlchemyDialogStore
    from app.pipeline import MessagePipeline

    dialog_model, classifier_model = resolve_models(settings)
    # Одна переменная, а не инлайн в MessagePipeline(store=...): её
    # count_concessions_today нужен ещё и AgentLoop — R10 (дневной лимит
    # уступок) без него никогда не видит реальное число, только 0.
    dialog_store = SqlAlchemyDialogStore(get_sessionmaker())
    agent_loop = AgentLoop(
        client=build_provider(settings),
        kb=app.state.kb,
        dialog_model=dialog_model,
        classifier_model=classifier_model,
        booking_provider=booking_provider,
        concessions_today_provider=dialog_store.count_concessions_today,
    )
    pipeline = MessagePipeline(
        store=dialog_store,
        agent_loop=agent_loop,
        ops_service=ops_service,
        settings=settings,
        kb=app.state.kb,
        avito_client=avito_client,
        ops_bot=ops_bot,
        # Пауза «как живой человек» перед реальной отправкой — не в DRY_RUN,
        # там ответ и так ждёт кнопки оператора (см. MessagePipeline.delay_fn).
        delay_fn=human_delay,
    )
    app.state.pipeline = pipeline

    webhooks.configure(redis=redis_client, handler=pipeline.handle_message)
    logger.info("incoming pipeline: wired to the Avito webhook")

    # Воркер таймаута запроса на скидку — отдельной задачей, не частью
    # воркера касаний выше: разный ритм (минуты, а не полчаса-час) и разная
    # причина существования (незабытый оператор, а не молчание клиента).
    concession_timeout_task = asyncio.create_task(
        supervised_concession_timeout_scheduler(
            pipeline,
            interval_seconds=settings.concession_timeout_scheduler_interval_seconds,
        )
    )
    logger.info("concession timeout scheduler: started")

    yield

    concession_timeout_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await concession_timeout_task
    touch_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await touch_task
    await avito_client.aclose()
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
