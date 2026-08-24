"""Тесты SqlAlchemyZoneMapping — реальная (не мок) БД: in-memory SQLite,
только таблица zone_service_map (остальная metadata использует
Postgres-специфичные типы — JSONB/ARRAY — которых SQLite не понимает,
поэтому создаём точечно, не всю Base.metadata).

`get`/`mapped_zones`/`unmapped_zones` читают только кеш в памяти и никогда
не трогают сессию сами — это то самое свойство, которое здесь и
проверяется: без `load()` они не видят ничего, после `load()` видят
записанное, а `set()` обновляет кеш точечно без нового `load()`.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.booking.mapping import SqlAlchemyZoneMapping, ZoneServiceMap


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ZoneServiceMap.metadata.create_all, tables=[ZoneServiceMap.__table__])
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def seeded_session_factory(session_factory):
    async with session_factory() as session:
        session.add(ZoneServiceMap(
            zone_id="bath_russian", service_id="10", staff_id="20", company_id="1", enabled=True,
        ))
        session.add(ZoneServiceMap(
            zone_id="bath_garage", service_id="11", staff_id="21", company_id="1", enabled=False,
        ))
        await session.commit()
    return session_factory


# --------------------------------------------------------------------------
# «SqlAlchemyZoneMapping читает и кеширует»
# --------------------------------------------------------------------------

async def test_get_returns_none_before_load(seeded_session_factory):
    """Без явного load() кеш пуст — то же безопасное вырождение, что и у
    пустого InMemoryZoneMapping, а не поход в базу на каждый вызов."""
    mapping = SqlAlchemyZoneMapping(seeded_session_factory)
    assert mapping.get("bath_russian") is None


async def test_load_populates_cache_from_db(seeded_session_factory):
    mapping = SqlAlchemyZoneMapping(seeded_session_factory)
    await mapping.load()
    row = mapping.get("bath_russian")
    assert row is not None
    assert row["service_id"] == "10"
    assert row["staff_id"] == "20"
    assert row["company_id"] == "1"


async def test_disabled_row_is_treated_as_absent(seeded_session_factory):
    mapping = SqlAlchemyZoneMapping(seeded_session_factory)
    await mapping.load()
    assert mapping.get("bath_garage") is None   # enabled=False в БД


async def test_get_after_load_does_not_hit_the_db_again(seeded_session_factory):
    """Проверяем именно КЕШИРОВАНИЕ: закрываем движок под капотом сессии
    невозможно между вызовами get(), поэтому доказываем это иначе — get()
    синхронный и не может ходить в асинхронную сессию в принципе."""
    import inspect

    mapping = SqlAlchemyZoneMapping(seeded_session_factory)
    await mapping.load()
    assert not inspect.iscoroutinefunction(mapping.get)


async def test_mapped_and_unmapped_zones_after_load(seeded_session_factory):
    mapping = SqlAlchemyZoneMapping(seeded_session_factory)
    await mapping.load()
    assert mapping.mapped_zones() == ["bath_russian"]   # bath_garage выключена
    assert mapping.unmapped_zones(["bath_russian", "bath_garage", "dome_bags"]) == [
        "bath_garage", "dome_bags",
    ]


async def test_missing_table_row_is_unmapped(seeded_session_factory):
    mapping = SqlAlchemyZoneMapping(seeded_session_factory)
    await mapping.load()
    assert mapping.get("dome_bags") is None


# --------------------------------------------------------------------------
# «Инвалидация после правки»
# --------------------------------------------------------------------------

async def test_set_writes_to_db_and_updates_cache_immediately(session_factory):
    mapping = SqlAlchemyZoneMapping(session_factory)
    await mapping.load()
    assert mapping.get("dome_bags") is None

    await mapping.set("dome_bags", service_id="30", staff_id="40", company_id="1")

    row = mapping.get("dome_bags")
    assert row is not None
    assert row["service_id"] == "30"


async def test_set_persists_across_a_fresh_load_from_a_new_instance(session_factory):
    """Не просто кеш в памяти одного объекта — реально долетает до БД."""
    writer = SqlAlchemyZoneMapping(session_factory)
    await writer.set("dome_bags", service_id="30", staff_id="40", company_id="1")

    reader = SqlAlchemyZoneMapping(session_factory)   # «другой процесс»
    await reader.load()
    row = reader.get("dome_bags")
    assert row is not None
    assert row["service_id"] == "30"


async def test_set_updates_existing_row_not_a_duplicate(seeded_session_factory):
    mapping = SqlAlchemyZoneMapping(seeded_session_factory)
    await mapping.load()

    await mapping.set("bath_russian", service_id="99")

    assert mapping.get("bath_russian")["service_id"] == "99"
    # тот же zone_id — не новая строка поверх старой
    async with seeded_session_factory() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(ZoneServiceMap).where(ZoneServiceMap.zone_id == "bath_russian")
        )).scalars().all()
        assert len(rows) == 1


async def test_set_without_enabled_defaults_new_row_to_enabled(session_factory):
    mapping = SqlAlchemyZoneMapping(session_factory)
    await mapping.set("tent", service_id="50")
    assert mapping.get("tent") is not None


async def test_disabling_a_zone_via_set_removes_it_from_get(seeded_session_factory):
    mapping = SqlAlchemyZoneMapping(seeded_session_factory)
    await mapping.load()
    assert mapping.get("bath_russian") is not None

    await mapping.set("bath_russian", enabled=False)

    assert mapping.get("bath_russian") is None


async def test_invalidate_clears_cache_without_touching_db():
    mapping = SqlAlchemyZoneMapping(session_factory=None)  # никогда не должен понадобиться
    mapping._cache = {"bath_russian": {"enabled": True}}
    mapping.invalidate()
    assert mapping.get("bath_russian") is None
