"""
Идентификация сообщений Starvell для дедупликации (без повторной обработки).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any


def message_id(msg: dict) -> str:
    for key in ("id", "messageId", "message_id", "uuid"):
        val = msg.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def message_ts(msg: dict) -> int:
    for key in ("createdAt", "created_at", "sentAt", "sent_at", "timestamp", "date"):
        val = msg.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            ts = int(val)
            return ts // 1000 if ts > 10_000_000_000 else ts
        if isinstance(val, str) and val.isdigit():
            ts = int(val)
            return ts // 1000 if ts > 10_000_000_000 else ts
    return 0


def message_anchor(msg: dict) -> str:
    """Стабильный ключ сообщения для БД и дедупликации."""
    mid = message_id(msg)
    if mid:
        return f"id:{mid}"
    text = str(msg.get("content") or msg.get("text") or "").strip()
    if not text:
        return ""
    ts = message_ts(msg) or int(time.time())
    digest = hashlib.sha256(f"{ts}\n{text}".encode("utf-8")).hexdigest()[:20]
    return f"fp:{digest}"


def anchor_from_context(chat_id: str, message_id_str: str, text: str, raw: dict | None = None) -> str:
    mid = (message_id_str or "").strip()
    if mid:
        return f"id:{mid}"
    if raw:
        a = message_anchor(raw)
        if a:
            return a
    text = (text or "").strip()
    if not text:
        return ""
    digest = hashlib.sha256(f"{chat_id}\n{text}".encode("utf-8")).hexdigest()[:20]
    return f"fp:{digest}"


class MessageDedup:
    """In-memory TTL-кэш обработанных сообщений."""

    def __init__(self, ttl: float = 300.0, max_entries: int = 5000) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._seen: dict[str, float] = {}

    def _key(self, chat_id: str, anchor: str) -> str:
        return f"{chat_id}:{anchor}"

    def _purge(self, now: float) -> None:
        if len(self._seen) <= self._max:
            return
        stale = [k for k, ts in self._seen.items() if now - ts > self._ttl]
        for k in stale:
            self._seen.pop(k, None)
        if len(self._seen) > self._max:
            oldest = sorted(self._seen.items(), key=lambda x: x[1])[: len(self._seen) - self._max]
            for k, _ in oldest:
                self._seen.pop(k, None)

    def was_seen(self, chat_id: str, anchor: str) -> bool:
        if not chat_id or not anchor:
            return False
        now = time.time()
        ts = self._seen.get(self._key(chat_id, anchor))
        if ts is None:
            return False
        if now - ts > self._ttl:
            self._seen.pop(self._key(chat_id, anchor), None)
            return False
        return True

    def mark(self, chat_id: str, anchor: str) -> None:
        if not chat_id or not anchor:
            return
        now = time.time()
        self._seen[self._key(chat_id, anchor)] = now
        self._purge(now)
