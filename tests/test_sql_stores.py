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
from app.dialog_store import MOSCOW_TZ, SqlAlchemyDialogStore
from app.ops.state import ChatFlags, PendingReply, SqlAlchemyOpsStore
from app.pricing.concessions import ConcessionDecision, ConcessionEvent, DialogConcessionState
from app.pricing.engine import PriceQuote

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
    "catalog_overrides",
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


def _price_concession_event(base=Decimal("7000"), final=Decimal("6000")):
    decision = ConcessionDecision(
        allowed=True, tier=5, kind="price",
        new_quote=PriceQuote(status="ok", total=final, zone_id="bath_russian"),
        revenue_delta=final - base, revenue_delta_basis="base_rate",
        offer_template="Скидка",
    )
    return ConcessionEvent(decision=decision, base_price=base, zone_id="bath_russian", trigger="price_objection")


async def test_dialog_store_log_concession_writes_a_row(session_factory):
    """`session_factory`, а не `seeded` — та фикстура уже сеет одну строку
    ConcessionLog сама, и тест проверял бы не то, что написал он сам."""
    store = SqlAlchemyDialogStore(session_factory)

    await store.log_concession("c-1", _price_concession_event())

    async with session_factory() as session:
        from sqlalchemy import select
        row = (await session.execute(select(ConcessionLog))).scalar_one()
        assert row.dialog_id == "c-1"
        assert row.tier == 5
        assert row.base_price == Decimal("7000")
        assert row.final_price == Decimal("6000")
        assert row.revenue_delta == Decimal("-1000")
        assert row.allowed is True


async def test_dialog_store_count_concessions_today_counts_only_allowed(session_factory):
    store = SqlAlchemyDialogStore(session_factory)
    denied = ConcessionEvent(
        decision=ConcessionDecision(allowed=False, tier=5, kind="price", requires_operator_approval=True),
        base_price=Decimal("7000"), zone_id="bath_russian", trigger="price_objection",
    )

    assert await store.count_concessions_today() == 0
    await store.log_concession("c-1", _price_concession_event())
    await store.log_concession("c-1", denied)

    assert await store.count_concessions_today() == 1


async def test_dialog_store_count_concessions_today_ignores_yesterday(session_factory):
    store = SqlAlchemyDialogStore(session_factory)
    await store.log_concession("c-1", _price_concession_event())

    async with session_factory() as session:
        from sqlalchemy import update
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        await session.execute(update(ConcessionLog).values(created_at=yesterday))
        await session.commit()

    assert await store.count_concessions_today() == 0


async def test_dialog_store_day_boundary_is_moscow_not_utc(session_factory):
    """Детерминированная проверка границы дня — НЕ зависит от того, в какой
    час UTC реально идёт прогон.

    `now` зафиксирован на 2026-08-25 01:30 МСК (= 2026-08-24 22:30 UTC) —
    попадает ровно в окно, где UTC- и MSK-полночь расходятся: MSK уже
    перевалила на 25-е, UTC ещё на 24-м. Дальше два события по разные
    стороны ИМЕННО MSK-полночи:
      * `c-yesterday` в 23:59 МСК 24-го (= 20:59 UTC 24-го) — MSK-вчера;
      * `c-today` в 00:30 МСК 25-го (= 21:30 UTC 24-го) — MSK-сегодня.
    Обе метки лежат ПОСЛЕ ошибочной UTC-полночи (00:00 UTC 24-го), поэтому
    UTC-граница засчитала бы обе как «сегодня» (count=2) — только
    MSK-граница отличает их (count=1). Без параметра `now` этот тест ловил
    бы регрессию UTC↔MSK только по случайности, в зависимости от времени
    суток прогона — предыдущая версия теста (относительным `datetime.now()
    - timedelta(days=1))` именно так и не поймала внесённую мутацию.
    """
    from sqlalchemy import select, update

    store = SqlAlchemyDialogStore(session_factory)
    fixed_now_msk = datetime(2026, 8, 25, 1, 30, tzinfo=MOSCOW_TZ)
    yesterday_msk = datetime(2026, 8, 24, 23, 59, tzinfo=MOSCOW_TZ)
    today_msk = datetime(2026, 8, 25, 0, 30, tzinfo=MOSCOW_TZ)

    await store.log_concession("c-yesterday", _price_concession_event())
    await store.log_concession("c-today", _price_concession_event())
    async with session_factory() as session:
        await session.execute(
            update(ConcessionLog).where(ConcessionLog.dialog_id == "c-yesterday")
            .values(created_at=yesterday_msk)
        )
        await session.execute(
            update(ConcessionLog).where(ConcessionLog.dialog_id == "c-today")
            .values(created_at=today_msk)
        )
        await session.commit()

    assert await store.count_concessions_today(now=fixed_now_msk) == 1


# --------------------------------------------------------------------------
# R10 сквозь весь путь: ToolExecutor -> concessions_today_provider ->
# decide() -> реальный Postgres. Не просто count_concessions_today() саму
# по себе (это уже проверено выше) — а что порог реально СРАБАТЫВАЕТ на
# живом ToolExecutor, тем же путём, каким его вызывает AgentLoop в проде.
# --------------------------------------------------------------------------

_CALCULATE_PRICE_ARGS = {
    "zone_id": "bath_russian", "date": "2026-07-18",
    "start_time": "14:00", "hours": 3, "guests": 6,
}


async def _request_concession(store, dialog_id: str):
    from app.agent.tools import ToolExecutor
    from app.kb.loader import load_catalog

    kb = load_catalog()
    ex = ToolExecutor(kb, dialog_id, concessions_today_provider=store.count_concessions_today)
    await ex.run("calculate_price", _CALCULATE_PRICE_ARGS)
    result = await ex.run("request_concession", {"observed_triggers": ["price_objection"]})
    return result, ex, kb.concessions.policy.max_concessions_per_day


async def test_r10_nth_concession_is_still_allowed(session_factory):
    """N-1 уступок уже выдано (другими диалогами) — N-я, для НОВОГО
    диалога, всё ещё в пределах лимита: R10 её не блокирует, уступка
    реально ВЫДАЁТСЯ.

    Раньше этот тест проверял `daily_limit_exhausted` вместо
    `result["allowed"]`, потому что `_slot_known_free()` был захардкожен в
    False и R6 отказывал сразу после R10 — уступка не выдавалась никогда.
    После подключения провайдера (UNKNOWN больше не «занято») ассерт
    вернулся к тому, чем он и должен был быть.
    """
    store = SqlAlchemyDialogStore(session_factory)
    _, _ex, limit = await _request_concession(store, "d-probe")
    assert limit > 1, "тест предполагает лимит больше единицы (реально 5)"

    for i in range(limit - 1):
        await store.log_concession(f"c-other-{i}", _price_concession_event())

    result, ex, _ = await _request_concession(store, "d-new")

    decision = ex.concession_events[-1].decision
    assert decision.daily_limit_exhausted is False
    assert result["allowed"] is True
    assert "R10" not in (decision.denial_reason or "")


async def test_r10_nplus1th_concession_is_denied(session_factory):
    """Ровно `limit` уступок уже выдано СЕГОДНЯ по РАЗНЫМ чатам — следующий
    запрос, из третьего, нового диалога, запрещён явно, не молча."""
    store = SqlAlchemyDialogStore(session_factory)
    _, _ex, limit = await _request_concession(store, "d-probe")

    for i in range(limit):
        await store.log_concession(f"c-other-{i}", _price_concession_event())
    assert await store.count_concessions_today() == limit

    result, ex, _ = await _request_concession(store, "d-new")

    assert result["allowed"] is False
    decision = ex.concession_events[-1].decision
    assert decision.daily_limit_exhausted is True
    assert "R10" in decision.denial_reason
    assert str(limit) in decision.denial_reason


async def test_r10_resets_the_next_day(session_factory):
    """Вчерашние `limit` уступок не должны блокировать сегодняшний запрос —
    граница дня по Москве, реальное «вчера», а не смещение от фикстуры."""
    store = SqlAlchemyDialogStore(session_factory)
    _, _ex, limit = await _request_concession(store, "d-probe")

    for i in range(limit):
        await store.log_concession(f"c-yesterday-{i}", _price_concession_event())
    async with session_factory() as session:
        from sqlalchemy import update
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        await session.execute(update(ConcessionLog).values(created_at=yesterday))
        await session.commit()
    assert await store.count_concessions_today() == 0

    result, ex, _ = await _request_concession(store, "d-today")

    decision = ex.concession_events[-1].decision
    assert decision.daily_limit_exhausted is False
    assert result["allowed"] is True
    assert "R10" not in (decision.denial_reason or "")


# --------------------------------------------------------------------------
# R6 — занятость слота: FREE / BUSY / UNKNOWN через живой ToolExecutor
#
# Провайдер броней здесь фейковый (сеть в тестах не нужна и вредна), а вот
# счётчик уступок и запись решений — настоящий Postgres: проверяется, что
# весь путь «провайдер -> ToolExecutor -> decide -> ConcessionLog» сходится,
# а не только логика движка в отрыве от хранилища.
# --------------------------------------------------------------------------

class _FakeBookingProvider:
    """Отдаёт заранее заданный статус. `calls` — чтобы проверить кеш."""

    def __init__(self, status):
        self.status = status
        self.calls: list[dict] = []

    async def check_availability(self, zone_id, date, start_time=None, hours=None):
        from app.booking.base import Availability

        self.calls.append({"zone_id": zone_id, "date": date, "start_time": start_time, "hours": hours})
        return Availability(status=self.status, reason="каталог пуст")


async def _concession_with_provider(store, dialog_id, provider, *, zone="bath_russian"):
    from app.agent.tools import ToolExecutor
    from app.kb.loader import load_catalog

    kb = load_catalog()
    ex = ToolExecutor(
        kb, dialog_id,
        booking_provider=provider,
        concessions_today_provider=store.count_concessions_today,
    )
    await ex.run("calculate_price", {**_CALCULATE_PRICE_ARGS, "zone_id": zone})
    # Неценовые ступени уже израсходованы — интересует именно ЦЕНОВАЯ, ради
    # которой всё это и делалось.
    ex.state = DialogConcessionState(base_price_quoted=True, used_tiers=frozenset({1, 2, 3, 4}))
    result = await ex.run("request_concession", {"observed_triggers": ["price_objection"]})
    return result, ex


async def test_r6_unknown_availability_routes_the_price_tier_to_the_operator(session_factory):
    """Каталог YCLIENTS пуст -> UNKNOWN. Раньше это схлопывалось в «занято»
    и R6 отказывал молча; теперь решение уходит человеку."""
    from app.booking.base import AvailabilityStatus

    store = SqlAlchemyDialogStore(session_factory)
    provider = _FakeBookingProvider(AvailabilityStatus.UNKNOWN)

    result, ex = await _concession_with_provider(store, "d-unknown", provider)

    decision = ex.concession_events[-1].decision
    assert result["allowed"] is False
    assert decision.requires_operator_approval is True
    assert decision.kind == "price"
    assert "неизвестна" in decision.denial_reason
    # И конвейер увидит это как «нужно одобрение» (тот же предикат, что в
    # app/pipeline.py) — иначе решение никуда бы не поехало.
    assert ex.concession_events[-1].needs_operator_approval is True


async def test_r6_free_slot_passes_the_slot_check_and_stops_at_occupancy(session_factory):
    """FREE проходит R6 — и упирается в СЛЕДУЮЩЕЕ правило, R7: загрузка на
    дату по-прежнему неизвестна, потому что `occupancy_ratio` в
    `ToolExecutor` тоже никем не заполняется (тот же класс необорванного
    провода, что и `_slot_known_free` до этой правки, только одним правилом
    ниже — см. «Известные пробелы» в README).

    Итог для прода при пустом каталоге YCLIENTS: ценовая уступка всегда
    доезжает до оператора и никогда не выдаётся автоматически — это
    безопасно и ровно та логика, ради которой заводился
    requires_operator_approval. Проверка, что при ИЗВЕСТНОЙ загрузке
    уступка реально выдаётся (10 500 -> 7 500 ₽), живёт на уровне движка:
    tests/test_concessions.py::test_r6_free_slot_with_known_occupancy_grants.

    Главное, что фиксирует этот тест: отказ БОЛЬШЕ НЕ на R6 и не молчаливый.
    """
    from app.booking.base import AvailabilityStatus

    store = SqlAlchemyDialogStore(session_factory)
    provider = _FakeBookingProvider(AvailabilityStatus.FREE)

    result, ex = await _concession_with_provider(store, "d-free", provider)

    decision = ex.concession_events[-1].decision
    assert result["allowed"] is False
    assert decision.requires_operator_approval is True     # к человеку, не отказ
    assert decision.kind == "price"
    assert "Загрузка" in decision.denial_reason            # R7, не R6
    assert "слот занят" not in (decision.denial_reason or "")


async def test_r6_busy_slot_denies_without_bothering_the_operator(session_factory):
    """BUSY — отказ, и НЕ на согласование: занятый слот не предмет торга,
    клиенту нужно другое время."""
    from app.booking.base import AvailabilityStatus

    store = SqlAlchemyDialogStore(session_factory)
    provider = _FakeBookingProvider(AvailabilityStatus.BUSY)

    result, ex = await _concession_with_provider(store, "d-busy", provider)

    decision = ex.concession_events[-1].decision
    assert result["allowed"] is False
    assert decision.requires_operator_approval is False
    assert "занят" in decision.denial_reason
    assert ex.concession_events[-1].needs_operator_approval is False


async def test_availability_is_asked_once_per_slot_within_a_turn(session_factory):
    """check_availability и проверка слота при уступке спрашивают про ОДИН
    слот — второй сетевой запрос не нужен и опасен: провайдер мог бы
    ответить иначе, и агент принял бы два решения на разных данных."""
    from app.agent.tools import ToolExecutor
    from app.booking.base import AvailabilityStatus
    from app.kb.loader import load_catalog

    store = SqlAlchemyDialogStore(session_factory)
    provider = _FakeBookingProvider(AvailabilityStatus.FREE)
    kb = load_catalog()
    ex = ToolExecutor(kb, "d-cache", booking_provider=provider,
                      concessions_today_provider=store.count_concessions_today)

    await ex.run("calculate_price", _CALCULATE_PRICE_ARGS)
    await ex.run("check_availability", {"zone_id": "bath_russian", "date": "2026-07-18",
                                        "start_time": "14:00", "hours": 3})
    ex.state = DialogConcessionState(base_price_quoted=True, used_tiers=frozenset({1, 2, 3, 4}))
    await ex.run("request_concession", {"observed_triggers": ["price_objection"]})

    assert len(provider.calls) == 1


async def test_provider_failure_is_unknown_not_a_crash(session_factory):
    """Сбой YCLIENTS не должен ронять ход агента: «не знаю» — законный
    ответ, и он уводит решение к оператору, а не наружу исключением."""
    from app.agent.tools import ToolExecutor
    from app.kb.loader import load_catalog

    class _Broken:
        async def check_availability(self, **kw):
            raise RuntimeError("YCLIENTS 503")

    store = SqlAlchemyDialogStore(session_factory)
    kb = load_catalog()
    ex = ToolExecutor(kb, "d-broken", booking_provider=_Broken(),
                      concessions_today_provider=store.count_concessions_today)
    await ex.run("calculate_price", _CALCULATE_PRICE_ARGS)
    ex.state = DialogConcessionState(base_price_quoted=True, used_tiers=frozenset({1, 2, 3, 4}))

    result = await ex.run("request_concession", {"observed_triggers": ["price_objection"]})

    decision = ex.concession_events[-1].decision
    assert result["allowed"] is False
    assert decision.requires_operator_approval is True


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


async def test_ops_store_pending_concession_round_trips_all_fields(seeded):
    """is_concession/fallback_text/due_at — новые колонки миграции
    95c862132eaf, ради модерации ценовых уступок."""
    due = NOW + timedelta(minutes=15)
    writer = SqlAlchemyOpsStore(seeded)
    await writer.set_pending("c-1", PendingReply(
        chat_id="c-1", text="6 000 ₽ вместо 7 000 ₽.", created_at=NOW,
        is_concession=True, fallback_text="Уточню детали и вернусь с ответом.", due_at=due,
    ))

    reader = SqlAlchemyOpsStore(seeded)      # «после рестарта контейнера»
    pending = await reader.get_pending("c-1")

    assert pending.is_concession is True
    assert pending.fallback_text == "Уточню детали и вернусь с ответом."
    assert pending.due_at == due


async def test_ops_store_regular_pending_defaults_is_concession_to_false(seeded):
    """Обычный DRY_RUN-холд (не запрос на скидку) — is_concession=False по
    умолчанию, а не требует явного указания на каждом вызове."""
    store = SqlAlchemyOpsStore(seeded)
    await store.set_pending("c-1", PendingReply(chat_id="c-1", text="текст", created_at=NOW))

    pending = await store.get_pending("c-1")

    assert pending.is_concession is False
    assert pending.due_at is None


async def test_ops_store_list_due_concessions_finds_overdue_and_skips_the_rest(seeded):
    store = SqlAlchemyOpsStore(seeded)
    await store.set_pending("c-1", PendingReply(
        chat_id="c-1", text="просрочен", created_at=NOW - timedelta(minutes=20),
        is_concession=True, fallback_text="fb-1", due_at=NOW - timedelta(minutes=1),
    ))

    async with seeded() as session:
        session.add(Chat(chat_id="c-2"))
        await session.commit()
    await store.set_pending("c-2", PendingReply(
        chat_id="c-2", text="ещё не подошёл срок", created_at=NOW,
        is_concession=True, fallback_text="fb-2", due_at=NOW + timedelta(minutes=10),
    ))

    async with seeded() as session:
        session.add(Chat(chat_id="c-3"))
        await session.commit()
    await store.set_pending("c-3", PendingReply(
        chat_id="c-3", text="обычный холд, не скидка", created_at=NOW - timedelta(minutes=30),
    ))

    due = await store.list_due_concessions(NOW)

    assert [p.chat_id for p in due] == ["c-1"]
    assert due[0].fallback_text == "fb-1"


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


# ==========================================================================
# Правки каталога (управление ассистентом из Telegram) — реальный Postgres
#
# InMemoryOverrideStore в tests/test_kb_editor.py и tests/test_menu_service.py
# проверяет ПРАВИЛА (валидация, откат, пересчёт цены). Здесь — что ровно то
# же самое переживает настоящую запись/чтение из БД: единственное, ради
# чего вся эта таблица вообще существует, — файловая система контейнера на
# Railway эфемерная, а Postgres нет.
# ==========================================================================

async def test_override_store_survives_a_new_instance(session_factory):
    """«Новый процесс» — эквивалент рестарта контейнера на Railway."""
    from app.kb.override_store import SqlAlchemyOverrideStore

    writer = SqlAlchemyOverrideStore(session_factory)
    await writer.add(
        path="$.catalog.zones[id=dome_bags].pricing.weekend_per_hour",
        value={"value": 1800, "resolved_from": "оператор через Telegram (user_id=111)"},
        previous_value={"value": 1500}, field_key="we_hour", zone_id="dome_bags",
        changed_by=111,
    )

    reader = SqlAlchemyOverrideStore(session_factory)
    active = await reader.list_active()

    assert len(active) == 1
    assert active[0].value["value"] == 1800
    assert active[0].changed_by == 111


async def test_override_store_last_active_is_the_most_recent(session_factory):
    from app.kb.override_store import SqlAlchemyOverrideStore

    store = SqlAlchemyOverrideStore(session_factory)
    await store.add(path="$.a", value=1, previous_value=None, field_key=None, zone_id=None, changed_by=1)
    second = await store.add(path="$.b", value=2, previous_value=None, field_key=None, zone_id=None, changed_by=1)

    last = await store.last_active()

    assert last.id == second.id


async def test_override_store_revert_marks_reverted_not_deletes(session_factory):
    from app.kb.override_store import SqlAlchemyOverrideStore

    store = SqlAlchemyOverrideStore(session_factory)
    record = await store.add(
        path="$.a", value=2, previous_value=1, field_key=None, zone_id=None, changed_by=111,
    )

    reverted = await store.revert(record.id, reverted_by=222)

    assert reverted.reverted_by == 222
    assert reverted.reverted_at is not None
    # Строка НЕ удалена — journal её всё ещё видит.
    journal = await store.list_journal()
    assert len(journal) == 1
    assert journal[0].id == record.id
    # И больше не активна — накладываться на YAML не будет.
    assert await store.list_active() == []


async def test_override_store_reverting_twice_is_a_noop(session_factory):
    from app.kb.override_store import SqlAlchemyOverrideStore

    store = SqlAlchemyOverrideStore(session_factory)
    record = await store.add(path="$.a", value=2, previous_value=1, field_key=None, zone_id=None, changed_by=1)
    await store.revert(record.id, reverted_by=1)

    assert await store.revert(record.id, reverted_by=1) is None


async def test_overrides_from_the_database_apply_on_top_of_the_yaml(session_factory):
    """Полный путь: правка сохранена в БД реальным store -> `load_catalog`
    с этими правками -> итоговая цена именно та, что была введена, а не из
    YAML. Ради этого пути таблица и заведена."""
    from app.kb.loader import load_catalog
    from app.kb.override_store import SqlAlchemyOverrideStore, to_overrides

    store = SqlAlchemyOverrideStore(session_factory)
    await store.add(
        path="$.catalog.zones[id=dome_bags].pricing.weekend_per_hour",
        value={"value": 1800, "resolved_from": "оператор через Telegram (user_id=111)"},
        previous_value={"value": 1500}, field_key="we_hour", zone_id="dome_bags",
        changed_by=111,
    )

    overrides = to_overrides(await store.list_active())
    kb = load_catalog(overrides=overrides)

    zone = next(z for z in kb.catalog.zones if z.id == "dome_bags")
    assert zone.pricing["weekend_per_hour"]["value"] == 1800


async def test_reverted_overrides_are_not_applied(session_factory):
    from app.kb.loader import load_catalog
    from app.kb.override_store import SqlAlchemyOverrideStore, to_overrides

    store = SqlAlchemyOverrideStore(session_factory)
    record = await store.add(
        path="$.catalog.zones[id=dome_bags].pricing.weekend_per_hour",
        value={"value": 1800}, previous_value={"value": 1500},
        field_key="we_hour", zone_id="dome_bags", changed_by=111,
    )
    await store.revert(record.id, reverted_by=111)

    overrides = to_overrides(await store.list_active())
    kb = load_catalog(overrides=overrides)

    zone = next(z for z in kb.catalog.zones if z.id == "dome_bags")
    assert zone.pricing["weekend_per_hour"]["value"] == 1500      # исходное


async def test_catalog_editor_over_the_sql_store_end_to_end(session_factory):
    """CatalogEditor (валидация + пересчёт примера цены) поверх настоящего
    Postgres, а не InMemory — тот же путь, каким его вызывает Telegram-бот
    в проде."""
    from app.kb.editable import field_by_key
    from app.kb.editor import CatalogEditor
    from app.kb.override_store import SqlAlchemyOverrideStore

    editor = CatalogEditor(SqlAlchemyOverrideStore(session_factory))

    preview = await editor.preview("we_hour", "1000", user_id=111, zone_id="dome_bags")
    result = await editor.apply(preview, user_id=111)

    assert result.record.changed_by == 111
    assert "4000" in result.price_example        # 1000 ₽/ч × 4 ч

    # «Новый процесс» подхватывает то же значение.
    fresh_editor = CatalogEditor(SqlAlchemyOverrideStore(session_factory))
    value = await fresh_editor.current_value(field_by_key("we_hour"), "dome_bags")
    assert value["value"] == 1000

    reverted = await fresh_editor.revert_last(user_id=111)
    assert "6000" in reverted.price_example      # обратно к 1500 ₽/ч × 4 ч
