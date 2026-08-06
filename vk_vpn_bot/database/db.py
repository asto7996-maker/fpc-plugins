"""
Асинхронное подключение к БД и CRUD-операции с пользователями.
Поддерживает SQLite (aiosqlite) и PostgreSQL (asyncpg) через DATABASE_URL.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings
from database.models import Base, User

# Глобальные объекты движка и фабрики сессий
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_sqlite_dir(database_url: str) -> None:
    """Для SQLite создаём директорию под файл БД, если её ещё нет."""
    if not database_url.startswith("sqlite"):
        return
    # sqlite+aiosqlite:///./data/vpn_bot.db  или  sqlite+aiosqlite:////abs/path
    raw = database_url.split(":///", 1)[-1]
    db_path = Path(raw)
    if not db_path.is_absolute():
        # Относительный путь — от корня проекта
        from config import BASE_DIR

        db_path = BASE_DIR / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)


async def init_db() -> None:
    """Инициализирует движок и создаёт таблицы."""
    global _engine, _session_factory

    settings = get_settings()
    _ensure_sqlite_dir(settings.database_url)

    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Корректно закрывает соединение с БД."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_session() -> AsyncSession:
    """Возвращает новую асинхронную сессию."""
    if _session_factory is None:
        raise RuntimeError("БД не инициализирована. Вызовите init_db() при старте.")
    return _session_factory()


async def get_or_create_user(user_id: int, first_name: str | None = None) -> User:
    """Находит пользователя по VK ID или создаёт нового."""
    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                user_id=user_id,
                first_name=first_name,
                is_trial_used=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        elif first_name and user.first_name != first_name:
            user.first_name = first_name
            await session.commit()
            await session.refresh(user)

        return user


async def get_user(user_id: int) -> User | None:
    """Возвращает пользователя или None."""
    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        return result.scalar_one_or_none()


async def activate_trial(user_id: int, vpn_key: str, trial_days: int) -> User:
    """
    Активирует тестовый период:
    - ставит is_trial_used = True
    - продлевает subscription_end на trial_days
    - сохраняет vpn_key
    """
    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one()

        now = datetime.utcnow()
        user.is_trial_used = True
        user.subscription_end = now + timedelta(days=trial_days)
        user.vpn_key = vpn_key

        await session.commit()
        await session.refresh(user)
        return user


async def renew_subscription(user_id: int, days: int, vpn_key: str | None = None) -> User:
    """Продлевает подписку на N дней (от текущей даты окончания или от сейчас)."""
    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one()

        now = datetime.utcnow()
        base = user.subscription_end if user.is_subscription_active(now) else now
        if base.tzinfo is not None:
            base = base.replace(tzinfo=None)
        user.subscription_end = base + timedelta(days=days)
        if vpn_key:
            user.vpn_key = vpn_key

        await session.commit()
        await session.refresh(user)
        return user


async def update_vpn_key(user_id: int, vpn_key: str) -> User:
    """Обновляет только ключ VPN у пользователя."""
    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one()
        user.vpn_key = vpn_key
        await session.commit()
        await session.refresh(user)
        return user
