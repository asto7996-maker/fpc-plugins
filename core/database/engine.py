"""
SQLAlchemy async engine с пулом соединений.
Готов к миграции с aiosqlite на PostgreSQL.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import DB_PATH

logger = logging.getLogger("starvell.db.engine")

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        logger.info("SQLAlchemy engine инициализирован: %s", DATABASE_URL)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def close_engine() -> None:
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
