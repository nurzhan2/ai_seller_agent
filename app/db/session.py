"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    # Railway (и Heroku-совместимые платформы) отдают postgres:// и часто
    # дописывают sslmode=require — ни то, ни другое asyncpg не понимает
    # напрямую. См. Settings.normalized_database_url / app.config.normalize_database_url.
    url, connect_args = settings.normalized_database_url()
    return create_async_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,   # a stale connection must not surface as a 500
        echo=False,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
