"""Dataclasses representing database entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
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
    status: str  # active | inactive | error
    display_name: Optional[str] = None
    last_error: Optional[str] = None

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
