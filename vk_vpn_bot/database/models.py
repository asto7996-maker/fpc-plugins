"""
Модели пользователей VPN-бота (SQLAlchemy 2.0).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Базовый класс декларативных моделей."""


class User(Base):
    """
    Пользователь бота.

    user_id            — VK ID пользователя
    created_at         — дата регистрации в боте
    subscription_end   — дата окончания подписки (None = нет активной)
    vpn_key            — выданный ключ/ссылка конфигурации
    is_trial_used      — был ли уже использован тестовый период
    bedolaga_user_id   — ID пользователя в Bedolaga Web API
    first_name         — имя из VK
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    subscription_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    vpn_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_trial_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bedolaga_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    def is_subscription_active(self, now: datetime | None = None) -> bool:
        """Проверяет, действует ли подписка прямо сейчас."""
        if self.subscription_end is None:
            return False
        current = now or datetime.utcnow()
        end = self.subscription_end
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        if current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        return end > current
