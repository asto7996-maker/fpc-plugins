"""Dataclasses representing database entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional


@dataclass
class Agent:
    id: int
    phone: str
    session_string: str
    api_id: int
    api_hash: str
    proxy_type: Optional[str]  # socks5 | http | None
    proxy_host: Optional[str]
    proxy_port: Optional[int]
    proxy_username: Optional[str]
    proxy_password: Optional[str]
    work_window_start: str  # HH:MM
    work_window_end: str  # HH:MM
    status: str  # active | inactive | cooldown | banned | paused
    display_name: Optional[str] = None
    last_error: Optional[str] = None
    device_model: Optional[str] = None
    system_version: Optional[str] = None
    app_version: Optional[str] = None
    lang_code: Optional[str] = None
    cooldown_until: Optional[str] = None
    last_action_at: Optional[str] = None

    def parse_work_window(self) -> tuple[time, time]:
        start = datetime.strptime(self.work_window_start, "%H:%M").time()
        end = datetime.strptime(self.work_window_end, "%H:%M").time()
        return start, end

    def is_within_work_window(self, now: Optional[datetime] = None) -> bool:
        """Return True if current local time is inside the agent's work window.

        Supports overnight windows (e.g. 22:00–06:00).
        """
        now = now or datetime.now()
        current = now.time()
        start, end = self.parse_work_window()
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def is_in_cooldown(self, now: Optional[datetime] = None) -> bool:
        if not self.cooldown_until:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            until = datetime.fromisoformat(self.cooldown_until)
        except ValueError:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now < until

    @property
    def has_fingerprint(self) -> bool:
        return bool(self.device_model and self.system_version and self.app_version)

    @property
    def proxy_tuple(self) -> Optional[tuple]:
        """Telethon-compatible proxy tuple or None."""
        if not self.proxy_type or not self.proxy_host or not self.proxy_port:
            return None

        kind = self.proxy_type.lower()
        if kind == "socks5":
            import socks

            proxy_type = socks.SOCKS5
        elif kind in {"http", "https"}:
            import socks

            proxy_type = socks.HTTP
        else:
            raise ValueError(f"Unsupported proxy type: {self.proxy_type}")

        return (
            proxy_type,
            self.proxy_host,
            int(self.proxy_port),
            True,
            self.proxy_username,
            self.proxy_password,
        )


@dataclass
class Keyword:
    id: int
    keyword: str
    category: str
    is_active: bool = True
    knowledge_base_id: Optional[int] = None


@dataclass
class KnowledgeEntry:
    id: int
    title: str
    response_template: str
    category: str
    is_active: bool = True


@dataclass
class InteractionLog:
    id: int
    chat_id: int
    message_id: int
    agent_id: int
    keyword_id: Optional[int]
    knowledge_base_id: Optional[int]
    timestamp: str
    status: str = "sent"
    trigger_keyword: Optional[str] = None
    reply_text: Optional[str] = None
    source_text: Optional[str] = None


@dataclass
class StopWord:
    id: int
    word: str
    is_active: bool = True
