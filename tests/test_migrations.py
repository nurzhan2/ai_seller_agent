"""Тесты Alembic-миграции: применяется на чистой базе, откатывается назад.

Реальный Postgres, не SQLite — схема опирается на Postgres-специфичные типы
(JSONB, ARRAY, именованные ENUM), которых SQLite не понимает. Без доступной
базы весь модуль пропускается, а не падает — окружения без локального
Postgres (например, обычный прогон `pytest` без поднятой БД) не должны
краснеть на этом файле. Указать нестандартный адрес: TEST_DATABASE_URL.

По умолчанию используется отдельная база `parmangal_test` — не боевая/дев
база разработчика, специально ради того, чтобы эти тесты могли свободно
дропать и пересоздавать схему.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://parmangal_test:parmangal_test@127.0.0.1:5432/parmangal_test",
)

EXPECTED_TABLES = {
    "chats",
    "concession_log",
    "item_zone_map",
    "leads",
    "operator_actions",
    "zone_service_map",
    "dialog_states",
    "messages",
}
EXPECTED_ENUM_TYPES = {"chat_state", "direction", "author", "send_status"}


def _run(coro):
    return asyncio.run(coro)


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

    return _run(_ping())


pytestmark = pytest.mark.skipif(
    not _database_reachable(TEST_DATABASE_URL),
    reason=(
        "Нужен реальный Postgres на TEST_DATABASE_URL (по умолчанию — "
        "parmangal_test@127.0.0.1:5432) — миграции используют JSONB/ARRAY/"
        "именованные ENUM, которых SQLite не понимает."
    ),
)


def _table_names(url: str) -> set[str]:
    async def _names() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        finally:
            await engine.dispose()

    return _run(_names())


def _enum_type_names(url: str) -> set[str]:
    async def _names() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return _run(_names())


@pytest.fixture
def alembic_config(monkeypatch):
    """Направляет migrations/env.py на тестовую БД.

    `get_settings()` кеширован через `lru_cache` на уровне модуля — если его
    не сбросить, migrations/env.py в этом же процессе увидит DATABASE_URL от
    другого теста (или от импорта app.main где-то ещё в сборке) вместо
    подменённого здесь. Сбрасываем и до, и после — чтобы не протащить
    тестовый DATABASE_URL в остальные тесты сьюта.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    try:
        yield cfg
    finally:
        command.downgrade(cfg, "base")  # чистая база и для соседних тестов, и для follow-up прогона
        get_settings.cache_clear()


def test_upgrade_head_creates_all_tables_on_empty_db(alembic_config):
    command.downgrade(alembic_config, "base")  # гарантируем чистый старт независимо от прошлого состояния

    command.upgrade(alembic_config, "head")

    tables = _table_names(TEST_DATABASE_URL)
    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables


def test_downgrade_base_removes_tables_and_enum_types(alembic_config):
    command.upgrade(alembic_config, "head")

    command.downgrade(alembic_config, "base")

    tables = _table_names(TEST_DATABASE_URL)
    assert tables.isdisjoint(EXPECTED_TABLES)
    enum_types = _enum_type_names(TEST_DATABASE_URL)
    assert enum_types.isdisjoint(EXPECTED_ENUM_TYPES)


def test_upgrade_after_downgrade_is_idempotent(alembic_config):
    """Регрессия: autogenerate не генерирует DROP TYPE для именованных
    Postgres ENUM в downgrade() — без ручной правки второй upgrade после
    downgrade падал на CREATE TYPE ... DuplicateObjectError, потому что
    первый downgrade оставлял chat_state/direction/author/send_status
    висеть в базе. Воспроизведено и исправлено вручную в самой миграции —
    этот тест проверяет, что регрессия не вернётся."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    command.upgrade(alembic_config, "head")

    tables = _table_names(TEST_DATABASE_URL)
    assert EXPECTED_TABLES <= tables


def test_running_a_migration_does_not_disable_application_logging(alembic_config):
    """Регрессия: `fileConfig()` в migrations/env.py по умолчанию ОТКЛЮЧАЕТ
    все логгеры, которых нет в alembic.ini — то есть всё дерево «parmangal.*».
    Прогон миграции в том же процессе гасил логирование приложения молча,
    навсегда и целиком. Поймано косвенно (падали проверки логов в
    tests/test_main.py после того, как рядом появился ещё один файл,
    гоняющий alembic), поэтому здесь — прямая проверка, не зависящая от
    порядка файлов в прогоне.

    Свой handler, а не caplog: `fileConfig` заодно заменяет handler'ы
    корневого логгера, снося тот, который ставит caplog, — поэтому caplog
    после миграции слеп независимо от того, исправлен баг или нет, и
    проверять им тут нечего.
    """
    logger = logging.getLogger("parmangal.migrations_probe")
    logger.setLevel(logging.ERROR)

    command.upgrade(alembic_config, "head")

    assert not logger.disabled, (
        "migrations/env.py погасил логгер приложения — "
        "fileConfig нужен с disable_existing_loggers=False"
    )

    captured: list[str] = []

    class _Probe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Probe()
    logger.addHandler(handler)
    try:
        logger.error("после миграции логи обязаны работать")
    finally:
        logger.removeHandler(handler)

    assert captured == ["после миграции логи обязаны работать"]
