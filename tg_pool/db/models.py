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
    Float,
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


class UserRole(str, enum.Enum):
    creator = "creator"
    admin = "admin"
    user = "user"


class PanelUser(Base, TimestampMixin):
    """Telegram user of the control panel (invite-gated access)."""

    __tablename__ = "panel_users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=False),
        nullable=False,
        default=UserRole.user,
    )
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class InviteCode(Base, TimestampMixin):
    """One-time invite codes for panel access."""

    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


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
    # Per-account draft-engine participation (HITL Gemini assistant)
    assistant_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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


class DraftStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    sent = "sent"
    failed = "failed"
    expired = "expired"


class AutoReplySettings(Base, TimestampMixin):
    """
    Global settings for the Gemini draft assistant.

    Singleton row (id=1). auto_approve_enabled defaults to False —
    drafts go to operators for review unless explicitly enabled.
    """

    __tablename__ = "auto_reply_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_approve_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gemini_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gemini_model: Mapped[str] = mapped_column(String(64), default="gemini-2.5-flash", nullable=False)
    promote_username: Mapped[str] = mapped_column(
        String(64), default="@PaskodVPN_bot", nullable=False
    )
    delay_min_sec: Mapped[float] = mapped_column(Float, default=120.0, nullable=False)
    delay_max_sec: Mapped[float] = mapped_column(Float, default=480.0, nullable=False)
    typing_min_sec: Mapped[float] = mapped_column(Float, default=3.0, nullable=False)
    typing_max_sec: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    max_replies_per_chat_day: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    dedupe_ttl_hours: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    trigger_regex: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=(
            r"(?i)\b(vpn|впн|вэпээн|прокси|proxy|замедлен\w*|обход\w*|"
            r"заблок\w*|не\s*работает\s*ют(уб|ube)|доступ\w*)\b"
        ),
    )


class PendingDraft(Base, TimestampMixin):
    """Operator-reviewable Gemini draft before sending via a userbot."""

    __tablename__ = "pending_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    matched_trigger: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[DraftStatus] = mapped_column(
        Enum(DraftStatus, name="draft_status", native_enum=False),
        nullable=False,
        default=DraftStatus.pending,
        index=True,
    )
    admin_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    admin_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
