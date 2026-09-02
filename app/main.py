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
from app.media.photos import KbPhotoProvider
from app.metrics import DailyCostGuard, dry_run_gauge, render_metrics
from app.ops.bot import OpsService
from app.ops.handlers import build_dispatcher, set_bot_commands
from app.ops.notifications import (
    DialogCard,
    dialog_keyboard,
    render_booking_handoff,
    render_booking_notice,
    render_daily_cost_limit_notice,
    render_dialog_card,
    render_outbound_daily_limit_notice,
    render_outbound_daily_limit_unavailable_notice,
)
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


def build_booking_notifier(settings: Any, ops_bot: Any):
    """Уведомление оператору о поставленной броне — БЕЗ КНОПОК.

    Бронь уже в YCLIENTS, одобрять нечего (см. render_booking_notice).
    Без бота или без TELEGRAM_OPS_CHAT_ID возвращается None — тогда
    `_tool_create_booking` просто не уведомляет, и это не мешает брони:
    факт остаётся в нашей таблице `bookings` и в логе.
    """
    if ops_bot is None or not getattr(settings, "telegram_ops_chat_id", ""):
        return None

    async def notify(record: dict) -> None:
        await ops_bot.send_message(
            chat_id=settings.telegram_ops_chat_id,
            text=render_booking_notice(record),
        )

    return notify


def build_booking_handoff_notifier(settings: Any, ops_bot: Any):
    """Карточка «поставьте бронь руками» — этап оплаты ведёт человек.

    Тоже без кнопок, но по обратной причине, чем `build_booking_notifier`:
    там одобрять нечего, потому что бронь уже стоит; здесь — потому что
    нужна работа, а не одобрение. Кнопки «Взять на себя» и ссылка на чат
    приходят отдельной карточкой диалога: ход помечен эскалацией
    (app/agent/tools.py:PAYMENT_HANDOFF_REASON), и конвейер шлёт её сам.

    Без бота или без TELEGRAM_OPS_CHAT_ID возвращается None — передача от
    этого не отменяется: бронь всё равно не ставится, чат всё равно
    эскалирован и виден в /admin/dialogs.
    """
    if ops_bot is None or not getattr(settings, "telegram_ops_chat_id", ""):
        return None

    async def notify(card: dict) -> None:
        await ops_bot.send_message(
            chat_id=settings.telegram_ops_chat_id,
            text=render_booking_handoff(card),
        )

    return notify


def _takeover_lookup(ops_service: Any):
    """`ChatFlags` операторского стора -> `TakeoverState` границы исходящих.

    Переходник, а не прямая передача `get_flags`: гейт намеренно знает только
    два поля и ничего — про операторский контур (см. TakeoverState).
    """

    from app.channels.outbound_gate import TakeoverState

    async def lookup(chat_id: str) -> TakeoverState:
        flags = await ops_service.store.get_flags(chat_id)
        return TakeoverState(flags.is_human_takeover, flags.takeover_at)

    return lookup


def build_cost_guard(settings: Any, ops_bot: Any) -> DailyCostGuard:
    """Предохранитель по расходу на модели — с реальной паузой агента.

    Пауза ставится тем же способом, что и оператором из Telegram
    (`settings.agent_paused`, см. app/ops/bot.py:pause_all): один рубильник,
    один способ снять — /resume. Заводить второй флаг «остановлен из-за
    денег» значило бы получить состояние, о котором /resume не знает.

    Алерт уходит `create_task`, а не `await`: `DailyCostGuard.add` вызывается
    из синхронного участка хода агента, и ждать там Telegram нельзя — клиент
    в это время ждёт ответа. Без бота или без TELEGRAM_OPS_CHAT_ID пауза всё
    равно ставится: молча остановиться хуже, чем остановиться с записью
    только в логе, но продолжать тратить деньги хуже обоих вариантов.
    """

    def on_pause() -> None:
        settings.agent_paused = True
        logger.error(
            "DAILY_COST_LIMIT_RUB=%s исчерпан — агент поставлен на паузу, "
            "снять только через /resume",
            settings.daily_cost_limit_rub,
        )
        if ops_bot is None or not getattr(settings, "telegram_ops_chat_id", ""):
            return
        text = render_daily_cost_limit_notice(guard.spent, guard.limit_rub)

        async def send() -> None:
            try:
                await ops_bot.send_message(
                    chat_id=settings.telegram_ops_chat_id, text=text
                )
            except Exception:
                logger.exception("cost limit alert send failed")

        try:
            asyncio.get_running_loop().create_task(send())
        except RuntimeError:
            # Цикла нет (тест зовёт add() синхронно) — пауза уже стоит,
            # это главное; алерт в таком контексте отправлять некуда.
            logger.warning("cost limit alert skipped: нет работающего event loop")

    guard = DailyCostGuard(settings.daily_cost_limit_rub, on_pause=on_pause)
    return guard


def build_daily_limit_alert(settings: Any, ops_bot: Any):
    """Алерт в Telegram — не тихая остановка, ни на исчерпанный лимит, ни
    на аварию Redis, которым он считается.

    Два разных сообщения через один колбэк — `DailyLimitResult` знает,
    какой случай перед нами: `just_exceeded` (ровно (лимит + 1)-е
    сообщение, дальше до конца суток не повторяется) или
    `redis_unavailable` (fail closed на каждой попытке, пока Redis не
    поднимется — см. докстринг app/channels/daily_limit.py про то, почему
    именно на каждой).
    """
    if ops_bot is None or not getattr(settings, "telegram_ops_chat_id", ""):
        return None

    async def alert(result: Any) -> None:
        text = (
            render_outbound_daily_limit_unavailable_notice(result.limit)
            if result.redis_unavailable
            else render_outbound_daily_limit_notice(result.count, result.limit)
        )
        await ops_bot.send_message(chat_id=settings.telegram_ops_chat_id, text=text)

    return alert


async def supervised_touch_scheduler(
    store: SqlAlchemyTouchStore,
    kb: Any,
    send: Any,
    *,
    delay_minutes: int,
    max_count: int,
    interval_seconds: int,
    now_fn: Any = lambda: datetime.now(timezone.utc),
    can_send: Any = None,
    last_inbound: Any = None,
    min_inbound_ts: int = 0,
) -> None:
    """Периодический проход воркера отложенных касаний.

    Один сбой прохода (временная недоступность БД/Авито) не должен
    останавливать все последующие — та же логика изоляции, что и у
    `supervised_bot_polling`. `now_fn` — реальное время по умолчанию;
    параметризовано ради теста устойчивости к сбою, который иначе зависел бы
    от того, идёт ли прогон тестов внутри рабочего окна 9:00–23:00.

    `kb` принимает и саму базу знаний, и функцию, которая её отдаёт. Второе
    нужно, потому что рабочее окно правится из Telegram на лету: со
    снимком, взятым один раз при старте, воркер продолжал бы будить
    клиентов по старому расписанию до ближайшего рестарта.
    """
    def _kb() -> KnowledgeBase:
        return kb() if callable(kb) else kb

    while True:
        try:
            current = _kb()
            await run_scheduler_pass(
                store, current.concessions.policy.touch_templates,
                current.catalog.constants.working_window, send, now_fn(),
                delay_minutes=delay_minutes, max_count=max_count,
                can_send=can_send,
                last_inbound=last_inbound, min_inbound_ts=min_inbound_ts,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("touch scheduler: pass failed")
        await asyncio.sleep(interval_seconds)


async def supervised_webhook_resubscribe(
    base_url: str,
    *,
    interval_hours: int,
) -> None:
    """Перепривязка вебхука раз в сутки.

    Переподписка однажды уже оживляла молчащий вебхук — ненадолго, но
    оживляла, — и раз доказательного объяснения молчанию нет, дешевле
    переподписываться по расписанию, чем выяснять причину у поддержки.

    Первая перепривязка идёт СРАЗУ ПОСЛЕ ПАУЗЫ, а не на старте: старт и так
    самое нагруженное место (миграции, база знаний, бот, первый проход
    поллера), и лишний внешний запрос там ничего не даёт — подписка на
    момент деплоя уже актуальна.

    Сбой изолирован так же, как у остальных воркеров: неудачная попытка не
    должна отменять все последующие.
    """
    from app.channels import avito_webhook_admin as webhook_admin

    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            await webhook_admin.subscribe(base_url)
            logger.info(
                "webhook resubscribe: подписка обновлена на %s",
                webhook_admin.webhook_url_for_display(base_url),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webhook resubscribe: не удалось переподписаться")


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


async def supervised_item_scope_refresh(
    items_client: Any,
    store: Any,
    settings: Any,
    *,
    interval_seconds: int,
) -> None:
    """Часовой проход классификации объявлений — см.
    app/channels/item_scope.py:run_item_scope_refresh_pass. Тот же приём
    изоляции сбоя, что и у остальных `supervised_*`: временная недоступность
    Авито/БД не должна останавливать все последующие проходы, а до первого
    успешного прохода `ItemScopeResolver` всё равно классифицирует
    неизвестные item_id синхронно (см. его докстринг)."""
    from app.channels.item_scope import run_item_scope_refresh_pass

    while True:
        try:
            await run_item_scope_refresh_pass(items_client, store, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("item_scope refresh: pass failed")
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Фильтр секретов ставится раньше всего: httpx логирует URL целиком, а у
    # Авито параметры токена идут в query string. См. app/logging_setup.py.
    configure_logging()

    # Load and validate the knowledge base at startup: an invalid KB must
    # stop the process, not surface as a wrong price mid-conversation.
    #
    # Правки из Telegram (app/ops/menu_service.py) накладываются здесь же —
    # они живут в БД (`catalog_overrides`), а не в YAML на диске: файловая
    # система контейнера на Railway эфемерная, запись в неё пропала бы при
    # следующем деплое молча. Сбой самой БД на старте — не повод падать: YAML
    # сам по себе всегда валиден (его тело этот же вызов и проверяет), и
    # деградация в «без правок» безопаснее, чем не подняться вовсе.
    from app.kb.override_store import SqlAlchemyOverrideStore, to_overrides

    override_store = SqlAlchemyOverrideStore(get_sessionmaker())
    try:
        active_overrides = to_overrides(await override_store.list_active())
    except Exception:
        logger.exception("catalog overrides: failed to load, starting with plain YAML")
        active_overrides = []
    app.state.kb = load_catalog(overrides=active_overrides)
    dry_run_gauge.set(1 if settings.dry_run else 0)

    if settings.dry_run:
        logger.warning("DRY_RUN включён — сообщения клиентам в Авито не уходят")

    # Фактический фильтр объявлений — В ЛОГ ПРИ СТАРТЕ. Иначе «применился ли
    # конфиг» приходится выяснять по косвенным признакам: в диалогах видно,
    # что агент отвечал по запрещённому объявлению, а из чего именно
    # состоял список в тот момент — уже нет.
    if settings.avito_allowed_items:
        logger.warning(
            "Фильтр объявлений: БЕЛЫЙ список (%d шт.), он в приоритете — "
            "чёрный список НЕ применяется. Разрешены только: %s",
            len(settings.avito_allowed_items), ", ".join(settings.avito_allowed_items),
        )
    elif settings.avito_blocked_items:
        # Тип каждого элемента печатается намеренно: item_id в API Авито —
        # число, у нас везде строка, и «строка против числа» — первая
        # гипотеза при разборе «фильтр не сработал». Пусть будет видно
        # сразу, а не выясняется ещё одним заходом.
        logger.info(
            "Фильтр объявлений: чёрный список (%d шт.): %s",
            len(settings.avito_blocked_items),
            ", ".join(
                f"{item!r}({type(item).__name__})" for item in settings.avito_blocked_items
            ),
        )
    else:
        logger.warning(
            "Фильтр объявлений ВЫКЛЮЧЕН (AVITO_BLOCKED_ITEMS=none) — агент "
            "отвечает по ЛЮБОМУ объявлению аккаунта, включая вакансии и "
            "продажу бизнеса.",
        )
    logger.info(
        "Чаты без объявления (обращения из профиля, u2u/a2u): %s",
        "агент отвечает" if settings.avito_allow_chats_without_item else "агент молчит",
    )
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
    ops_service = OpsService(
        store=SqlAlchemyOpsStore(get_sessionmaker()), settings=settings, redis=redis_client,
    )
    app.state.ops_service = ops_service

    # `ops_bot` (клиент aiogram) заводится здесь, РАНЬШЕ диспетчера — тот
    # соберётся позже, когда появится menu_service, а конвейеру и воркеру
    # касаний бот нужен уже сейчас, чтобы слать карточки.
    ops_bot = None
    if settings.telegram_bot_token.get_secret_value():
        from aiogram import Bot

        ops_bot = Bot(token=settings.telegram_bot_token.get_secret_value())
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

    # item_scope — классификация объявлений по заголовку вместо зашитого в
    # код списка (app/channels/item_scope.py). Собирается ЗДЕСЬ, безусловно
    # (не только при POLLER_ENABLED=true): им пользуется is_listing_allowed
    # на границе отправки и на входе конвейера, а эти пути живы и с
    # выключенным поллером, через один только вебхук.
    #
    # AvitoItemsClient и OwnItemIds — ОДИН экземпляр на процесс, тот же,
    # которым ниже (если POLLER_ENABLED) пользуется гуард поллера: второй
    # независимый кеш означал бы два часовых обновления списка объявлений
    # аккаунта вместо одного.
    from app.avito.own_items import OwnItemIds
    from app.channels.avito_items import AvitoItemsClient
    from app.channels.item_scope import ItemScopeResolver, SqlAlchemyItemScopeStore

    avito_items_client = AvitoItemsClient(settings=settings)
    own_item_ids = OwnItemIds(avito_items_client, settings)
    item_scope_store = SqlAlchemyItemScopeStore(get_sessionmaker())
    item_scope_resolver = ItemScopeResolver(
        item_scope_store,
        settings,
        own_items_provider=own_item_ids,
        # get_listing — тот же часовой снимок, что и own_items_provider
        # выше, без отдельного запроса на каждый неизвестный item_id (см.
        # докстринг OwnItemIds — живой баг 429 Too Many Requests).
        fetch_item=own_item_ids.get_listing,
    )
    app.state.item_scope_resolver = item_scope_resolver
    item_scope_task = asyncio.create_task(
        supervised_item_scope_refresh(
            avito_items_client, item_scope_store, settings,
            interval_seconds=settings.poller_items_refresh_seconds,
        )
    )
    logger.info(
        "item_scope: часовая классификация объявлений запущена (интервал %d с)",
        settings.poller_items_refresh_seconds,
    )

    # dialog_store создаётся ЗДЕСЬ, а не ниже вместе с конвейером: он нужен
    # гейту исходящих, а гейт — воркеру касаний, который стартует раньше.
    from app.channels import daily_limit, kill_switch
    from app.channels.outbound_gate import OutboundGate
    from app.dialog_store import SqlAlchemyBookingSink, SqlAlchemyDialogStore

    dialog_store = SqlAlchemyDialogStore(get_sessionmaker())

    # /hold и /unhold правят `chats.manual_hold` — ту же колонку, что читает
    # граница исходящих. Ставится ПОСЛЕ создания стора, а не в конструкторе
    # OpsService: операторский контур поднимается раньше конвейера, и ссылка
    # на ещё не созданный dialog_store роняла бы весь старт приложения.
    # Не через операторский стор — hold обязан работать и тогда, когда
    # операторский контур не поднялся вовсе.
    ops_service.manual_hold_setter = dialog_store.set_chat_manual_hold
    # Единственная дверь наружу. Дальше по коду в качестве «клиента Авито»
    # передаётся ИМЕННО гейт, а не avito_client — чтобы новый путь отправки
    # физически не мог обойти белый список объявлений (см. докстринг
    # app/channels/outbound_gate.py: касание уже однажды ушло клиенту,
    # которому агент писать не должен).
    outbound = OutboundGate(
        avito_client,
        settings,
        dialog_store.get_chat_item_id,
        dialog_store.get_chat_manual_hold,
        # Перехват чата человеком. Состояние берётся из операторского стора
        # (тот же флаг, что двигает кнопка «Взять на себя»), а решение по
        # нему принимает сам гейт — по TAKEOVER_MODE, одной функцией на весь
        # проект (app/channels/outbound_gate.py:takeover_blocks).
        takeover_lookup=_takeover_lookup(ops_service),
        # Аварийный рубильник (/stop, /resume) и суточный лимит — та же
        # причина, что у белого списка объявлений строкой выше: должны
        # работать одинаково для всех четырёх путей отправки, а не только
        # там, где их проверили первыми. Оба читают Redis на каждом
        # проходе, а не только переменную окружения при старте — см.
        # докстринги app/channels/kill_switch.py и app/channels/daily_limit.py.
        kill_switch_lookup=lambda: kill_switch.is_stopped(redis_client),
        daily_limit_check=lambda: daily_limit.check_and_increment(
            redis_client, settings.outbound_daily_limit
        ),
        daily_limit_alert=build_daily_limit_alert(settings, ops_bot),
        item_scope_resolver=item_scope_resolver,
    )

    # Четвёртый путь отправки — ответ, одобренный оператором в Telegram
    # (`OpsService.approve`/`send_edited`). ДОЛГО оставался неподключённым:
    # `ops_service` собирается выше без `send_to_avito`, и без этой строки
    # /approve отвечал оператору «Отправлено клиенту», а
    # `self.send_to_avito` было `None` — сообщение не уходило вообще (см.
    # тесты tests/test_ops.py и tests/test_main.py:
    # test_lifespan_wires_operator_approval_through_the_outbound_gate).
    # Присваивание, а не параметр конструктора: `ops_service` нужен воркеру
    # касаний ВЫШЕ по коду, до того как здесь появляется гейт.
    ops_service.send_to_avito = outbound.send_message

    touch_store = SqlAlchemyTouchStore(get_sessionmaker())
    touch_sender = build_touch_sender(settings, ops_service, ops_bot, outbound)
    touch_task = None
    if not settings.touch_enabled:
        # ОБА СОСТОЯНИЯ В СТАРТОВОМ ЛОГЕ — как у AUTO_BOOKING_ENABLED и
        # OUTBOUND_DAILY_LIMIT. Выключенный воркер, о котором лог молчит,
        # неотличим от воркера, который просто не нашёл, кого коснуться.
        logger.warning(
            "TOUCH_ENABLED=false — отложенные касания ВЫКЛЮЧЕНЫ, воркер не "
            "запущен. Это единственный путь, которым агент писал клиенту без "
            "его сообщения; остальные исходящие — только в ответ. Взведённые "
            "таймеры остаются в БД и оживут при обратном включении"
        )
    else:
        touch_task = asyncio.create_task(
            supervised_touch_scheduler(
                # Функция, а не снимок: рабочее окно правится из Telegram на
                # лету (app/ops/menu_service.py), и воркер обязан видеть
                # правку без рестарта — см. докстринг.
                touch_store, lambda: app.state.kb, touch_sender,
                delay_minutes=settings.touch_reminder_delay_minutes,
                max_count=settings.touch_max_count,
                interval_seconds=settings.touch_scheduler_interval_seconds,
                # Отдельно от гейта внутри touch_sender: воркеру нужно не
                # просто не отправить, а погасить таймер — иначе чат остаётся
                # due навсегда. См. run_scheduler_pass.
                can_send=outbound.is_allowed,
                # AGENT_MIN_INBOUND_TS — тот же порог, что у конвейера. Воркер
                # был вторым путём наружу, который его не спрашивал: таймер
                # переживает и смену порога, и месяцы простоя.
                last_inbound=dialog_store.last_incoming_at,
                min_inbound_ts=settings.agent_min_inbound_ts,
            )
        )
        logger.info(
            "touch scheduler: started (TOUCH_ENABLED=true) — агент может "
            "напомнить о себе через %d мин молчания, максимум %d раза",
            settings.touch_reminder_delay_minutes, settings.touch_max_count,
        )

    # Конвейер обработки входящих — последнее звено: вебхук → агент → ответ.
    # Собирается здесь, а не в webhooks.py, потому что ему нужны ВСЕ части
    # выше сразу (провайдер модели, операторский контур, клиент Авито, БД),
    # а вебхук обязан оставаться тонким: принял, дедуплицировал, ответил 200.
    from app.agent.loop import AgentLoop
    from app.agent.providers.factory import build_provider, resolve_models
    from app.pipeline import MessagePipeline

    dialog_model, classifier_model = resolve_models(settings)

    # Предохранитель по расходу. Затравка из БД — обязательна: без неё
    # дневной лимит считался бы «с последнего рестарта», а на Railway
    # контейнер перезапускается и при каждом деплое. Читаем тем же
    # admin_queries, что и /admin/costs, — один источник, одни числа.
    cost_guard = build_cost_guard(settings, ops_bot)
    try:
        already_tripped = cost_guard.seed(await admin_queries.cost_spent_today())
    except Exception:
        # БД недоступна на старте (та же ситуация, что и у остальных
        # supervised_*): поднимаемся с нулевым счётчиком, а не падаем.
        # Предохранитель при этом слабее — скажем об этом вслух.
        logger.exception(
            "cost guard: расход за сегодня не прочитан из БД — счётчик "
            "стартует с нуля, дневной лимит на этих сутках занижен не будет"
        )
        already_tripped = False
    if already_tripped:
        settings.agent_paused = True
        logger.error(
            "DAILY_COST_LIMIT_RUB=%s был исчерпан ещё до старта (потрачено "
            "%s ₽) — агент поднят на паузе, снять только через /resume",
            settings.daily_cost_limit_rub, cost_guard.spent,
        )
    app.state.cost_guard = cost_guard

    # dialog_store уже создан выше (нужен гейту исходящих). Он же идёт и
    # сюда: его count_concessions_today нужен AgentLoop — R10 (дневной
    # лимит уступок) без него никогда не видит реальное число, только 0.
    agent_loop = AgentLoop(
        client=build_provider(settings),
        kb=app.state.kb,
        dialog_model=dialog_model,
        classifier_model=classifier_model,
        booking_provider=booking_provider,
        # Фотографии — из базы знаний, КОЛБЭКОМ: `app.state.kb` заменяется
        # целиком, когда оператор правит каталог из Telegram, и провайдер
        # обязан это видеть. На 2026-08-30 у всех зон `photos: []` — файлы
        # лежат в media/photos/, но scripts/import_photos.py не запускался,
        # так что инструмент по-прежнему честно отвечает «фотографий нет».
        # Проводка нужна, чтобы в день импорта не пришлось её вспоминать.
        photo_provider=KbPhotoProvider(lambda: app.state.kb),
        concessions_today_provider=dialog_store.count_concessions_today,
        booking_sink=SqlAlchemyBookingSink(get_sessionmaker()),
        booking_notifier=build_booking_notifier(settings, ops_bot),
        booking_handoff_notifier=build_booking_handoff_notifier(settings, ops_bot),
        cost_guard=cost_guard,
    )
    pipeline = MessagePipeline(
        store=dialog_store,
        agent_loop=agent_loop,
        ops_service=ops_service,
        settings=settings,
        kb=app.state.kb,
        avito_client=outbound,
        ops_bot=ops_bot,
        # Дедупликация входящих переехала сюда из вебхука: каналов приёма
        # два, и разводить их обязана одна общая точка. См.
        # app/channels/inbound_dedup.py.
        redis=redis_client,
        # Пауза «как живой человек» перед реальной отправкой — не в DRY_RUN,
        # там ответ и так ждёт кнопки оператора (см. MessagePipeline.delay_fn).
        delay_fn=human_delay,
        item_scope_resolver=item_scope_resolver,
    )
    app.state.pipeline = pipeline

    webhooks.configure(handler=pipeline.handle_message)
    logger.info("incoming pipeline: wired to the Avito webhook")

    if settings.agent_min_inbound_ts <= 0:
        # Дефолт 0 = агент не отвечает НИ НА ЧТО, ни поллеру, ни вебхуку —
        # см. app/config.py:agent_min_inbound_ts и
        # app/pipeline.py:_is_too_old_to_answer. Не внутри `if
        # settings.poller_enabled` ниже: вебхук подключён строкой выше
        # безусловно и молчит по той же причине независимо от поллера.
        logger.warning(
            "pipeline: AGENT_MIN_INBOUND_TS не задан (<= 0) — агент НЕ БУДЕТ "
            "отвечать ни на одно сообщение ни на одном канале, пока порог "
            "не поднят осознанно"
        )

    # Автобронирование логируется ОБОИМИ состояниями, а не только
    # выключенным: флаг читается лениво, в момент вызова инструмента
    # (app/agent/tools.py:_tool_create_booking), поэтому по стартовому логу
    # иначе нельзя отличить «выключено осознанно» от «переменную забыли на
    # деплое» — тот же класс ошибки, что уже стоил 65 сообщений. Включённое
    # состояние тем более обязано быть видно при старте: до первой брони
    # оно ничем себя не проявляет.
    #
    # Порядок проверок в логе повторяет порядок в коде: этап оплаты старше
    # рубильника. Пока handoff_on_payment_step=true, AUTO_BOOKING_ENABLED не
    # значит ничего — и сказать об этом при старте важнее, чем повторить
    # значение переменной, иначе включённый рубильник читается как
    # «агент бронирует», а он не бронирует.
    if app.state.kb.payment.payment.handoff_on_payment_step:
        logger.warning(
            "payment.handoff_on_payment_step=true — агент НЕ ставит брони "
            "вообще: на этапе оплаты диалог передаётся оператору с готовой "
            "карточкой, бронь в календаре ставит человек. AUTO_BOOKING_ENABLED"
            "=%s при этом ни на что не влияет",
            settings.auto_booking_enabled,
        )
    elif settings.auto_booking_enabled:
        logger.warning(
            "AUTO_BOOKING_ENABLED=true — агент ставит брони в YCLIENTS САМ, "
            "БЕЗ проверки оплаты: её в коде нет, подтверждение платежа "
            "остаётся целиком на операторе"
        )
    else:
        logger.warning(
            "AUTO_BOOKING_ENABLED=false — автобронирование выключено: агент "
            "только придерживает время и эскалирует оператору. Включать "
            "нельзя, пока в коде не появится проверка оплаты"
        )

    # Суточный лимит — по тому же принципу: состояние читается из лога, а не
    # выводится из наличия переменной. `0` в
    # app/channels/daily_limit.py:check_and_increment означает «лимита нет» и
    # до Redis не доходит вовсе, поэтому выключенный лимит — WARNING, а не
    # info: молча снятый потолок исходящих ничем себя не проявит, пока не
    # уйдёт лишняя тысяча сообщений. Настроенный лимит достаточно показать
    # числом.
    # Дневной лимит расхода — по тому же принципу, что и лимит исходящих:
    # состояние читается из лога, а не выводится из наличия переменной.
    # Ноль здесь означает «потолка на деньги нет вообще» — это WARNING, а не
    # info: незаданный лимит ничем себя не проявит, пока не придёт счёт.
    if settings.daily_cost_limit_rub <= 0:
        logger.warning(
            "DAILY_COST_LIMIT_RUB=%s — дневной лимит расхода на модели "
            "ОТКЛЮЧЁН: агент не остановится ни на какой сумме",
            settings.daily_cost_limit_rub,
        )
    else:
        logger.info(
            "DAILY_COST_LIMIT_RUB=%s ₽ — дневной лимит расхода активен "
            "(потрачено сегодня: %s ₽). При превышении агент уходит на паузу; "
            "счётчик обнуляется в полночь по Москве, пауза снимается только "
            "командой /resume",
            settings.daily_cost_limit_rub, cost_guard.spent,
        )

    if settings.outbound_daily_limit <= 0:
        logger.warning(
            "OUTBOUND_DAILY_LIMIT=%d — суточный лимит исходящих ОТКЛЮЧЁН: "
            "потолка на число сообщений в сутки нет ни на одном из четырёх "
            "путей отправки",
            settings.outbound_daily_limit,
        )
    else:
        logger.info(
            "OUTBOUND_DAILY_LIMIT=%d — суточный лимит исходящих активен, "
            "счётчик сбрасывается в полночь по Москве",
            settings.outbound_daily_limit,
        )

    # ПОСЛЕДНИЕ РУБЕЖИ — В СТАРТОВЫЙ ЛОГ. Они не настраиваются и работают
    # всегда, поэтому строка не про «включено/выключено», а про то, КАКАЯ
    # версия правил сейчас в контейнере. Без неё «рубеж активен» приходилось
    # подтверждать догадкой о том, какой коммит уехал в прод (2026-09-02).
    from app.agent.loop import AVAILABILITY_TOOLS

    logger.info(
        "последние рубежи активны: цена — только после вызова инструмента; "
        "занятость («свободно», «занято», «окошко») — только после %s; "
        "дата в ответе сверяется с блоком «Сейчас:» и не должна ему "
        "противоречить. Нарушение пишется в лог как ERROR «guard rail: ...», "
        "задержанный текст сохраняется в llm_meta",
        " или ".join(sorted(AVAILABILITY_TOOLS)),
    )

    # Поллер — ОСНОВНОЙ канал; вебхук выше остаётся вторым. Причина в том,
    # что вебхуки по объявлениям комплекса не доставляются: за всю историю
    # базы 49 входящих, и все u2i-чаты среди них — по объявлениям из чёрного
    # списка. Подробности и живые числа — в докстринге app/avito/poller.py.
    poller_task: asyncio.Task | None = None
    if settings.poller_enabled:
        from app.avito.cursors import SqlAlchemyCursorStore
        from app.avito.poller import AvitoPoller, supervised_poller

        cursor_store = SqlAlchemyCursorStore(get_sessionmaker())
        app.state.cursor_store = cursor_store
        poller = AvitoPoller(
            # avito_client, а НЕ outbound: поллер только читает, а гейт
            # исходящих защищает отправку. Заворачивать чтение в гейт значило
            # бы ходить в базу за item_id на каждый список чатов без всякой
            # пользы.
            client=avito_client,
            pipeline=pipeline,
            cursors=cursor_store,
            settings=settings,
            redis=redis_client,
            # own_item_ids — тот же экземпляр, что у item_scope_resolver
            # выше: один часовой кеш списка объявлений аккаунта на процесс,
            # а не два независимых.
            items_provider=own_item_ids,
        )
        app.state.poller = poller
        poller_task = asyncio.create_task(
            supervised_poller(poller, interval_seconds=settings.poller_interval_seconds)
        )
        logger.info(
            "poller: запущен, интервал %d с, AGENT_MIN_INBOUND_TS=%d "
            "(предупреждение о пороге — в общем логе старта выше, если он не поднят)",
            settings.poller_interval_seconds, settings.agent_min_inbound_ts,
        )
    else:
        logger.warning(
            "POLLER_ENABLED=false — сообщения приходят ТОЛЬКО вебхуком, "
            "который по объявлениям комплекса не доставляется."
        )

    resubscribe_task: asyncio.Task | None = None
    if settings.public_base_url:
        resubscribe_task = asyncio.create_task(
            supervised_webhook_resubscribe(
                settings.public_base_url,
                interval_hours=settings.webhook_resubscribe_interval_hours,
            )
        )
        logger.info(
            "webhook resubscribe: раз в %d ч",
            settings.webhook_resubscribe_interval_hours,
        )
    else:
        logger.warning(
            "PUBLIC_BASE_URL не задан — суточная перепривязка вебхука не запущена."
        )

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

    # Управление ассистентом из Telegram — меню, правка каталога.
    # `on_kb_reloaded` доводит новый KnowledgeBase до каждого места, что
    # держит собственную ссылку на него: `app.state.kb` (админка читает его
    # заново на каждый запрос — этого достаточно), `agent_loop.kb` (иначе
    # следующий ход агента считал бы по старой цене) и `pipeline.kb`
    # (нужен только для уведомления о дневном лимите, но должен быть
    # согласован с остальными). Воркер касаний правку подхватывает сам —
    # ему передан `lambda: app.state.kb`, а не снимок.
    from app.kb.editor import CatalogEditor
    from app.kb.override_store import SqlAlchemyOverrideStore as _OverrideStore
    from app.ops.menu_service import MenuService

    def _on_kb_reloaded(new_kb) -> None:
        app.state.kb = new_kb
        agent_loop.kb = new_kb
        pipeline.kb = new_kb

    # Тот же колбэк использует /admin/catalog при откате правки — одна
    # функция вместо двух копий одной и той же логики в двух модулях.
    app.state.on_kb_reloaded = _on_kb_reloaded

    catalog_editor = CatalogEditor(_OverrideStore(get_sessionmaker()))
    menu_service = MenuService(
        editor=catalog_editor,
        settings=settings,
        ops_service=ops_service,
        stats_provider=lambda: admin_queries.stats(ops_service.store),
        dialogs_provider=admin_queries.list_dialogs,
        on_kb_reloaded=_on_kb_reloaded,
    )
    app.state.menu_service = menu_service
    # Отдельно от menu_service — /admin/catalog читает журнал и делает откат
    # напрямую, ему незачем тянуть весь Telegram-слой ради этого.
    app.state.catalog_editor = catalog_editor

    bot_task: asyncio.Task | None = None
    if ops_bot is not None:
        dispatcher = build_dispatcher(
            ops_service,
            stats_provider=lambda: admin_queries.stats(ops_service.store),
            menu_service=menu_service,
        )
        bot_task = asyncio.create_task(supervised_bot_polling(dispatcher, ops_bot))
        # Список команд в интерфейсе Telegram — появляется сам, без ручной
        # настройки через @BotFather. Сбой не должен ронять старт (см.
        # set_bot_commands), поэтому отдельным шагом, не внутри try/except
        # остального лайфспана.
        await set_bot_commands(ops_bot)
        logger.info("telegram operator bot: polling started")

    yield

    for task in (poller_task, resubscribe_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    item_scope_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await item_scope_task
    concession_timeout_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await concession_timeout_task
    # None при TOUCH_ENABLED=false — воркер не запускался. Тот же приём, что
    # у bot_task ниже: гасить нечего, но и падать на shutdown нельзя.
    if touch_task is not None:
        touch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await touch_task
    await avito_client.aclose()
    await avito_items_client.aclose()
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
