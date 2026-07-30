"""
SQLAlchemy models for the account pool.

Account  — Telegram userbot identity + runtime health
Proxy    — sticky SOCKS5/HTTP endpoint bound 1:1 to an account
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tg_pool.db.base import Base, TimestampMixin


class AccountStatus(str, enum.Enum):
    active = "active"
    banned = "banned"
    flood_wait = "flood_wait"
    paused = "paused"
    spambot = "spambot"  # restricted by @SpamBot


class ProxyProtocol(str, enum.Enum):
    socks5 = "socks5"
    http = "http"


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    session_string: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Telegram API credentials (may differ per account)
    api_id: Mapped[int] = mapped_column(Integer, nullable=False)
    api_hash: Mapped[str] = mapped_column(String(128), nullable=False)

    # Sticky device fingerprint — MUST stay constant for the lifetime of the session
    device_model: Mapped[str] = mapped_column(String(64), nullable=False)
    system_version: Mapped[str] = mapped_column(String(64), nullable=False)
    app_version: Mapped[str] = mapped_column(String(32), nullable=False)
    lang_code: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")

    proxy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("proxies.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,  # one proxy → one account
    )

    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name="account_status", native_enum=False),
        nullable=False,
        default=AccountStatus.paused,
    )
    flood_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_spambot_restricted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    total_actions_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_day_key: Mapped[Optional[str]] = mapped_column(
        String(10),
        nullable=True,
        doc="UTC date YYYY-MM-DD for which total_actions_today is valid",
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    proxy: Mapped[Optional["Proxy"]] = relationship(
        "Proxy",
        back_populates="account",
        foreign_keys=[proxy_id],
        uselist=False,
    )


class Proxy(Base, TimestampMixin):
    __tablename__ = "proxies"
    __table_args__ = (UniqueConstraint("ip", "port", "username", name="uq_proxy_endpoint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ip: Mapped[str] = mapped_column(String(128), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    password: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    protocol: Mapped[ProxyProtocol] = mapped_column(
        Enum(ProxyProtocol, name="proxy_protocol", native_enum=False),
        nullable=False,
        default=ProxyProtocol.socks5,
    )
    # Denormalized reverse link (optional; Account.proxy_id is the source of truth)
    assigned_account_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Mirrors Account.id when bound; kept for admin listing convenience",
    )
    is_alive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    account: Mapped[Optional[Account]] = relationship(
        "Account",
        back_populates="proxy",
        foreign_keys=[Account.proxy_id],
        uselist=False,
    )

    def as_telethon_tuple(self) -> tuple:
        """
        Build a Telethon-compatible proxy tuple.

        Format: (proxy_type, host, port, rdns, username, password)
        Critical: always pass the SAME proxy for a given session — rotating
        proxies on an authorized session is a common ban trigger.
        """
        import socks

        proxy_type = socks.SOCKS5 if self.protocol == ProxyProtocol.socks5 else socks.HTTP
        return (
            proxy_type,
            self.ip,
            int(self.port),
            True,  # rdns — resolve DNS remotely through the proxy
            self.username,
            self.password,
        )
