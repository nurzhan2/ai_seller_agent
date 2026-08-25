"""SQL-реализации хранилищ — на реальном Postgres, не на моках.

Зачем отдельно от tests/test_pipeline.py и tests/test_ops.py. Те гоняют
`InMemory*`-двойники: это правильно для проверки ПРАВИЛ (кто кого зовёт, в
каком порядке, что происходит при перехвате), но ничего не говорит о том,
работает ли то, что реально крутится в проде. Ровно на этом мы уже
обожглись дважды: `/admin/dialogs` месяцами показывал «источник не
подключён», потому что реализации провайдера не существовало вовсе, а
тесты были зелёные на фейках; и `prometheus_client` уехал в прод без
requirements.txt, потому что локально стоял в окружении.

Здесь поэтому — настоящая база, настоящая схема (накатанная alembic'ом,
той же миграцией, что и в проде) и настоящий SQL. Без доступного Postgres
модуль пропускается целиком, а не падает: адрес — TEST_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin.queries import SqlAlchemyAdminQueries
from app.agent.listing_context import ItemZoneRow
from app.agent.touch_tracking import TouchState
from app.config import get_settings
from app.db.models import (
    Author,
    Chat,
    ChatState,
    ConcessionLog,
    Direction,
    ItemZoneMap,
    Lead,
    Message,
    SendStatus,
)
from app.dialog_store import SqlAlchemyDialogStore
from app.ops.state import ChatFlags, PendingReply, SqlAlchemyOpsStore
from app.pricing.concessions import DialogConcessionState

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://parmangal_test:parmangal_test@127.0.0.1:5432/parmangal_test",
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

# Порядок не важен: чистим одним TRUNCATE ... CASCADE.
_TABLES = (
    "messages", "dialog_states", "pending_replies", "operator_actions",
    "concession_log", "leads", "item_zone_map", "zone_service_map", "chats",
)


def _database_reachable(url: str) -> bool:
    async def _ping() -> bool:
        try:
            engine = create_async_engine(url)
            async with engine.connect():
                pass
            await engine.dispose()
            return True
        except Exception:
            return False

    return asyncio.run(_ping())


pytestmark = pytest.mark.skipif(
    not _database_reachable(TEST_DATABASE_URL),
    reason="Нужен реальный Postgres на TEST_DATABASE_URL (по умолчанию parmangal_test@127.0.0.1:5432)",
)


@pytest.fixture(scope="module")
def _schema():
    """Накатывает схему той же миграцией, что поедет в прод, а не create_all.

    СИНХРОННАЯ и модульная намеренно. Синхронная — потому что
    `command.upgrade` под капотом зовёт `asyncio.run()` (см.
    migrations/env.py), а его нельзя вызвать изнутри уже работающего цикла
    событий: из async-фикстуры это падает «cannot be called when another
    asyncio event loop is running». Модульная — потому что даже no-op
    upgrade стоит порядка секунды, и на три десятка тестов это минута
    впустую; схема между тестами не меняется, чистится только содержимое.

    `command.upgrade` идемпотентен, поэтому фикстура не зависит от того, в
    каком порядке pytest запустил файлы и что оставил после себя
    tests/test_migrations.py (он в teardown уходит в base).
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", TEST_DATABASE_URL)
        get_settings.cache_clear()
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(cfg, "head")
        yield
    get_settings.cache_clear()


@pytest.fixture
async def session_factory(_schema):
    """Чистая база на каждый тест — TRUNCATE, а не пересоздание схемы."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))

    yield async_sessionmaker(engine, expire_on_commit=False)

    await engine.dispose()


@pytest.fixture
async def seeded(session_factory):
    """Один диалог с перепиской, лид, уступка и маппинг объявления."""
    async with session_factory() as session:
        session.add(Chat(
            chat_id="c-1", item_id="item-1", zone_id="bath_russian",
            buyer_name="Иван", state=ChatState.quoted, last_msg_at=NOW,
        ))
        session.add(ItemZoneMap(item_id="item-1", zone_id="bath_russian"))
        await session.commit()

        session.add(Message(
            chat_id="c-1", direction=Direction.incoming, author=Author.client,
            text="Здравствуйте", avito_message_id="m-1", status=SendStatus.sent,
        ))
        session.add(Message(
            chat_id="c-1", direction=Direction.outgoing, author=Author.agent,
            text="Добрый день!", status=SendStatus.sent,
            llm_meta={"provider": "anthropic", "model": "claude-sonnet-5",
                      "input_tokens": 1000, "output_tokens": 50, "cost_rub": "2.50"},
        ))
        session.add(Lead(chat_id="c-1", name="Иван", phone="+79990000000", zone_id="bath_russian", guests=6))
        session.add(ConcessionLog(
            dialog_id="c-1", zone="bath_russian", tier=1, trigger="price_objection",
            base_price=Decimal("7000"), final_price=Decimal("6000"),
            revenue_delta=Decimal("1000"), revenue_delta_basis="base_price",
            allowed=True, provisional_policy=True,
        ))
        await session.commit()
    return session_factory


# ==========================================================================
# SqlAlchemyDialogStore — то, чем пишет конвейер
# ==========================================================================

async def test_dialog_store_creates_a_chat_and_resolves_the_zone(seeded):
    store = SqlAlchemyDialogStore(seeded)

    chat = await store.get_or_create_chat("c-new", item_id="item-1")

    assert chat.chat_id == "c-new"
    assert chat.item_id == "item-1"
    assert chat.zone_id == "bath_russian"   # подтянулась из item_zone_map


async def test_dialog_store_does_not_wipe_item_id_on_a_later_webhook(seeded):
    """Вебхук без item_id не должен стирать связь с объявлением."""
    store = SqlAlchemyDialogStore(seeded)

    again = await store.get_or_create_chat("c-1", item_id=None)

    assert again.item_id == "item-1"
    assert again.zone_id == "bath_russian"


async def test_dialog_store_duplicate_message_id_is_rejected_by_the_unique_index(seeded):
    """Второй рубеж дедупликации после Redis — именно уникальный индекс,
    а не SELECT перед INSERT."""
    store = SqlAlchemyDialogStore(seeded)

    first = await store.save_incoming("c-1", "текст", avito_message_id="m-999")
    second = await store.save_incoming("c-1", "текст", avito_message_id="m-999")

    assert first is True
    assert second is False


async def test_dialog_store_history_is_chronological_and_role_tagged(seeded):
    store = SqlAlchemyDialogStore(seeded)

    history = await store.load_history("c-1")

    assert history == [
        {"role": "user", "content": "Здравствуйте"},
        {"role": "assistant", "content": "Добрый день!"},
    ]


async def test_dialog_store_history_keeps_the_last_n_in_order(session_factory):
    """ORDER BY DESC + LIMIT + разворот: хвост диалога, а не его начало."""
    store = SqlAlchemyDialogStore(session_factory)
    await store.get_or_create_chat("c-long")
    for i in range(40):
        await store.save_incoming("c-long", f"сообщение {i}", avito_message_id=f"m-{i}")

    history = await store.load_history("c-long", limit=5)

    assert [h["content"] for h in history] == [
        "сообщение 35", "сообщение 36", "сообщение 37", "сообщение 38", "сообщение 39",
    ]


async def test_dialog_store_rejected_messages_stay_out_of_history(seeded):
    store = SqlAlchemyDialogStore(seeded)
    await store.save_outgoing("c-1", "Отклонённый текст", SendStatus.rejected)

    contents = [h["content"] for h in await store.load_history("c-1")]

    assert "Отклонённый текст" not in contents


async def test_dialog_store_round_trips_the_ratchet_through_the_database(seeded):
    """Храповик обязан переживать рестарт — иначе агент назовёт цену выше
    уже обещанной. Проверяем через ДВА разных экземпляра стора."""
    writer = SqlAlchemyDialogStore(seeded)
    await writer.save_dialog_state(
        "c-1",
        DialogConcessionState(
            base_price_quoted=True, used_tiers=frozenset({1, 2}),
            floor_reached=Decimal("6000.00"), touch_count=1,
        ),
        TouchState(touch_count=1, last_touch_at=NOW, next_touch_due_at=NOW + timedelta(minutes=30)),
    )

    reader = SqlAlchemyDialogStore(seeded)      # «другой процесс»
    concession, touch = await reader.load_dialog_state("c-1")

    assert concession.floor_reached == Decimal("6000.00")
    assert concession.used_tiers == frozenset({1, 2})
    assert concession.base_price_quoted is True
    assert touch.touch_count == 1
    assert touch.next_touch_due_at == NOW + timedelta(minutes=30)


async def test_dialog_store_saves_llm_meta_as_jsonb(seeded):
    store = SqlAlchemyDialogStore(seeded)
    meta = {"provider": "deepseek", "model": "deepseek-chat", "cost_rub": "0.40"}

    await store.save_outgoing("c-1", "Ответ", SendStatus.dry_run, llm_meta=meta)

    async with seeded() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(Message).where(Message.text == "Ответ")
        )).scalar_one()
        assert row.llm_meta == meta
        assert row.status == SendStatus.dry_run


async def test_dialog_store_item_lookup_reads_the_mapping(seeded):
    store = SqlAlchemyDialogStore(seeded)

    assert await store.get("item-1") == ItemZoneRow(zone_id="bath_russian", category=None)
    assert await store.get("item-нет") is None


async def test_dialog_store_bumps_the_reply_counter(seeded):
    store = SqlAlchemyDialogStore(seeded)

    await store.bump_agent_reply_count("c-1")
    await store.bump_agent_reply_count("c-1")

    chat = await store.get_or_create_chat("c-1")
    assert chat.agent_reply_count == 2


# ==========================================================================
# SqlAlchemyOpsStore — состояние модерации переживает рестарт
# ==========================================================================

async def test_ops_store_flags_survive_a_new_instance(seeded):
    writer = SqlAlchemyOpsStore(seeded)
    await writer.set_flags("c-1", ChatFlags(is_human_takeover=True, takeover_at=NOW))

    reader = SqlAlchemyOpsStore(seeded)      # «после рестарта контейнера»
    flags = await reader.get_flags("c-1")

    assert flags.is_human_takeover is True
    assert flags.takeover_at == NOW


async def test_ops_store_unknown_chat_returns_defaults_without_creating_a_row(session_factory):
    """Чтение не должно писать: строку заводит конвейер на первом сообщении."""
    store = SqlAlchemyOpsStore(session_factory)

    flags = await store.get_flags("никогда-не-было")

    assert flags == ChatFlags()
    async with session_factory() as session:
        from sqlalchemy import func, select
        count = (await session.execute(select(func.count()).select_from(Chat))).scalar()
        assert count == 0


async def test_ops_store_takeover_works_for_a_chat_the_pipeline_has_not_seen(session_factory):
    """Оператор может забрать чат по карточке от воркера касаний раньше,
    чем конвейер завёл строку — set_flags обязан её создать."""
    store = SqlAlchemyOpsStore(session_factory)

    await store.set_flags("c-ранний", ChatFlags(is_human_takeover=True))

    assert (await store.get_flags("c-ранний")).is_human_takeover is True


async def test_ops_store_pending_reply_survives_a_new_instance(seeded):
    """Главное, ради чего OpsStore переехал в БД: неодобренный ответ не
    должен исчезать при редеплое, пока клиент ждёт."""
    writer = SqlAlchemyOpsStore(seeded)
    await writer.set_pending("c-1", PendingReply(chat_id="c-1", text="Ответ на модерации", created_at=NOW))

    reader = SqlAlchemyOpsStore(seeded)
    pending = await reader.get_pending("c-1")

    assert pending is not None
    assert pending.text == "Ответ на модерации"
    assert pending.status == "pending"


async def test_ops_store_new_pending_replaces_the_old_one(seeded):
    """Одна ожидающая реплика на чат — как было у словаря."""
    store = SqlAlchemyOpsStore(seeded)
    await store.set_pending("c-1", PendingReply(chat_id="c-1", text="первый", created_at=NOW))
    await store.set_pending("c-1", PendingReply(chat_id="c-1", text="второй", created_at=NOW))

    assert (await store.get_pending("c-1")).text == "второй"


async def test_ops_store_clearing_pending_removes_it(seeded):
    store = SqlAlchemyOpsStore(seeded)
    await store.set_pending("c-1", PendingReply(chat_id="c-1", text="текст", created_at=NOW))

    await store.set_pending("c-1", None)

    assert await store.get_pending("c-1") is None


async def test_ops_store_logs_actions_with_the_operator_id(seeded):
    from sqlalchemy import select

    from app.db.models import OperatorAction

    store = SqlAlchemyOpsStore(seeded)
    await store.log_action("c-1", 777, "approve", {"text": "ответ"})

    async with seeded() as session:
        row = (await session.execute(select(OperatorAction))).scalar_one()
        assert row.telegram_user_id == 777
        assert row.action == "approve"
        assert row.payload == {"text": "ответ"}


async def test_ops_store_moderation_stats_come_from_the_action_log(seeded):
    """Счётчики не хранятся отдельным полем — они восстанавливаются из
    журнала, поэтому не могут разойтись с ним."""
    store = SqlAlchemyOpsStore(seeded)
    await store.log_action("c-1", 1, "approve", {})
    await store.log_action("c-1", 1, "approve", {})
    await store.log_action("c-1", 1, "reject", {})
    await store.log_action("c-1", 1, "send_edited", {})
    await store.log_action("c-1", 1, "takeover", {})       # не метрика модерации

    assert await store.moderation_stats() == {"approved": 2, "rejected": 1, "edited": 1}


async def test_ops_service_end_to_end_over_the_sql_store(seeded):
    """Кнопки оператора поверх БД: одобрение отправляет ровно один раз, а
    повторный клик (Telegram переотправляет callback) — уже нет."""
    from app.config import Settings
    from app.ops.bot import OpsService

    sent: list[tuple[str, str]] = []

    async def _send(chat_id: str, text_: str):
        sent.append((chat_id, text_))

    service = OpsService(
        store=SqlAlchemyOpsStore(seeded),
        settings=Settings(telegram_allowed_users=[1], dry_run=True),
        send_to_avito=_send,
    )
    await service.queue_reply("c-1", "Готовый ответ")

    first = await service.approve("c-1", user_id=1)
    second = await service.approve("c-1", user_id=1)

    assert first["sent"] is True
    assert second["sent"] is False           # идемпотентность
    assert sent == [("c-1", "Готовый ответ")]
    assert await service.store.moderation_stats() == {"approved": 1, "rejected": 0, "edited": 0}


# ==========================================================================
# SqlAlchemyAdminQueries — то, что читает админка
# ==========================================================================

async def test_admin_dialogs_shows_the_chat_the_pipeline_wrote(seeded):
    """Ровно тот баг, ради которого всё это: конвейер пишет Chat/Message,
    а /admin/dialogs обязан их увидеть."""
    queries = SqlAlchemyAdminQueries(seeded)

    rows = await queries.list_dialogs()

    assert len(rows) == 1
    assert rows[0]["chat_id"] == "c-1"
    assert rows[0]["zone_id"] == "bath_russian"
    assert rows[0]["messages"] == 2
    assert rows[0]["is_human_takeover"] is False


async def test_admin_dialogs_counts_messages_per_chat_not_globally(session_factory):
    """Подзапрос с GROUP BY легко написать так, что счётчик у всех чатов
    одинаковый — проверяем на двух диалогах с разным числом сообщений."""
    store = SqlAlchemyDialogStore(session_factory)
    await store.get_or_create_chat("c-a")
    await store.get_or_create_chat("c-b")
    await store.save_incoming("c-a", "раз", avito_message_id="a1")
    await store.save_incoming("c-b", "раз", avito_message_id="b1")
    await store.save_incoming("c-b", "два", avito_message_id="b2")
    await store.save_incoming("c-b", "три", avito_message_id="b3")

    rows = {r["chat_id"]: r["messages"] for r in await SqlAlchemyAdminQueries(session_factory).list_dialogs()}

    assert rows == {"c-a": 1, "c-b": 3}


async def test_admin_dialogs_includes_a_chat_with_no_messages_yet(session_factory):
    """outerjoin, а не join: только что заведённый чат не должен пропадать
    со страницы из-за того, что сообщений к нему ещё нет."""
    await SqlAlchemyDialogStore(session_factory).get_or_create_chat("c-пустой")

    rows = await SqlAlchemyAdminQueries(session_factory).list_dialogs()

    assert [r["chat_id"] for r in rows] == ["c-пустой"]
    assert rows[0]["messages"] == 0


async def test_admin_leads_returns_the_lead(seeded):
    rows = await SqlAlchemyAdminQueries(seeded).list_leads()

    assert len(rows) == 1
    assert rows[0]["phone"] == "+79990000000"
    assert rows[0]["zone_id"] == "bath_russian"


async def test_admin_concessions_lists_only_granted_ones(seeded):
    """Отказы пишутся в тот же журнал по правилу R12 — но страница считает
    недополученную выручку, и отказы её исказили бы."""
    async with seeded() as session:
        session.add(ConcessionLog(
            dialog_id="c-1", tier=2, allowed=False,
            denial_reason="ступень уже использована", revenue_delta=Decimal("5000"),
        ))
        await session.commit()

    rows = await SqlAlchemyAdminQueries(seeded).list_concessions()

    assert len(rows) == 1
    assert rows[0]["tier"] == 1
    assert rows[0]["revenue_delta"] == Decimal("1000")


async def test_admin_costs_aggregates_llm_meta(seeded):
    rows = await SqlAlchemyAdminQueries(seeded).list_costs()

    assert len(rows) == 1
    assert rows[0]["llm_provider"] == "anthropic"
    assert rows[0]["model"] == "claude-sonnet-5"
    assert rows[0]["cost_rub"] == Decimal("2.50")
    assert rows[0]["dialogs"] == 1


async def test_admin_costs_survives_broken_cost_values(seeded):
    """Битое значение внутри JSONB — это ноль в сумме, а не упавшая
    страница расходов. Именно поэтому агрегация в Python, а не приведение
    типа в SQL, которое роняет весь запрос на первой же плохой строке."""
    store = SqlAlchemyDialogStore(seeded)
    await store.save_outgoing("c-1", "битая", SendStatus.sent,
                              llm_meta={"provider": "anthropic", "model": "claude-sonnet-5",
                                        "cost_rub": "не число"})

    rows = await SqlAlchemyAdminQueries(seeded).list_costs()

    assert rows[0]["cost_rub"] == Decimal("2.50")   # битая строка добавила ноль


async def test_admin_costs_ignores_messages_without_meta(seeded):
    """Входящие от клиента и ответы оператора не стоят ничего."""
    store = SqlAlchemyDialogStore(seeded)
    await store.save_outgoing("c-1", "ответ оператора", SendStatus.sent, author=Author.operator)

    rows = await SqlAlchemyAdminQueries(seeded).list_costs()

    assert sum(r["dialogs"] for r in rows) == 1


async def test_admin_stats_combines_db_counts_with_moderation_log(seeded):
    ops = SqlAlchemyOpsStore(seeded)
    await ops.log_action("c-1", 1, "approve", {})

    data = await SqlAlchemyAdminQueries(seeded).stats(ops)

    assert data["dialogs"] == 1
    assert data["leads"] == 1
    assert data["cost_rub"] == Decimal("2.50")
    assert data["approved"] == 1
    # render_stats принимает ровно этот набор ключей — иначе /stats упадёт
    # TypeError прямо в руках оператора.
    from app.ops.notifications import render_stats
    assert isinstance(render_stats(**data), str)
