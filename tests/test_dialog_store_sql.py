"""SqlAlchemyDialogStore на НАСТОЯЩЕМ Postgres.

Зачем отдельно от tests/test_pipeline.py: там конвейер ездит на
`InMemoryDialogStore` — это тестовый дубль, а в проде работает совсем другой
класс. Ровно этот класс расхождения («локально зелено, в контейнере не
работает») уже стоил проекту одного упавшего деплоя, поэтому продовая
реализация проверяется против реальной схемы, а не против своего двойника.

Проверяется то, чего в памяти не воспроизвести в принципе: уникальный индекс
на `avito_message_id` как второй рубеж дедупликации, JSONB для `llm_meta`,
ARRAY для `used_tiers`, Numeric для `floor_reached` (Decimal туда и обратно
без float), ORDER BY ... DESC LIMIT для «хвоста» переписки.

Без доступной базы модуль целиком пропускается — как и tests/test_migrations.py.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.listing_context import ItemZoneRow
from app.agent.touch_tracking import TouchState
from app.config import get_settings
from app.db.models import Author, SendStatus
from app.dialog_store import SqlAlchemyDialogStore
from app.pricing.concessions import DialogConcessionState

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://parmangal_test:parmangal_test@127.0.0.1:5432/parmangal_test",
)

# Порядок важен: messages/dialog_states ссылаются на chats по внешнему ключу.
TABLES_TO_CLEAN = ("messages", "dialog_states", "item_zone_map", "chats")


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
    reason=(
        "Нужен реальный Postgres на TEST_DATABASE_URL (по умолчанию — "
        "parmangal_test@127.0.0.1:5432): схема использует JSONB/ARRAY/"
        "именованные ENUM, которых SQLite не понимает."
    ),
)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    """Схема поднимается миграцией, а не `Base.metadata.create_all` —
    тест обязан ездить по той же схеме, что реально будет в проде.
    Заодно это ловит расхождение моделей с миграцией."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    yield
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
    get_settings.cache_clear()


@pytest.fixture
async def store():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        for table in TABLES_TO_CLEAN:
            await conn.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    yield SqlAlchemyDialogStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------

async def test_get_or_create_chat_creates_then_finds_the_same_row(store):
    created = await store.get_or_create_chat("chat-1", item_id="item-1")
    assert created.chat_id == "chat-1"
    assert created.item_id == "item-1"

    again = await store.get_or_create_chat("chat-1")
    assert again.item_id == "item-1"     # не затёрлось отсутствием item_id


async def test_item_id_is_filled_in_later_but_never_erased(store):
    await store.get_or_create_chat("chat-1")           # первый вебхук без item_id
    filled = await store.get_or_create_chat("chat-1", item_id="item-9")
    assert filled.item_id == "item-9"


async def test_zone_is_resolved_from_the_item_zone_map(store):
    from app.db.models import ItemZoneMap

    async with store._session_factory() as session:
        session.add(ItemZoneMap(item_id="item-1", zone_id="bath_russian"))
        await session.commit()

    chat = await store.get_or_create_chat("chat-1", item_id="item-1")
    assert chat.zone_id == "bath_russian"


async def test_takeover_flag_survives_a_reread(store):
    from sqlalchemy import select

    from app.db.models import Chat

    await store.get_or_create_chat("chat-1")
    async with store._session_factory() as session:
        chat = (await session.execute(select(Chat).where(Chat.chat_id == "chat-1"))).scalar_one()
        chat.is_human_takeover = True
        await session.commit()

    assert (await store.get_or_create_chat("chat-1")).is_human_takeover is True


# --------------------------------------------------------------------------
# Дедупликация через уникальный индекс
# --------------------------------------------------------------------------

async def test_duplicate_avito_message_id_is_rejected_by_the_unique_index(store):
    """Второй рубеж после Redis: сброс кеша не должен приводить к удвоенным
    сообщениям в переписке."""
    await store.get_or_create_chat("chat-1")

    assert await store.save_incoming("chat-1", "Привет", avito_message_id="m-1") is True
    assert await store.save_incoming("chat-1", "Привет", avito_message_id="m-1") is False

    history = await store.load_history("chat-1")
    assert history == [{"role": "user", "content": "Привет"}]


async def test_messages_without_an_id_are_not_deduplicated_against_each_other(store):
    """avito_message_id = NULL: в Postgres NULL не равен NULL, поэтому
    уникальный индекс их не схлопывает — и это правильно, иначе второе
    сообщение без id молча потерялось бы."""
    await store.get_or_create_chat("chat-1")

    assert await store.save_incoming("chat-1", "первое", avito_message_id=None) is True
    assert await store.save_incoming("chat-1", "второе", avito_message_id=None) is True

    assert len(await store.load_history("chat-1")) == 2


# --------------------------------------------------------------------------
# История
# --------------------------------------------------------------------------

async def test_history_keeps_order_and_maps_roles(store):
    await store.get_or_create_chat("chat-1")
    await store.save_incoming("chat-1", "Здравствуйте", avito_message_id="m-1")
    await store.save_outgoing("chat-1", "Добрый день!", SendStatus.sent)
    await store.save_incoming("chat-1", "Сколько стоит?", avito_message_id="m-2")

    assert await store.load_history("chat-1") == [
        {"role": "user", "content": "Здравствуйте"},
        {"role": "assistant", "content": "Добрый день!"},
        {"role": "user", "content": "Сколько стоит?"},
    ]


async def test_history_returns_the_tail_not_the_head(store):
    """ORDER BY ... DESC LIMIT N + разворот: в контекст должны попасть
    ПОСЛЕДНИЕ сообщения, а не первые."""
    await store.get_or_create_chat("chat-1")
    for i in range(40):
        await store.save_incoming("chat-1", f"сообщение {i}", avito_message_id=f"m-{i}")

    history = await store.load_history("chat-1", limit=5)
    assert [m["content"] for m in history] == [
        "сообщение 35", "сообщение 36", "сообщение 37", "сообщение 38", "сообщение 39",
    ]


async def test_rejected_and_failed_replies_stay_out_of_the_history(store):
    await store.get_or_create_chat("chat-1")
    await store.save_outgoing("chat-1", "отклонён оператором", SendStatus.rejected)
    await store.save_outgoing("chat-1", "не ушёл в Авито", SendStatus.failed)
    await store.save_outgoing("chat-1", "ушёл клиенту", SendStatus.sent)

    assert await store.load_history("chat-1") == [
        {"role": "assistant", "content": "ушёл клиенту"}
    ]


async def test_history_is_per_chat(store):
    await store.get_or_create_chat("chat-1")
    await store.get_or_create_chat("chat-2")
    await store.save_incoming("chat-1", "первый чат", avito_message_id="m-1")
    await store.save_incoming("chat-2", "второй чат", avito_message_id="m-2")

    assert await store.load_history("chat-1") == [{"role": "user", "content": "первый чат"}]


# --------------------------------------------------------------------------
# llm_meta и состояние диалога
# --------------------------------------------------------------------------

async def test_llm_meta_survives_the_jsonb_round_trip(store):
    from sqlalchemy import select

    from app.db.models import Message

    await store.get_or_create_chat("chat-1")
    meta = {
        "provider": "anthropic", "model": "claude-sonnet-5",
        "input_tokens": 1200, "output_tokens": 80, "cost_rub": "1.23",
    }
    await store.save_outgoing("chat-1", "Ответ", SendStatus.dry_run, llm_meta=meta)

    async with store._session_factory() as session:
        row = (await session.execute(select(Message).where(Message.chat_id == "chat-1"))).scalar_one()

    assert row.llm_meta == meta
    assert row.author == Author.agent
    assert row.status == SendStatus.dry_run


async def test_ruble_sign_survives_a_round_trip(store):
    """Знак ₽ (U+20BD) есть почти в каждом реальном ответе агента, но его
    НЕТ в WIN1251. База, созданная с этой кодировкой (а на русской Windows
    это значение по умолчанию), принимает кириллицу и падает на ₽ —
    UntranslatableCharacterError при вставке каждого сообщения с ценой.
    Поймано ручным прогоном через настоящий вебхук: тесты были зелёными,
    потому что в них не было ни одного ₽. Railway отдаёт UTF8, так что это
    про локальные и самостоятельно поднятые базы.
    """
    from sqlalchemy import select

    from app.db.models import Message

    await store.get_or_create_chat("chat-1")
    text_with_ruble = "Баня в субботу — 10 500 ₽ за 3 часа."
    await store.save_outgoing("chat-1", text_with_ruble, SendStatus.sent)

    async with store._session_factory() as session:
        row = (await session.execute(select(Message).where(Message.chat_id == "chat-1"))).scalar_one()
    assert row.text == text_with_ruble
    assert await store.load_history("chat-1") == [
        {"role": "assistant", "content": text_with_ruble}
    ]


async def test_dialog_state_round_trip_keeps_decimal_and_tiers(store):
    """floor_reached — Numeric, а не float: весь ценовой стек в проекте на
    Decimal, и потеря точности здесь означала бы неверную цену клиенту."""
    await store.get_or_create_chat("chat-1")
    concession = DialogConcessionState(
        base_price_quoted=True,
        used_tiers=frozenset({1, 3}),
        floor_reached=Decimal("5000.50"),
        touch_count=2,
    )
    touch = TouchState(touch_count=2)

    await store.save_dialog_state("chat-1", concession, touch)
    loaded_concession, loaded_touch = await store.load_dialog_state("chat-1")

    assert loaded_concession.floor_reached == Decimal("5000.50")
    assert isinstance(loaded_concession.floor_reached, Decimal)
    assert loaded_concession.used_tiers == frozenset({1, 3})
    assert loaded_concession.base_price_quoted is True
    assert loaded_touch.touch_count == 2


async def test_saving_dialog_state_twice_updates_one_row(store):
    from sqlalchemy import func, select

    from app.db.models import DialogState

    await store.get_or_create_chat("chat-1")
    await store.save_dialog_state("chat-1", DialogConcessionState(), TouchState())
    await store.save_dialog_state(
        "chat-1", DialogConcessionState(floor_reached=Decimal("4000")), TouchState(touch_count=1)
    )

    async with store._session_factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(DialogState).where(DialogState.chat_id == "chat-1")
            )
        ).scalar_one()
    assert count == 1

    concession, touch = await store.load_dialog_state("chat-1")
    assert concession.floor_reached == Decimal("4000")
    assert touch.touch_count == 1


async def test_missing_dialog_state_degrades_to_empty_not_an_error(store):
    concession, touch = await store.load_dialog_state("chat-never-seen")
    assert concession == DialogConcessionState()
    assert touch == TouchState()


async def test_touch_state_written_here_is_visible_to_the_scheduler_store(store):
    """Конвейер и воркер касаний пишут в ОДНИ И ТЕ ЖЕ колонки — если они
    разойдутся, сброс таймера в конвейере не будет виден воркеру, и второе
    касание всё равно уйдёт ответившему клиенту."""
    from datetime import datetime, timedelta, timezone

    from app.ops.touch_scheduler import SqlAlchemyTouchStore

    now = datetime.now(timezone.utc)
    await store.get_or_create_chat("chat-1")
    await store.save_dialog_state(
        "chat-1",
        DialogConcessionState(base_price_quoted=True, touch_count=1),
        TouchState(touch_count=1, last_touch_at=now - timedelta(hours=1),
                   next_touch_due_at=now - timedelta(minutes=1)),
    )

    scheduler_store = SqlAlchemyTouchStore(store._session_factory)
    due = await scheduler_store.list_due(now, max_count=3)
    assert [d.chat_id for d in due] == ["chat-1"]

    # Клиент ответил — конвейер сбрасывает таймер.
    concession, touch = await store.load_dialog_state("chat-1")
    from app.agent.touch_tracking import reset_timer_on_reply
    await store.save_dialog_state("chat-1", concession, reset_timer_on_reply(touch))

    assert await scheduler_store.list_due(now, max_count=3) == []


async def test_agent_reply_count_increments(store):
    await store.get_or_create_chat("chat-1")
    await store.bump_agent_reply_count("chat-1")
    await store.bump_agent_reply_count("chat-1")

    assert (await store.get_or_create_chat("chat-1")).agent_reply_count == 2


# --------------------------------------------------------------------------
# ItemZoneLookup
# --------------------------------------------------------------------------

async def test_item_zone_lookup_returns_row_or_none(store):
    from app.db.models import ItemZoneMap

    async with store._session_factory() as session:
        session.add(ItemZoneMap(item_id="item-1", zone_id="tent"))
        session.add(ItemZoneMap(item_id="item-2", category="bath"))
        await session.commit()

    assert await store.get("item-1") == ItemZoneRow(zone_id="tent", category=None)
    assert await store.get("item-2") == ItemZoneRow(zone_id=None, category="bath")
    assert await store.get("item-unknown") is None
