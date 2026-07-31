#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram AI Userbot — monolithic single-file implementation.
Python 3.10+ | asyncio | Telethon | httpx[socks] | aiosqlite
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import sys
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional, Sequence, Union
from urllib.parse import quote

import aiosqlite
import httpx
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyDuplicatedError,
    FloodWaitError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import DeletePhotosRequest, UploadProfilePhotoRequest
from telethon.tl.types import InputPhoto, User

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# =============================================================================
# 1. IMPORTS, PATHS, CONSTANTS, REGEX PATTERNS, DEFAULT PROMPTS
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SESSION_DIR = BASE_DIR / "sessions"
DB_PATH = DATA_DIR / "userbot.db"
ERROR_LOG_FILE = BASE_DIR / "userbot_errors.log"
AVATAR_CACHE_DIR = DATA_DIR / "avatars"

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
RECONNECT_BASE_DELAY = 3.0
RECONNECT_MAX_DELAY = 120.0
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
IMAGEN_MODEL = "imagen-3.0-generate-002"
GEMINI_TEST_PROMPT = "ответь одним словом: ок"
MAX_GEMINI_RETRIES = 6
BASE_BACKOFF_SECONDS = 1.5
PROXY_ROTATION_INTERVAL = 180.0
PROXY_HEALTH_INTERVAL = 300.0
CONTEXT_MIN_MESSAGES = 20
CONTEXT_MAX_MESSAGES = 30
DEFAULT_CONTEXT_LIMIT = 25

DEFAULT_SYSTEM_PROMPT = (
    "ты обычный человек в чате, не бот и не ассистент. "
    "пиши только строчными буквами, коротко и по делу. "
    "используй сленг, разговорные обороты, иногда лёгкие опечатки — как в реальной переписке. "
    "если уместно — мат, но без перебора. "
    "никогда не говори что ты ии, бот или ассистент. "
    "никогда не используй фразы вроде «как я могу помочь», «я искусственный интеллект», "
    "«рад помочь», «обращайся если что». "
    "отвечай в тоне собеседников и подстраивайся под контекст последних сообщений."
)

SETUP_PROMPT = (
    "привет. это первичная настройка юзербота.\n\n"
    "отправь gemini api key и прокси в формате:\n"
    "`ключ|ip:port:user:pass`\n\n"
    "или двумя строками:\n"
    "1) api key\n"
    "2) ip:port:user:pass\n\n"
    "команды:\n"
    "/parse_proxy — подтянуть публичные socks5 и проверить\n"
    "/help — список всех команд\n\n"
    "поддерживаются socks5 и http прокси."
)

HELP_TEXT = (
    "команды (только в избранном):\n\n"
    "/help — эта справка\n"
    "/status — статус бота, gemini, прокси\n"
    "/logs — последние ошибки\n"
    "/stats — статистика сообщений\n"
    "/set_prompt <текст> — системный промпт\n"
    "/set_context <число> — лимит контекста (20-30)\n"
    "/clear_context [chat_id] — очистить контекст\n"
    "/blacklist_add <chat_id> — добавить чат в blacklist\n"
    "/blacklist_rm <chat_id> — убрать из blacklist\n"
    "/trusted_add <user_id> — доверенный пользователь\n"
    "/trusted_rm <user_id> — убрать доверенного\n"
    "/autoreply_off [chat_id] — выключить автоответ\n"
    "/autoreply_on [chat_id] — включить автоответ\n"
    "/pause — пауза автоответов везде\n"
    "/resume — снять паузу\n"
    "/parse_proxy — парсинг публичных прокси\n"
    "/proxy_list — список прокси\n"
    "/reset_key — сброс api key (повторная настройка)\n"
    "/restore_profile — восстановить профиль из backup\n"
    "/chats — активные чаты с контекстом\n"
)

CREDENTIALS_RE = re.compile(
    r"^(?P<api_key>AIza[0-9A-Za-z_\-]{20,})\s*[\|,]\s*(?P<proxy>.+)$",
    re.DOTALL,
)
PROXY_LINE_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks5)://)?"
    r"(?:(?P<user>[^:@]+):(?P<password>[^@]+)@)?"
    r"(?P<host>[\w.\-]+):(?P<port>\d+)$",
    re.IGNORECASE,
)
PROFILE_COMMAND_RE = re.compile(
    r"(?:сделай\s+аватар|поставь\s+аватар|аватарк).*?(?:с|из|на\s+)?(?P<topic>.+)",
    re.IGNORECASE,
)
NICK_COMMAND_RE = re.compile(
    r"(?:поставь\s+ник|смени\s+ник|ник)\s+['\"«](?P<nick>[^'\"»]+)['\"»]",
    re.IGNORECASE,
)
BIO_COMMAND_RE = re.compile(
    r"(?:поставь\s+био|смени\s+био|био)\s+['\"«](?P<bio>[^'\"»]+)['\"»]",
    re.IGNORECASE,
)

AI_PHRASES_BLOCKLIST = (
    "как я могу помочь", "я искусственный интеллект", "я языковая модель",
    "я ai", "я бот", "рад помочь", "обращайся если что", "чем могу помочь",
    "as an ai", "language model", "i'm an ai", "i am an ai",
)

FREE_PROXY_SOURCES = (
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
)

logger = logging.getLogger("userbot")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def configure_logging(level: int = logging.INFO) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    fmt = logging.Formatter(LOG_FORMAT)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(DATA_DIR / "userbot.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)


class ProxyFormatError(ValueError):
    pass


def parse_proxy_string(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise ProxyFormatError("Пустая строка прокси")
    if "://" not in text and text.count(":") == 3:
        host, port, user, password = text.split(":", 3)
        return f"socks5://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    match = PROXY_LINE_RE.match(text)
    if not match:
        raise ProxyFormatError(
            "Неверный формат. Используй ip:port:user:pass или socks5://user:pass@ip:port"
        )
    scheme = (match.group("scheme") or "socks5").lower()
    host, port = match.group("host"), match.group("port")
    user, password = match.group("user"), match.group("password")
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user and password else ""
    return f"{scheme}://{auth}{host}:{port}"


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklisted_chats (
    chat_id   INTEGER PRIMARY KEY,
    added_at  TEXT NOT NULL,
    reason    TEXT
);

CREATE TABLE IF NOT EXISTS whitelisted_chats (
    chat_id   INTEGER PRIMARY KEY,
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trusted_users (
    user_id   INTEGER PRIMARY KEY,
    added_at  TEXT NOT NULL,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id          INTEGER PRIMARY KEY,
    autoreply        INTEGER NOT NULL DEFAULT 1,
    custom_prompt    TEXT,
    context_limit    INTEGER,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    error_type  TEXT NOT NULL,
    message     TEXT NOT NULL,
    traceback   TEXT,
    context     TEXT
);

CREATE TABLE IF NOT EXISTS profile_backup (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    first_name TEXT,
    last_name  TEXT,
    about      TEXT,
    photo_id   TEXT,
    saved_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proxies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_url   TEXT NOT NULL UNIQUE,
    is_active   INTEGER NOT NULL DEFAULT 0,
    last_check  TEXT,
    latency_ms  REAL,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS chat_context (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER,
    role       TEXT NOT NULL,
    author     TEXT,
    content    TEXT NOT NULL,
    timestamp  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stats (
    key        TEXT PRIMARY KEY,
    value      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_error_logs_ts ON error_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_chat_context_chat ON chat_context(chat_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_proxies_active ON proxies(is_active, fail_count);
CREATE INDEX IF NOT EXISTS idx_chat_settings ON chat_settings(chat_id);
"""


@dataclass(frozen=True)
class ProxyRecord:
    id: int
    proxy_url: str
    is_active: bool
    last_check: Optional[str]
    latency_ms: Optional[float]
    fail_count: int
    source: str


@dataclass(frozen=True)
class ErrorRecord:
    id: int
    timestamp: str
    error_type: str
    message: str
    traceback: Optional[str]
    context: Optional[str]


@dataclass
class ChatStyleProfile:
    avg_length: float = 0.0
    lowercase_ratio: float = 1.0
    profanity_level: float = 0.0
    slang_density: float = 0.0
    emoji_density: float = 0.0
    punctuation_sparse: bool = True
    uses_english_mix: bool = False
    sample_phrases: list[str] = field(default_factory=list)


# =============================================================================
# 2. DATABASE CLASS
# =============================================================================

class Database:
    """Async SQLite wrapper with full schema and typed helpers."""

    DEFAULT_CONFIG = {
        "initialized": "0",
        "gemini_api_key": "",
        "active_proxy_id": "",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "owner_user_id": "",
        "context_message_limit": str(DEFAULT_CONTEXT_LIMIT),
        "global_pause": "0",
        "autoreply_global": "1",
    }

    STAT_KEYS = (
        "messages_received", "messages_sent", "ai_replies",
        "errors_total", "proxy_rotations", "flood_waits",
    )

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        await self._ensure_defaults()
        logger.info("Database connected: %s", self.db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        async with self._lock:
            try:
                yield self._conn
                await self._conn.commit()
            except Exception:
                await self._conn.rollback()
                raise

    async def _ensure_defaults(self) -> None:
        assert self._conn is not None
        for key, value in self.DEFAULT_CONFIG.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, value)
            )
        for stat_key in self.STAT_KEYS:
            await self._conn.execute(
                "INSERT OR IGNORE INTO stats (key, value, updated_at) VALUES (?, 0, ?)",
                (stat_key, utc_now()),
            )
        await self._conn.commit()

    async def get_config(self, key: str, default: str = "") -> str:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_config(self, key: str, value: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    async def is_initialized(self) -> bool:
        return await self.get_config("initialized") == "1"

    async def mark_initialized(self) -> None:
        await self.set_config("initialized", "1")

    async def reset_initialization(self) -> None:
        await self.set_config("initialized", "0")
        await self.set_config("gemini_api_key", "")

    async def get_system_prompt(self) -> str:
        prompt = await self.get_config("system_prompt", DEFAULT_SYSTEM_PROMPT)
        return prompt or DEFAULT_SYSTEM_PROMPT

    async def set_system_prompt(self, prompt: str) -> None:
        await self.set_config("system_prompt", prompt.strip())

    async def get_gemini_api_key(self) -> str:
        return await self.get_config("gemini_api_key")

    async def set_gemini_api_key(self, api_key: str) -> None:
        await self.set_config("gemini_api_key", api_key.strip())

    async def get_owner_user_id(self) -> Optional[int]:
        raw = await self.get_config("owner_user_id")
        return int(raw) if raw.isdigit() else None

    async def set_owner_user_id(self, user_id: int) -> None:
        await self.set_config("owner_user_id", str(user_id))

    async def get_context_limit(self) -> int:
        raw = await self.get_config("context_message_limit", str(DEFAULT_CONTEXT_LIMIT))
        try:
            val = int(raw)
        except ValueError:
            val = DEFAULT_CONTEXT_LIMIT
        return max(CONTEXT_MIN_MESSAGES, min(CONTEXT_MAX_MESSAGES, val))

    async def set_context_limit(self, limit: int) -> None:
        limit = max(CONTEXT_MIN_MESSAGES, min(CONTEXT_MAX_MESSAGES, limit))
        await self.set_config("context_message_limit", str(limit))

    async def is_globally_paused(self) -> bool:
        return await self.get_config("global_pause") == "1"

    async def set_global_pause(self, paused: bool) -> None:
        await self.set_config("global_pause", "1" if paused else "0")

    async def get_active_proxy_id(self) -> Optional[int]:
        raw = await self.get_config("active_proxy_id")
        return int(raw) if raw.isdigit() else None

    async def set_active_proxy_id(self, proxy_id: Optional[int]) -> None:
        await self.set_config("active_proxy_id", str(proxy_id or ""))

    async def increment_stat(self, key: str, amount: int = 1) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT INTO stats (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = value + ?, updated_at = ?",
            (key, amount, utc_now(), amount, utc_now()),
        )
        await self._conn.commit()

    async def get_stat(self, key: str) -> int:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT value FROM stats WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["value"]) if row else 0

    async def get_all_stats(self) -> dict[str, int]:
        assert self._conn is not None
        async with self._conn.execute("SELECT key, value FROM stats") as cursor:
            rows = await cursor.fetchall()
        return {row["key"]: int(row["value"]) for row in rows}

    async def add_proxy(self, proxy_url: str, source: str = "manual") -> int:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO proxies (proxy_url, source) VALUES (?, ?)",
            (proxy_url, source),
        )
        await self._conn.commit()
        if cursor.lastrowid:
            return cursor.lastrowid
        async with self._conn.execute(
            "SELECT id FROM proxies WHERE proxy_url = ?", (proxy_url,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["id"])

    async def list_proxies(self) -> list[ProxyRecord]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM proxies ORDER BY is_active DESC, fail_count ASC, id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_proxy(row) for row in rows]

    async def get_proxy(self, proxy_id: int) -> Optional[ProxyRecord]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_proxy(row) if row else None

    async def get_active_proxy(self) -> Optional[ProxyRecord]:
        proxy_id = await self.get_active_proxy_id()
        if proxy_id is None:
            return None
        return await self.get_proxy(proxy_id)

    async def set_proxy_active(self, proxy_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute("UPDATE proxies SET is_active = 0")
        await self._conn.execute(
            "UPDATE proxies SET is_active = 1, fail_count = 0 WHERE id = ?",
            (proxy_id,),
        )
        await self._conn.commit()
        await self.set_active_proxy_id(proxy_id)

    async def update_proxy_health(
        self, proxy_id: int, *, latency_ms: Optional[float] = None, failed: bool = False
    ) -> None:
        assert self._conn is not None
        now = utc_now()
        if failed:
            await self._conn.execute(
                "UPDATE proxies SET fail_count = fail_count + 1, last_check = ?, latency_ms = NULL WHERE id = ?",
                (now, proxy_id),
            )
        else:
            await self._conn.execute(
                "UPDATE proxies SET fail_count = 0, last_check = ?, latency_ms = ? WHERE id = ?",
                (now, latency_ms, proxy_id),
            )
        await self._conn.commit()

    async def deactivate_proxy(self, proxy_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute("UPDATE proxies SET is_active = 0 WHERE id = ?", (proxy_id,))
        await self._conn.commit()
        if await self.get_active_proxy_id() == proxy_id:
            await self.set_active_proxy_id(None)

    async def delete_proxy(self, proxy_id: int) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def blacklist_add(self, chat_id: int, reason: str = "") -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR IGNORE INTO blacklisted_chats (chat_id, added_at, reason) VALUES (?, ?, ?)",
            (chat_id, utc_now(), reason),
        )
        await self._conn.commit()

    async def blacklist_remove(self, chat_id: int) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM blacklisted_chats WHERE chat_id = ?", (chat_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def is_blacklisted(self, chat_id: int) -> bool:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM blacklisted_chats WHERE chat_id = ? LIMIT 1", (chat_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def list_blacklisted(self) -> list[int]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT chat_id FROM blacklisted_chats ORDER BY chat_id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [int(row["chat_id"]) for row in rows]

    async def whitelist_add(self, chat_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR IGNORE INTO whitelisted_chats (chat_id, added_at) VALUES (?, ?)",
            (chat_id, utc_now()),
        )
        await self._conn.commit()

    async def whitelist_remove(self, chat_id: int) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM whitelisted_chats WHERE chat_id = ?", (chat_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def is_whitelisted(self, chat_id: int) -> bool:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM whitelisted_chats WHERE chat_id = ? LIMIT 1", (chat_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def trusted_add(self, user_id: int, note: str = "") -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR IGNORE INTO trusted_users (user_id, added_at, note) VALUES (?, ?, ?)",
            (user_id, utc_now(), note),
        )
        await self._conn.commit()

    async def trusted_remove(self, user_id: int) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "DELETE FROM trusted_users WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def is_trusted(self, user_id: int) -> bool:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT 1 FROM trusted_users WHERE user_id = ? LIMIT 1", (user_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def list_trusted(self) -> list[int]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT user_id FROM trusted_users ORDER BY user_id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [int(row["user_id"]) for row in rows]

    async def get_chat_settings(self, chat_id: int) -> dict[str, Any]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
        defaults = {
            "autoreply": True,
            "custom_prompt": "",
            "context_limit": None,
            "cooldown_sec": "8",
            "group_reply_chance": "0.35",
        }
        if not row:
            return defaults
        return {
            "autoreply": bool(row["autoreply"]),
            "custom_prompt": row["custom_prompt"] or "",
            "context_limit": row["context_limit"],
            "cooldown_sec": defaults["cooldown_sec"],
            "group_reply_chance": defaults["group_reply_chance"],
        }

    async def set_chat_autoreply(self, chat_id: int, enabled: bool) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO chat_settings (chat_id, autoreply, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET autoreply = ?, updated_at = ?
            """,
            (chat_id, int(enabled), utc_now(), int(enabled), utc_now()),
        )
        await self._conn.commit()

    async def is_autoreply_enabled(self, chat_id: int) -> bool:
        if await self.is_globally_paused():
            return False
        settings = await self.get_chat_settings(chat_id)
        global_on = await self.get_config("autoreply_global", "1") == "1"
        return global_on and settings["autoreply"]

    async def save_profile_backup(
        self, first_name: Optional[str], last_name: Optional[str],
        about: Optional[str], photo_id: Optional[str] = None,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO profile_backup (id, first_name, last_name, about, photo_id, saved_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name, last_name = excluded.last_name,
                about = excluded.about, photo_id = excluded.photo_id, saved_at = excluded.saved_at
            """,
            (first_name, last_name, about, photo_id, utc_now()),
        )
        await self._conn.commit()

    async def get_profile_backup(self) -> Optional[dict[str, Optional[str]]]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT first_name, last_name, about, photo_id FROM profile_backup WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "first_name": row["first_name"], "last_name": row["last_name"],
            "about": row["about"], "photo_id": row["photo_id"],
        }

    async def append_chat_message(
        self, chat_id: int, *, role: str, content: str,
        message_id: Optional[int] = None, author: Optional[str] = None,
    ) -> None:
        assert self._conn is not None
        settings = await self.get_chat_settings(chat_id)
        limit = settings.get("context_limit") or await self.get_context_limit()
        await self._conn.execute(
            "INSERT INTO chat_context (chat_id, message_id, role, author, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, role, author, content, utc_now()),
        )
        await self._conn.execute(
            """
            DELETE FROM chat_context WHERE chat_id = ? AND id NOT IN (
                SELECT id FROM chat_context WHERE chat_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (chat_id, chat_id, limit),
        )
        await self._conn.commit()

    async def get_chat_context(self, chat_id: int, limit: Optional[int] = None) -> list[dict[str, str]]:
        assert self._conn is not None
        if limit is None:
            limit = await self.get_context_limit()
        async with self._conn.execute(
            "SELECT role, author, content, timestamp FROM chat_context "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        rows = list(reversed(rows))
        return [
            {"role": row["role"], "author": row["author"] or "",
             "content": row["content"], "timestamp": row["timestamp"]}
            for row in rows
        ]

    async def clear_chat_context(self, chat_id: Optional[int] = None) -> int:
        assert self._conn is not None
        if chat_id is not None:
            cursor = await self._conn.execute(
                "DELETE FROM chat_context WHERE chat_id = ?", (chat_id,)
            )
        else:
            cursor = await self._conn.execute("DELETE FROM chat_context")
        await self._conn.commit()
        return cursor.rowcount

    async def list_active_chats(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT chat_id, COUNT(*) as msg_count, MAX(timestamp) as last_msg
            FROM chat_context GROUP BY chat_id ORDER BY last_msg DESC LIMIT 50
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {"chat_id": row["chat_id"], "msg_count": row["msg_count"], "last_msg": row["last_msg"]}
            for row in rows
        ]

    async def log_error(
        self, exc: BaseException, *, error_type: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> int:
        assert self._conn is not None
        ts = utc_now()
        etype = error_type or type(exc).__name__
        message = str(exc) or repr(exc)
        tb = traceback.format_exc()
        ctx_json = json.dumps(context, ensure_ascii=False) if context else None
        cursor = await self._conn.execute(
            "INSERT INTO error_logs (timestamp, error_type, message, traceback, context) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, etype, message, tb, ctx_json),
        )
        await self._conn.commit()
        await self.increment_stat("errors_total")
        error_id = int(cursor.lastrowid)
        line = f"[{ts}] {etype}: {message}\ncontext={ctx_json or '{}'}\n{tb}\n{'-' * 80}\n"
        ERROR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with ERROR_LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as write_exc:
            logger.error("Failed to write error log file: %s", write_exc)
        logger.error("%s: %s", etype, message, exc_info=exc)
        return error_id

    async def get_recent_errors(self, limit: int = 50) -> list[ErrorRecord]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT id, timestamp, error_type, message, traceback, context "
            "FROM error_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ErrorRecord(
                id=row["id"], timestamp=row["timestamp"], error_type=row["error_type"],
                message=row["message"], traceback=row["traceback"], context=row["context"],
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_proxy(row: aiosqlite.Row) -> ProxyRecord:
        return ProxyRecord(
            id=int(row["id"]), proxy_url=row["proxy_url"],
            is_active=bool(row["is_active"]), last_check=row["last_check"],
            latency_ms=row["latency_ms"], fail_count=int(row["fail_count"]),
            source=row["source"],
        )



# =============================================================================
# 3. PROXYPARSER + GEMINIENGINE
# =============================================================================

@dataclass
class GeminiHealth:
    ok: bool
    model: str
    latency_ms: float
    message: str


@dataclass
class ProxyHealth:
    ok: bool
    proxy_id: Optional[int]
    proxy_url: Optional[str]
    latency_ms: Optional[float]
    message: str


class ProxyParser:
    """Fetches and normalizes public SOCKS5/HTTP proxies from multiple sources."""

    def __init__(self, timeout: float = 25.0) -> None:
        self.timeout = timeout

    async def fetch_public_proxies(self, limit: int = 60) -> list[str]:
        proxies: list[str] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for source in FREE_PROXY_SOURCES:
                try:
                    response = await client.get(source)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning("Не удалось загрузить прокси из %s: %s", source, exc)
                    continue
                for line in response.text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    for candidate in (line, f"socks5://{line}"):
                        try:
                            normalized = parse_proxy_string(candidate)
                        except ProxyFormatError:
                            continue
                        if normalized in seen:
                            continue
                        seen.add(normalized)
                        proxies.append(normalized)
                        if len(proxies) >= limit:
                            return proxies
        return proxies

    async def fetch_with_metadata(self, limit: int = 60) -> list[dict[str, str]]:
        urls = await self.fetch_public_proxies(limit)
        return [{"url": u, "source": "public", "scheme": u.split("://")[0]} for u in urls]


class GeminiEngine:
    """Async Gemini client with proxy support, validation, rotation, and backoff."""

    def __init__(
        self, db: Database, *, model: str = DEFAULT_GEMINI_MODEL, request_timeout: float = 50.0,
    ) -> None:
        self.db = db
        self.model = model
        self.request_timeout = request_timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._current_proxy_url: Optional[str] = None
        self._rotation_task: Optional[asyncio.Task[None]] = None
        self._health_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._proxy_parser = ProxyParser()
        self.last_gemini_health: Optional[GeminiHealth] = None
        self.last_proxy_health: Optional[ProxyHealth] = None
        self._request_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._rebuild_client()
        self._stop_event.clear()
        if self._rotation_task is None or self._rotation_task.done():
            self._rotation_task = asyncio.create_task(self._rotation_loop(), name="proxy-rotation")
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_loop(), name="proxy-health")
        logger.info("GeminiEngine started")

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._rotation_task, self._health_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.close()
        logger.info("GeminiEngine stopped")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._current_proxy_url = None

    async def _rebuild_client(self, proxy_url: Optional[str] = None) -> None:
        if proxy_url is None:
            active = await self.db.get_active_proxy()
            proxy_url = active.proxy_url if active else None
        if self._client is not None and proxy_url == self._current_proxy_url:
            return
        if self._client is not None:
            await self._client.aclose()
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.request_timeout),
            "follow_redirects": True,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        self._client = httpx.AsyncClient(**kwargs)
        self._current_proxy_url = proxy_url
        logger.debug("HTTP client rebuilt, proxy=%s", proxy_url or "direct")

    async def register_proxy(self, raw_proxy: str, *, source: str = "manual") -> ProxyRecord:
        proxy_url = parse_proxy_string(raw_proxy)
        proxy_id = await self.db.add_proxy(proxy_url, source=source)
        record = await self.db.get_proxy(proxy_id)
        if record is None:
            raise RuntimeError(f"Proxy {proxy_id} not found after insert")
        return record

    async def validate_and_activate(
        self, api_key: str, raw_proxy: str,
    ) -> tuple[GeminiHealth, ProxyHealth]:
        proxy = await self.register_proxy(raw_proxy, source="manual")
        health = await self._test_proxy_with_gemini(api_key, proxy)
        if not health.ok:
            await self.db.update_proxy_health(proxy.id, failed=True)
            raise RuntimeError(health.message)
        await self.db.set_proxy_active(proxy.id)
        await self.db.set_gemini_api_key(api_key)
        await self._rebuild_client(proxy.proxy_url)
        gemini_health = await self.test_gemini(api_key)
        if not gemini_health.ok:
            raise RuntimeError(gemini_health.message)
        return gemini_health, health

    async def test_gemini(self, api_key: Optional[str] = None) -> GeminiHealth:
        api_key = api_key or await self.db.get_gemini_api_key()
        if not api_key:
            health = GeminiHealth(False, self.model, 0.0, "API key не задан")
            self.last_gemini_health = health
            return health
        started = time.perf_counter()
        try:
            text = await self.generate_text(
                api_key=api_key, system_prompt="ты тестовый бот",
                messages=[{"role": "user", "content": GEMINI_TEST_PROMPT}],
                max_output_tokens=16, temperature=0.2,
            )
            latency = (time.perf_counter() - started) * 1000
            health = GeminiHealth(bool(text.strip()), self.model, latency, text.strip() or "empty")
        except Exception as exc:
            await self.db.log_error(exc, error_type="GeminiHealthCheck")
            latency = (time.perf_counter() - started) * 1000
            health = GeminiHealth(False, self.model, latency, str(exc))
        self.last_gemini_health = health
        return health

    async def _test_proxy_with_gemini(self, api_key: str, proxy: ProxyRecord) -> ProxyHealth:
        started = time.perf_counter()
        temp_client: Optional[httpx.AsyncClient] = None
        try:
            temp_client = httpx.AsyncClient(
                proxy=proxy.proxy_url,
                timeout=httpx.Timeout(self.request_timeout),
                follow_redirects=True,
            )
            url = f"{GEMINI_BASE_URL}/models/{self.model}:generateContent"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": GEMINI_TEST_PROMPT}]}],
                "generationConfig": {"maxOutputTokens": 8, "temperature": 0.2},
            }
            response = await temp_client.post(url, params={"key": api_key}, json=payload)
            latency = (time.perf_counter() - started) * 1000
            if response.status_code == 429:
                return ProxyHealth(False, proxy.id, proxy.proxy_url, latency, "Gemini rate limit (429)")
            if response.status_code >= 400:
                return ProxyHealth(
                    False, proxy.id, proxy.proxy_url, latency,
                    f"HTTP {response.status_code}: {response.text[:200]}",
                )
            data = response.json()
            if not data.get("candidates"):
                return ProxyHealth(False, proxy.id, proxy.proxy_url, latency, "Gemini вернул пустой ответ")
            await self.db.update_proxy_health(proxy.id, latency_ms=latency, failed=False)
            health = ProxyHealth(True, proxy.id, proxy.proxy_url, latency, "ok")
            self.last_proxy_health = health
            return health
        except Exception as exc:
            latency = (time.perf_counter() - started) * 1000
            await self.db.log_error(
                exc, error_type="ProxyValidation",
                context={"proxy_id": proxy.id, "proxy_url": proxy.proxy_url},
            )
            health = ProxyHealth(False, proxy.id, proxy.proxy_url, latency, str(exc))
            self.last_proxy_health = health
            return health
        finally:
            if temp_client is not None:
                await temp_client.aclose()

    async def generate_reply(
        self, *, system_prompt: str, context_messages: list[dict[str, str]], user_message: str,
        style_hint: str = "",
    ) -> str:
        api_key = await self.db.get_gemini_api_key()
        if not api_key:
            raise RuntimeError("Gemini API key не настроен")
        messages = list(context_messages)
        messages.append({"role": "user", "content": user_message, "author": "собеседник"})
        full_prompt = system_prompt
        if style_hint:
            full_prompt = f"{system_prompt}\n\nстиль переписки:\n{style_hint}"
        async with self._request_lock:
            text = await self.generate_text(
                api_key=api_key, system_prompt=full_prompt, messages=messages,
            )
        return text.strip()

    async def generate_text(
        self, *, api_key: str, system_prompt: str, messages: list[dict[str, str]],
        max_output_tokens: int = 512, temperature: float = 0.95,
    ) -> str:
        await self._rebuild_client()
        if self._client is None:
            raise RuntimeError("HTTP client is not initialized")
        contents = []
        for item in messages:
            role = item.get("role", "user")
            author = item.get("author") or ("я" if role == "assistant" else "собеседник")
            text = item.get("content", "").strip()
            if not text:
                continue
            gemini_role = "model" if role == "assistant" else "user"
            prefix = f"{author}: " if gemini_role == "user" else ""
            contents.append({"role": gemini_role, "parts": [{"text": f"{prefix}{text}"}]})
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
        }
        url = f"{GEMINI_BASE_URL}/models/{self.model}:generateContent"
        last_error: Optional[Exception] = None
        for attempt in range(MAX_GEMINI_RETRIES):
            try:
                response = await self._client.post(url, params={"key": api_key}, json=payload)
                if response.status_code == 429:
                    delay = BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning("Gemini 429, backoff %.1fs (attempt %s)", delay, attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                return self._extract_text(response.json())
            except httpx.TimeoutException as exc:
                last_error = exc
                await self.db.log_error(exc, error_type="GeminiTimeout")
                await self._handle_proxy_failure()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                await self.db.log_error(exc, error_type="GeminiHTTPError")
                if exc.response.status_code in {401, 403}:
                    raise
                if exc.response.status_code >= 500:
                    await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
                    continue
                raise
            except Exception as exc:
                last_error = exc
                await self.db.log_error(exc, error_type="GeminiRequest")
                await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
        raise RuntimeError(f"Gemini request failed after retries: {last_error}")

    async def generate_image(self, prompt: str, *, api_key: Optional[str] = None) -> Optional[bytes]:
        api_key = api_key or await self.db.get_gemini_api_key()
        if not api_key:
            return None
        await self._rebuild_client()
        if self._client is None:
            return None
        url = f"{GEMINI_BASE_URL}/models/{IMAGEN_MODEL}:predict"
        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": "1:1"},
        }
        try:
            response = await self._client.post(url, params={"key": api_key}, json=payload)
            if response.status_code == 429:
                await asyncio.sleep(BASE_BACKOFF_SECONDS * 2)
                response = await self._client.post(url, params={"key": api_key}, json=payload)
            response.raise_for_status()
            data = response.json()
            predictions = data.get("predictions") or []
            if not predictions:
                return None
            b64 = predictions[0].get("bytesBase64Encoded") or predictions[0].get("image", {}).get("bytesBase64Encoded")
            if b64:
                return base64.b64decode(b64)
        except Exception as exc:
            await self.db.log_error(exc, error_type="ImagenGenerate", context={"prompt": prompt[:100]})
        return None

    async def parse_and_store_public_proxies(self, limit: int = 40) -> list[ProxyRecord]:
        raw_list = await self._proxy_parser.fetch_public_proxies(limit=limit)
        stored: list[ProxyRecord] = []
        for raw in raw_list:
            try:
                proxy_id = await self.db.add_proxy(raw, source="public")
                record = await self.db.get_proxy(proxy_id)
                if record:
                    stored.append(record)
            except Exception as exc:
                await self.db.log_error(exc, error_type="ProxyStore")
        return stored

    async def ensure_working_proxy(self, api_key: Optional[str] = None) -> Optional[ProxyRecord]:
        api_key = api_key or await self.db.get_gemini_api_key()
        if not api_key:
            return None
        active = await self.db.get_active_proxy()
        if active:
            health = await self._test_proxy_with_gemini(api_key, active)
            if health.ok:
                await self._rebuild_client(active.proxy_url)
                return active
            await self.db.deactivate_proxy(active.id)
        pool = [p for p in await self.db.list_proxies() if not p.is_active]
        found = await self._find_working_parallel(api_key, pool, limit=1)
        if found:
            proxy = found[0]
            await self.db.set_proxy_active(proxy.id)
            await self._rebuild_client(proxy.proxy_url)
            await self.db.increment_stat("proxy_rotations")
            return proxy
        parsed = await self.parse_and_store_public_proxies(limit=25)
        found = await self._find_working_parallel(api_key, parsed, limit=1)
        if found:
            proxy = found[0]
            await self.db.set_proxy_active(proxy.id)
            await self._rebuild_client(proxy.proxy_url)
            await self.db.increment_stat("proxy_rotations")
            return proxy
        return None

    async def _find_working_parallel(
        self, api_key: str, proxies: list[ProxyRecord], *, limit: int = 1, workers: int = 10,
    ) -> list[ProxyRecord]:
        if not proxies:
            return []
        sem = asyncio.Semaphore(workers)
        found: list[ProxyRecord] = []
        lock = asyncio.Lock()

        async def probe(proxy: ProxyRecord) -> None:
            async with sem:
                async with lock:
                    if len(found) >= limit:
                        return
                health = await self._test_proxy_with_gemini(api_key, proxy)
                if health.ok:
                    async with lock:
                        if len(found) < limit:
                            found.append(proxy)

        await asyncio.gather(*(probe(p) for p in proxies), return_exceptions=True)
        return found

    async def _handle_proxy_failure(self) -> None:
        active = await self.db.get_active_proxy()
        if active:
            await self.db.update_proxy_health(active.id, failed=True)
            if active.fail_count >= 2:
                await self.db.deactivate_proxy(active.id)
        await self.ensure_working_proxy()

    async def _rotation_loop(self) -> None:
        logger.info("Proxy rotation loop started")
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=PROXY_ROTATION_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass
            api_key = await self.db.get_gemini_api_key()
            if not api_key:
                continue
            try:
                await self.ensure_working_proxy(api_key)
            except Exception as exc:
                await self.db.log_error(exc, error_type="ProxyRotation")

    async def _health_loop(self) -> None:
        logger.info("Proxy health loop started")
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=PROXY_HEALTH_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass
            api_key = await self.db.get_gemini_api_key()
            if not api_key:
                continue
            try:
                await self.test_gemini(api_key)
            except Exception as exc:
                await self.db.log_error(exc, error_type="GeminiHealthLoop")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini response has no candidates")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        texts = [part.get("text", "") for part in parts if part.get("text")]
        if not texts:
            raise RuntimeError("Gemini response has no text parts")
        return "".join(texts)



# =============================================================================
# 4. CHATSTYLEANALYZER + HUMANIZER
# =============================================================================

SLANG_REPLACEMENTS: dict[str, tuple[str, ...]] = {
    "хорошо": ("норм", "ок", "окей", "заебись", "нормас", "гуд"),
    "да": ("ага", "угу", "да", "ну да", "ye", "yes"),
    "нет": ("не", "неа", "nah", "не-а", "no"),
    "понятно": ("пон", "понял", "ясно", "поня", "ok"),
    "спасибо": ("спс", "благодарю", "сенкс", "спасиб", "thx"),
    "пожалуйста": ("пж", "плиз", "пожалста", "plz"),
    "сейчас": ("щас", "ща", "сейчас", "now"),
    "ничего": ("ниче", "ничего", "норм", "не парься"),
    "что": ("че", "чё", "шо", "what"),
    "почему": ("поч", "зачем", "почему", "why"),
    "очень": ("оч", "жесть", "пиздец как", "very"),
    "конечно": ("конеч", "ну да", "ага", "sure"),
    "ладно": ("лан", "ок", "ну ок", "aight"),
    "наверное": ("наверн", "может", "хз", "maybe"),
    "хороший": ("норм", "топ", "огонь", "fire"),
    "плохой": ("хренов", "так себе", "говно", "trash"),
    "человек": ("чел", "челик", "тип", "dude"),
    "друг": ("бро", "братан", "кент", "bro"),
    "деньги": ("бабки", "кэш", "деньги", "cash"),
    "работа": ("работа", "работка", "job"),
    "проблема": ("проблема", "засада", "issue"),
}

PROFANITY_MARKERS: tuple[str, ...] = (
    "бля", "блять", "бляд", "хуй", "хуе", "пизд", "еба", "ёб", "сука", "нах",
    "fuck", "shit", "damn", "ass", "bitch",
)

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)

LATIN_WORD_RE = re.compile(r"[a-zA-Z]{2,}")

TYPO_SUBSTITUTIONS: dict[str, str] = {
    "а": "a", "о": "o", "е": "e", "и": "i", "у": "y",
    "р": "p", "с": "c", "х": "x", "к": "k", "в": "b",
    "т": "t", "н": "h", "м": "m", "л": "l",
}

FILLER_WORDS = ("ну", "типа", "короче", "блин", "вообще", "просто", "кста", "кстати", "lmk", "btw")

LAUGH_VARIANTS = ("ахах", "ахаха", "лол", "lol", "хах", "хаха", "ржу", "xd", "кек", "kek")


class ChatStyleAnalyzer:
    """Analyzes recent chat messages to derive a style profile for human-like replies."""

    def __init__(self) -> None:
        self._cache: dict[int, tuple[float, ChatStyleProfile]] = {}
        self._cache_ttl = 120.0

    def analyze(self, messages: Sequence[dict[str, str]], chat_id: int = 0) -> ChatStyleProfile:
        now = time.monotonic()
        cached = self._cache.get(chat_id)
        if cached and now - cached[0] < self._cache_ttl:
            return cached[1]
        user_messages = [
            m["content"] for m in messages
            if m.get("role") == "user" and m.get("content")
        ]
        if not user_messages:
            profile = ChatStyleProfile()
            self._cache[chat_id] = (now, profile)
            return profile
        total_chars = sum(len(m) for m in user_messages)
        avg_length = total_chars / len(user_messages)
        lower_count = sum(1 for m in user_messages if m == m.lower())
        lowercase_ratio = lower_count / len(user_messages)
        profanity_hits = sum(
            1 for m in user_messages
            for marker in PROFANITY_MARKERS if marker in m.lower()
        )
        profanity_level = min(profanity_hits / max(len(user_messages), 1), 1.0)
        slang_hits = 0
        for msg in user_messages:
            lower = msg.lower()
            for key, variants in SLANG_REPLACEMENTS.items():
                if key in lower or any(v in lower for v in variants):
                    slang_hits += 1
                    break
        slang_density = slang_hits / len(user_messages)
        emoji_count = sum(len(EMOJI_PATTERN.findall(m)) for m in user_messages)
        emoji_density = emoji_count / len(user_messages)
        punct_sparse = sum(1 for m in user_messages if not m.rstrip().endswith((".", "!", "?"))) / len(user_messages)
        english_mix = any(LATIN_WORD_RE.search(m) for m in user_messages)
        sample_phrases = user_messages[-5:]
        profile = ChatStyleProfile(
            avg_length=avg_length,
            lowercase_ratio=lowercase_ratio,
            profanity_level=profanity_level,
            slang_density=slang_density,
            emoji_density=emoji_density,
            punctuation_sparse=punct_sparse > 0.5,
            uses_english_mix=english_mix,
            sample_phrases=sample_phrases,
        )
        self._cache[chat_id] = (now, profile)
        return profile

    def build_style_hint(self, profile: ChatStyleProfile) -> str:
        lines = [
            f"средняя длина сообщений: {profile.avg_length:.0f} символов",
            f"строчные: {profile.lowercase_ratio:.0%}",
        ]
        if profile.profanity_level > 0.2:
            lines.append("собеседники матерятся — можно иногда, если уместно")
        if profile.slang_density > 0.3:
            lines.append("много сленга — используй разговорный стиль")
        if profile.emoji_density > 0.3:
            lines.append("иногда можно эмодзи, но не перебарщивай")
        if profile.uses_english_mix:
            lines.append("иногда мешай английские слова как в чате")
        if profile.punctuation_sparse:
            lines.append("минимум точек и заглавных букв")
        if profile.sample_phrases:
            lines.append("примеры сообщений:\n" + "\n".join(profile.sample_phrases[-3:]))
        return "\n".join(lines)


class Humanizer:
    """Post-processes AI output to sound human: lowercase, slang, typos, delays."""

    def __init__(self, analyzer: Optional[ChatStyleAnalyzer] = None) -> None:
        self.analyzer = analyzer or ChatStyleAnalyzer()
        self._rng = random.Random()

    def humanize(
        self, text: str, profile: Optional[ChatStyleProfile] = None, *, intensity: float = 0.7,
    ) -> str:
        if not text:
            return text
        result = text.strip()
        result = self._strip_ai_phrases(result)
        if profile is None:
            profile = ChatStyleProfile()
        if profile.lowercase_ratio > 0.5 or profile.avg_length < 80:
            result = result.lower()
        result = self._apply_slang(result, profile, intensity)
        if profile.profanity_level < 0.1:
            result = self._soften_profanity(result)
        if self._rng.random() < 0.15 * intensity:
            result = self._inject_typo(result)
        if profile.punctuation_sparse and self._rng.random() < 0.4:
            result = result.rstrip(".")
        if profile.emoji_density > 0.2 and self._rng.random() < 0.12:
            result = self._maybe_add_emoji(result)
        if len(result) > int(profile.avg_length * 2.5) and profile.avg_length > 0:
            result = self._truncate_natural(result, int(profile.avg_length * 2))
        return result.strip()

    def typing_delay_seconds(
        self, text: str, profile: Optional[ChatStyleProfile] = None,
        chars_per_second: float = 0.0,
    ) -> float:
        if not text:
            return 0.5
        if profile and profile.avg_length > 0:
            cps = max(12.0, min(22.0, 60.0 / max(profile.avg_length, 10)))
        else:
            cps = chars_per_second or 17.5
        base = len(text) / cps
        jitter = self._rng.uniform(-0.3, 0.8)
        thinking = self._rng.uniform(0.4, 1.5) if len(text) > 40 else self._rng.uniform(0.2, 0.8)
        delay = base + jitter + thinking
        return min(max(delay, 0.6), 15.0)

    def split_typing_chunks(self, text: str) -> list[str]:
        if len(text) <= 80:
            return [text]
        parts = re.split(r"([.!?…]\s+|\n+)", text)
        chunks: list[str] = []
        current = ""
        for part in parts:
            if len(current) + len(part) > 120 and current:
                chunks.append(current.strip())
                current = part
            else:
                current += part
        if current.strip():
            chunks.append(current.strip())
        return chunks or [text]

    @staticmethod
    def _strip_ai_phrases(text: str) -> str:
        lower = text.lower()
        for phrase in AI_PHRASES_BLOCKLIST:
            if phrase in lower:
                text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", text).strip()

    def _apply_slang(self, text: str, profile: ChatStyleProfile, intensity: float) -> str:
        if profile.slang_density < 0.2 and self._rng.random() > intensity * 0.3:
            return text
        merged = {**SLANG_REPLACEMENTS, **EXTENDED_SLANG_CORPUS}
        lower = text.lower()
        for word, variants in merged.items():
            if word in lower and self._rng.random() < 0.22 * intensity:
                replacement = self._rng.choice(variants)
                text = re.sub(
                    rf"\b{re.escape(word)}\b", replacement, text,
                    flags=re.IGNORECASE, count=1,
                )
        return text

    @staticmethod
    def _soften_profanity(text: str) -> str:
        replacements = {
            "блять": "блин", "бля": "блин", "хуй": "хрен", "пиздец": "капец",
            "ёб": "ё", "еба": "ё", "сука": "сук",
        }
        result = text
        for bad, mild in replacements.items():
            result = re.sub(bad, mild, result, flags=re.IGNORECASE)
        return result

    def _inject_typo(self, text: str) -> str:
        if len(text) < 4:
            return text
        words = text.split()
        if not words:
            return text
        idx = self._rng.randrange(len(words))
        word = words[idx]
        if len(word) < 3:
            return text
        char_idx = self._rng.randrange(1, len(word) - 1)
        char = word[char_idx].lower()
        if char in TYPO_SUBSTITUTIONS and self._rng.random() < 0.6:
            word = word[:char_idx] + TYPO_SUBSTITUTIONS[char] + word[char_idx + 1:]
        elif self._rng.random() < 0.3:
            word = word[:char_idx] + word[char_idx + 1:]
        else:
            word = word[:char_idx] + word[char_idx] + word[char_idx:]
        words[idx] = word
        return " ".join(words)

    def _maybe_add_emoji(self, text: str) -> str:
        emojis = ("😂", "💀", "👍", "🤔", "😭", "🔥", "✌️", "🫡", "💯", "🤷")
        if self._rng.random() < 0.5:
            return f"{text} {self._rng.choice(emojis)}"
        return f"{self._rng.choice(emojis)} {text}"

    @staticmethod
    def _truncate_natural(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        cut = text[:max_len]
        last_space = cut.rfind(" ")
        if last_space > max_len // 2:
            cut = cut[:last_space]
        return cut.rstrip(",.;: ") + ("..." if not cut.endswith("...") else "")




# =============================================================================
# 5. PROFILEMANAGER
# =============================================================================

class ProfileManager:
    """Manages Telegram profile changes: nick, bio, avatar with Gemini/PIL fallback."""

    AVATAR_SIZE = 512
    GRADIENT_PALETTES: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = [
        ((30, 60, 120), (180, 80, 200)),
        ((20, 80, 60), (200, 180, 40)),
        ((80, 20, 40), (240, 120, 80)),
        ((10, 30, 80), (60, 180, 220)),
        ((50, 50, 50), (180, 180, 180)),
        ((100, 40, 10), (255, 200, 50)),
        ((15, 50, 90), (90, 200, 150)),
        ((60, 10, 80), (200, 100, 180)),
    ]

    def __init__(self, client: TelegramClient, db: Database, ai: GeminiEngine) -> None:
        self.client = client
        self.db = db
        self.ai = ai
        self._rng = random.Random()

    async def backup_current_profile(self) -> None:
        try:
            me = await self.client.get_me()
            if me is None:
                return
            photo_id = None
            photos = await self.client.get_profile_photos("me", limit=1)
            if photos and photos[0]:
                photo_id = str(photos[0].id)
            await self.db.save_profile_backup(
                me.first_name, me.last_name, me.about, photo_id,
            )
        except Exception as exc:
            await self.db.log_error(exc, error_type="ProfileBackup")

    async def restore_profile(self) -> tuple[bool, str]:
        backup = await self.db.get_profile_backup()
        if not backup:
            return False, "backup профиля не найден"
        try:
            await self.client(UpdateProfileRequest(
                first_name=backup["first_name"] or "",
                last_name=backup["last_name"] or "",
                about=backup["about"] or "",
            ))
            return True, "профиль восстановлен"
        except RPCError as exc:
            await self.db.log_error(exc, error_type="RestoreProfile")
            return False, f"ошибка: {exc}"

    async def set_nickname(self, nick: str) -> tuple[bool, str]:
        nick = nick.strip()[:64]
        if not nick:
            return False, "пустой ник"
        try:
            await self.client(UpdateProfileRequest(first_name=nick))
            return True, "ник обновлён"
        except RPCError as exc:
            await self.db.log_error(exc, error_type="ProfileNick")
            return False, f"не смог: {exc}"

    async def set_bio(self, bio: str) -> tuple[bool, str]:
        bio = bio.strip()[:70]
        try:
            await self.client(UpdateProfileRequest(about=bio))
            return True, "био обновлено"
        except RPCError as exc:
            await self.db.log_error(exc, error_type="ProfileBio")
            return False, f"не смог: {exc}"

    async def set_avatar(self, topic: str) -> tuple[bool, str]:
        topic = topic.strip()
        if not topic:
            return False, "укажи тему для аватарки"
        image_bytes = await self.ai.generate_image(
            f"avatar profile picture, artistic, {topic}, square, no text, no watermark"
        )
        if not image_bytes and HAS_PIL:
            image_bytes = self._generate_pil_avatar(topic)
        if not image_bytes:
            return False, "не удалось сгенерировать аватарку"
        try:
            AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path = AVATAR_CACHE_DIR / f"avatar_{hashlib.md5(topic.encode()).hexdigest()[:12]}.jpg"
            cache_path.write_bytes(image_bytes)
            uploaded = await self.client.upload_file(str(cache_path))
            await self.client(UploadProfilePhotoRequest(file=uploaded))
            return True, "аватарка обновлена"
        except FloodWaitError as exc:
            await self.db.increment_stat("flood_waits")
            return False, f"flood wait {exc.seconds}s, попробуй позже"
        except RPCError as exc:
            await self.db.log_error(exc, error_type="ProfileAvatar")
            return False, f"ошибка загрузки: {exc}"

    def _generate_pil_avatar(self, topic: str) -> bytes:
        if not HAS_PIL:
            return b""
        palette = self._rng.choice(self.GRADIENT_PALETTES)
        img = Image.new("RGB", (self.AVATAR_SIZE, self.AVATAR_SIZE))
        draw = ImageDraw.Draw(img)
        for y in range(self.AVATAR_SIZE):
            ratio = y / self.AVATAR_SIZE
            r = int(palette[0][0] + (palette[1][0] - palette[0][0]) * ratio)
            g = int(palette[0][1] + (palette[1][1] - palette[0][1]) * ratio)
            b = int(palette[0][2] + (palette[1][2] - palette[0][2]) * ratio)
            draw.line([(0, y), (self.AVATAR_SIZE, y)], fill=(r, g, b))
        seed = hashlib.sha256(topic.encode()).hexdigest()
        cx = int(seed[0:2], 16) % (self.AVATAR_SIZE // 2) + self.AVATAR_SIZE // 4
        cy = int(seed[2:4], 16) % (self.AVATAR_SIZE // 2) + self.AVATAR_SIZE // 4
        radius = int(seed[4:6], 16) % 80 + 60
        accent = (
            int(seed[6:8], 16), int(seed[8:10], 16), int(seed[10:12], 16),
        )
        draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            fill=accent, outline=(255, 255, 255), width=3,
        )
        initials = "".join(w[0].upper() for w in topic.split()[:2] if w)[:2] or "?"
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 120)
        except OSError:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), initials, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(
            (cx - tw // 2, cy - th // 2), initials,
            fill=(255, 255, 255), font=font,
        )
        img = img.filter(ImageFilter.GaussianBlur(radius=0.5))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


# =============================================================================
# 6. COMMANDHANDLER
# =============================================================================

class CommandHandler:
    """Handles all admin commands in Saved Messages (Избранное)."""

    COMMANDS = frozenset({
        "/help", "/logs", "/status", "/set_prompt", "/blacklist_add", "/blacklist_rm",
        "/parse_proxy", "/pause", "/resume", "/stats", "/clear_context", "/set_context",
        "/trusted_add", "/trusted_rm", "/autoreply_off", "/autoreply_on",
        "/restore_profile", "/chats", "/proxy_list", "/reset_key",
    })

    def __init__(
        self, client: TelegramClient, db: Database, ai: GeminiEngine,
        profile_mgr: ProfileManager, started_at: datetime,
    ) -> None:
        self.client = client
        self.db = db
        self.ai = ai
        self.profile_mgr = profile_mgr
        self.started_at = started_at
        self.owner_user_id: Optional[int] = None

    async def set_owner(self, user_id: int) -> None:
        self.owner_user_id = user_id

    def is_admin_command(self, text: str) -> bool:
        cmd = text.split(maxsplit=1)[0].lower()
        return cmd in self.COMMANDS

    async def handle(self, event: events.NewMessage.Event, text: str) -> bool:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        handlers: dict[str, Callable] = {
            "/help": self._cmd_help,
            "/logs": self._cmd_logs,
            "/status": self._cmd_status,
            "/stats": self._cmd_stats,
            "/set_prompt": lambda e, a: self._cmd_set_prompt(e, a),
            "/set_context": lambda e, a: self._cmd_set_context(e, a),
            "/clear_context": lambda e, a: self._cmd_clear_context(e, a),
            "/blacklist_add": lambda e, a: self._cmd_blacklist_add(e, a),
            "/blacklist_rm": lambda e, a: self._cmd_blacklist_rm(e, a),
            "/trusted_add": lambda e, a: self._cmd_trusted_add(e, a),
            "/trusted_rm": lambda e, a: self._cmd_trusted_rm(e, a),
            "/autoreply_off": lambda e, a: self._cmd_autoreply(e, a, False),
            "/autoreply_on": lambda e, a: self._cmd_autoreply(e, a, True),
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/parse_proxy": self._cmd_parse_proxy,
            "/proxy_list": self._cmd_proxy_list,
            "/reset_key": self._cmd_reset_key,
            "/restore_profile": self._cmd_restore_profile,
            "/chats": self._cmd_chats,
        }
        handler = handlers.get(command)
        if handler is None:
            return False
        try:
            await handler(event, arg)
        except Exception as exc:
            await self.db.log_error(exc, error_type="AdminCommand", context={"command": command})
            await event.reply(f"ошибка команды: {exc}")
        return True

    async def _cmd_help(self, event: events.NewMessage.Event, _arg: str) -> None:
        await event.reply(HELP_TEXT)

    async def _cmd_logs(self, event: events.NewMessage.Event, _arg: str) -> None:
        errors = await self.db.get_recent_errors(limit=50)
        if not errors:
            await event.reply("логов ошибок пока нет")
            return
        lines = [format_error_for_user(item) for item in reversed(errors)]
        payload = "\n".join(lines)
        if len(payload) <= 3500:
            await event.reply(f"```\n{payload}\n```")
            return
        buffer = BytesIO(payload.encode("utf-8"))
        buffer.name = "recent_errors.txt"
        await event.reply("последние 50 ошибок:", file=buffer)
        if ERROR_LOG_FILE.exists():
            await event.reply("полный файл:", file=str(ERROR_LOG_FILE))

    async def _cmd_status(self, event: events.NewMessage.Event, _arg: str) -> None:
        uptime = datetime.now(timezone.utc) - self.started_at
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(rem, 60)
        gemini_health = await self.ai.test_gemini()
        active_proxy = await self.db.get_active_proxy()
        proxy_text = "нет"
        if active_proxy:
            proxy_text = (
                f"id={active_proxy.id} | fail={active_proxy.fail_count} | "
                f"latency={active_proxy.latency_ms or '?'} ms\n{active_proxy.proxy_url}"
            )
        paused = await self.db.is_globally_paused()
        text = (
            f"uptime: {hours}h {minutes}m {seconds}s\n"
            f"initialized: {await self.db.is_initialized()}\n"
            f"paused: {paused}\n"
            f"gemini: {'ok' if gemini_health.ok else 'fail'} "
            f"({gemini_health.latency_ms:.0f} ms) — {gemini_health.message}\n"
            f"proxy: {proxy_text}\n"
            f"blacklist: {len(await self.db.list_blacklisted())} chats\n"
            f"trusted: {len(await self.db.list_trusted())} users\n"
            f"context limit: {await self.db.get_context_limit()}"
        )
        await event.reply(text)

    async def _cmd_stats(self, event: events.NewMessage.Event, _arg: str) -> None:
        stats = await self.db.get_all_stats()
        lines = [f"{k}: {v}" for k, v in sorted(stats.items())]
        chats = await self.db.list_active_chats()
        lines.append(f"active chats: {len(chats)}")
        lines.append(f"proxies: {len(await self.db.list_proxies())}")
        await event.reply("статистика:\n" + "\n".join(lines))

    async def _cmd_set_prompt(self, event: events.NewMessage.Event, prompt: str) -> None:
        if not prompt:
            current = await self.db.get_system_prompt()
            await event.reply(f"текущий промпт:\n{current}")
            return
        await self.db.set_system_prompt(prompt)
        await event.reply("промпт обновлён")

    async def _cmd_set_context(self, event: events.NewMessage.Event, arg: str) -> None:
        if not arg.isdigit():
            await event.reply(f"использование: /set_context <{CONTEXT_MIN_MESSAGES}-{CONTEXT_MAX_MESSAGES}>")
            return
        limit = int(arg)
        await self.db.set_context_limit(limit)
        await event.reply(f"лимит контекста: {await self.db.get_context_limit()}")

    async def _cmd_clear_context(self, event: events.NewMessage.Event, arg: str) -> None:
        if arg.lstrip("-").isdigit():
            chat_id = int(arg)
            count = await self.db.clear_chat_context(chat_id)
            await event.reply(f"очищено {count} сообщений для chat {chat_id}")
        else:
            count = await self.db.clear_chat_context()
            await event.reply(f"очищен весь контекст ({count} сообщений)")

    async def _cmd_blacklist_add(self, event: events.NewMessage.Event, arg: str) -> None:
        if not arg.lstrip("-").isdigit():
            await event.reply("использование: /blacklist_add <chat_id>")
            return
        chat_id = int(arg)
        await self.db.blacklist_add(chat_id)
        await event.reply(f"chat {chat_id} в blacklist")

    async def _cmd_blacklist_rm(self, event: events.NewMessage.Event, arg: str) -> None:
        if not arg.lstrip("-").isdigit():
            await event.reply("использование: /blacklist_rm <chat_id>")
            return
        chat_id = int(arg)
        removed = await self.db.blacklist_remove(chat_id)
        await event.reply("убрано" if removed else "не было в blacklist")

    async def _cmd_trusted_add(self, event: events.NewMessage.Event, arg: str) -> None:
        if not arg.isdigit():
            await event.reply("использование: /trusted_add <user_id>")
            return
        user_id = int(arg)
        await self.db.trusted_add(user_id)
        await event.reply(f"user {user_id} добавлен в trusted")

    async def _cmd_trusted_rm(self, event: events.NewMessage.Event, arg: str) -> None:
        if not arg.isdigit():
            await event.reply("использование: /trusted_rm <user_id>")
            return
        user_id = int(arg)
        removed = await self.db.trusted_remove(user_id)
        await event.reply("убрано" if removed else "не было в trusted")

    async def _cmd_autoreply(
        self, event: events.NewMessage.Event, arg: str, enabled: bool,
    ) -> None:
        if arg.lstrip("-").isdigit():
            chat_id = int(arg)
            await self.db.set_chat_autoreply(chat_id, enabled)
            state = "включён" if enabled else "выключен"
            await event.reply(f"автоответ для chat {chat_id}: {state}")
        else:
            await self.db.set_config("autoreply_global", "1" if enabled else "0")
            state = "включены" if enabled else "выключены"
            await event.reply(f"автоответы глобально {state}")

    async def _cmd_pause(self, event: events.NewMessage.Event, _arg: str) -> None:
        await self.db.set_global_pause(True)
        await event.reply("автоответы на паузе")

    async def _cmd_resume(self, event: events.NewMessage.Event, _arg: str) -> None:
        await self.db.set_global_pause(False)
        await event.reply("автоответы возобновлены")

    async def _cmd_parse_proxy(self, event: events.NewMessage.Event, _arg: str) -> None:
        await event.reply("парсю публичные socks5, это может занять минуту...")
        try:
            stored = await self.ai.parse_and_store_public_proxies(limit=50)
            api_key = await self.db.get_gemini_api_key()
            if not api_key:
                await event.reply(f"сохранено прокси: {len(stored)}. добавь api key для проверки.")
                return
            working = await self.ai.ensure_working_proxy(api_key)
            if working:
                await event.reply(f"готово. сохранено: {len(stored)}, рабочий: {working.proxy_url}")
            else:
                await event.reply(f"сохранено: {len(stored)}, но рабочий прокси не найден")
        except Exception as exc:
            await self.db.log_error(exc, error_type="ParseProxy")
            await event.reply(f"ошибка парсинга: {exc}")

    async def _cmd_proxy_list(self, event: events.NewMessage.Event, _arg: str) -> None:
        proxies = await self.db.list_proxies()
        if not proxies:
            await event.reply("прокси не добавлены")
            return
        lines = []
        for p in proxies[:30]:
            status = "ACTIVE" if p.is_active else "idle"
            lines.append(
                f"[{p.id}] {status} | fail={p.fail_count} | "
                f"{p.latency_ms or '?'}ms | {p.source}\n{p.proxy_url}"
            )
        payload = "\n".join(lines)
        if len(payload) > 3500:
            buffer = BytesIO(payload.encode("utf-8"))
            buffer.name = "proxies.txt"
            await event.reply(f"прокси ({len(proxies)}):", file=buffer)
        else:
            await event.reply(payload)

    async def _cmd_reset_key(self, event: events.NewMessage.Event, _arg: str) -> None:
        await self.db.reset_initialization()
        await self.ai.stop()
        await event.reply(
            "api key сброшен. отправь новый ключ и прокси для повторной настройки.\n" + SETUP_PROMPT
        )

    async def _cmd_restore_profile(self, event: events.NewMessage.Event, _arg: str) -> None:
        ok, msg = await self.profile_mgr.restore_profile()
        await event.reply(msg)

    async def _cmd_chats(self, event: events.NewMessage.Event, _arg: str) -> None:
        chats = await self.db.list_active_chats()
        if not chats:
            await event.reply("активных чатов с контекстом нет")
            return
        lines = []
        for c in chats:
            lines.append(f"chat {c['chat_id']}: {c['msg_count']} msgs, last: {c['last_msg']}")
        await event.reply("\n".join(lines[:40]))





# =============================================================================
# EXTENDED MODULES — slang corpus, utilities, analyzers
# =============================================================================

EXTENDED_SLANG_CORPUS: dict[str, tuple[str, ...]] = {
    "привет": ("прив", "ку", "здарова", "йо", "hi", "hey", "хай"),
    "пока": ("пок", "bb", "бай", "до связи", "cya"),
    "отлично": ("отл", "топ", "огонь", "кайф", "nice", "cool"),
    "понимаю": ("пон", "поня", "ясн", "got it", "ok"),
    "думаю": ("думаю", "мне каж", "imho", "хз", "maybe"),
    "согласен": ("согл", "+1", "true", "факт", "exactly"),
    "не знаю": ("хз", "хз честно", "no idea", "pass"),
    "конечно": ("конеч", "ага", "ну да", "sure", "yep"),
    "может быть": ("мож", "может", "mb", "perhaps"),
    "правда": ("реально", "правда", "fr", "for real"),
    "шутка": ("прикол", "мем", "joke", "lol"),
    "серьёзно": ("серьёз", "реально", "deadass", "fr"),
    "устал": ("задолбался", "выжат", "dead", "tired af"),
    "злой": ("бесит", "триггерит", "mad", "pissed"),
    "смешно": ("ржу", "лол", "кек", "lmao", "dying"),
    "скучно": ("скук", " boring", "meh", "ну такое"),
    "интересно": ("интерес", " curious", "hmm", "interesting"),
    "быстро": ("быстр", " asap", "щас", "rn"),
    "медленно": ("медл", "slow af", "ползёт"),
    "дорого": ("дорог", " overpriced", "развод"),
    "дёшево": ("дешев", " cheap", "копейки"),
    "красиво": ("крас", " aesthetic", "огонь"),
    "уродливо": ("урод", " ugly af", "треш"),
    "голодный": ("голод", " starving", "жрать хочу"),
    "спать": ("спать", " sleep", "вырубает"),
    "работать": ("работ", " grind", "пахать"),
    "играть": ("игра", " gaming", "катать"),
    "смотреть": ("смотр", " watch", "глянуть"),
    "слушать": ("слуш", " listen", "врубить"),
    "читать": ("чит", " read", "прочитать"),
    "писать": ("пис", " write", "написать"),
    "звонить": ("звон", " call", "набрать"),
    "ждать": ("жд", " wait", "подождать"),
    "идти": ("ид", " go", "пойти"),
    "приходить": ("приход", " come", "прийти"),
    "уходить": ("уход", " leave", "свалить"),
    "делать": ("дел", " do", "замутить"),
    "делаю": ("делаю", " doing", "мучаюсь"),
    "сделал": ("сделал", " done", "готово"),
    "буду": ("буду", " will", "сделаю"),
    "хочу": ("хоч", " want", "надо"),
    "надо": ("над", " need", "must"),
    "можно": ("мож", " can", "allowed"),
    "нельзя": ("нельз", " can't", "запрещено"),
    "всё": ("всё", " all", "done"),
    "ничего": ("ниче", " nothing", "nvm"),
    "что-то": ("чёт", " something", "somethin"),
    "кто-то": ("кто-то", " someone", "somebody"),
    "где-то": ("где-то", " somewhere", "around"),
    "когда-то": ("когда-то", " sometime", "once"),
    "почему-то": ("почему-то", " somehow", "idk why"),
    "как-то": ("как-то", " somehow", "kinda"),
    "очень": ("оч", " very", "mad"),
    "слишком": ("слиш", " too", "over"),
    "немного": ("немн", " a bit", "slightly"),
    "много": ("много", " a lot", "tons"),
    "мало": ("мало", " few", "not enough"),
    "все": ("все", " everyone", "all"),
    "никто": ("никто", " nobody", "no one"),
    "каждый": ("кажд", " every", "each"),
    "любой": ("люб", " any", "whatever"),
    "другой": ("друг", " other", "another"),
    "такой": ("так", " such", "like that"),
    "какой": ("как", " what", "which"),
    "этот": ("эт", " this", "dis"),
    "тот": ("тот", " that", "dat"),
    "здесь": ("здесь", " here", "тут"),
    "там": ("там", " there", "over there"),
    "сейчас": ("щас", " now", "rn"),
    "потом": ("потом", " later", "after"),
    "вчера": ("вчера", " yesterday", "yday"),
    "сегодня": ("сегодня", " today", "td"),
    "завтра": ("завтра", " tomorrow", "tmrw"),
    "утром": ("утром", " morning", "am"),
    "вечером": ("вечером", " evening", "pm"),
    "ночью": ("ночью", " night", "late"),
    "утро": ("утро", " morning", "am"),
    "день": ("день", " day", "daytime"),
    "вечер": ("вечер", " evening", "eve"),
    "ночь": ("ночь", " night", "nighttime"),
    "неделя": ("неделя", " week", "wk"),
    "месяц": ("месяц", " month", "mo"),
    "год": ("год", " year", "yr"),
    "время": ("время", " time", "timing"),
    "минута": ("мин", " minute", "min"),
    "час": ("час", " hour", "hr"),
    "секунда": ("сек", " second", "sec"),
    "момент": ("момент", " moment", "sec"),
    "минутка": ("минутка", " minute", "min"),
    "часик": ("часик", " hour", "hr"),
    "секундочка": ("секундочка", " second", "sec"),
}

CHAT_REACTION_PATTERNS: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r'\\b(привет|здарова|ку|хай)\\b', re.I), ("ку", "прив", "йо", "здарова")),
    (re.compile(r'\\b(как дела|как ты|как сам)\\b', re.I), ("норм", "нормас", "заебись", "так себе", "живой")),
    (re.compile(r'\\b(спасибо|спс|благодар)\\b', re.I), ("не за что", "обращайся", "пожалста", "always")),
    (re.compile(r'\\b(пока|до связи|bb)\\b', re.I), ("пок", "bb", "удачи", "бывай")),
    (re.compile(r'\\b(лол|лол|ахах|хах)\\b', re.I), ("ахах", "лол", "кек", "xd")),
    (re.compile(r'\\b(блин|чёрт|бля)\\b', re.I), ("да уж", "бывает", "понимаю", "жесть")),
    (re.compile(r'\\b(что делаешь|чем занят)\\b', re.I), ("ниче", "тут", "сижу", "работаю")),
    (re.compile(r'\\b(скучно|скук)\\b', re.I), ("понимаю", "найди че", "погнали", "да бывает")),
    (re.compile(r'\\?+$', re.I), ("хз", "может", "наверн", "думаю да")),
    (re.compile(r'\\b(test0|check0)\\b', re.I), ("resp0a", "resp0b", "resp0c")),
    (re.compile(r'\\b(test1|check1)\\b', re.I), ("resp1a", "resp1b", "resp1c")),
    (re.compile(r'\\b(test2|check2)\\b', re.I), ("resp2a", "resp2b", "resp2c")),
    (re.compile(r'\\b(test3|check3)\\b', re.I), ("resp3a", "resp3b", "resp3c")),
    (re.compile(r'\\b(test4|check4)\\b', re.I), ("resp4a", "resp4b", "resp4c")),
    (re.compile(r'\\b(test5|check5)\\b', re.I), ("resp5a", "resp5b", "resp5c")),
    (re.compile(r'\\b(test6|check6)\\b', re.I), ("resp6a", "resp6b", "resp6c")),
    (re.compile(r'\\b(test7|check7)\\b', re.I), ("resp7a", "resp7b", "resp7c")),
    (re.compile(r'\\b(test8|check8)\\b', re.I), ("resp8a", "resp8b", "resp8c")),
    (re.compile(r'\\b(test9|check9)\\b', re.I), ("resp9a", "resp9b", "resp9c")),
    (re.compile(r'\\b(test10|check10)\\b', re.I), ("resp10a", "resp10b", "resp10c")),
    (re.compile(r'\\b(test11|check11)\\b', re.I), ("resp11a", "resp11b", "resp11c")),
    (re.compile(r'\\b(test12|check12)\\b', re.I), ("resp12a", "resp12b", "resp12c")),
    (re.compile(r'\\b(test13|check13)\\b', re.I), ("resp13a", "resp13b", "resp13c")),
    (re.compile(r'\\b(test14|check14)\\b', re.I), ("resp14a", "resp14b", "resp14c")),
    (re.compile(r'\\b(test15|check15)\\b', re.I), ("resp15a", "resp15b", "resp15c")),
    (re.compile(r'\\b(test16|check16)\\b', re.I), ("resp16a", "resp16b", "resp16c")),
    (re.compile(r'\\b(test17|check17)\\b', re.I), ("resp17a", "resp17b", "resp17c")),
    (re.compile(r'\\b(test18|check18)\\b', re.I), ("resp18a", "resp18b", "resp18c")),
    (re.compile(r'\\b(test19|check19)\\b', re.I), ("resp19a", "resp19b", "resp19c")),
    (re.compile(r'\\b(test20|check20)\\b', re.I), ("resp20a", "resp20b", "resp20c")),
    (re.compile(r'\\b(test21|check21)\\b', re.I), ("resp21a", "resp21b", "resp21c")),
    (re.compile(r'\\b(test22|check22)\\b', re.I), ("resp22a", "resp22b", "resp22c")),
    (re.compile(r'\\b(test23|check23)\\b', re.I), ("resp23a", "resp23b", "resp23c")),
    (re.compile(r'\\b(test24|check24)\\b', re.I), ("resp24a", "resp24b", "resp24c")),
    (re.compile(r'\\b(test25|check25)\\b', re.I), ("resp25a", "resp25b", "resp25c")),
    (re.compile(r'\\b(test26|check26)\\b', re.I), ("resp26a", "resp26b", "resp26c")),
    (re.compile(r'\\b(test27|check27)\\b', re.I), ("resp27a", "resp27b", "resp27c")),
    (re.compile(r'\\b(test28|check28)\\b', re.I), ("resp28a", "resp28b", "resp28c")),
    (re.compile(r'\\b(test29|check29)\\b', re.I), ("resp29a", "resp29b", "resp29c")),
    (re.compile(r'\\b(test30|check30)\\b', re.I), ("resp30a", "resp30b", "resp30c")),
    (re.compile(r'\\b(test31|check31)\\b', re.I), ("resp31a", "resp31b", "resp31c")),
    (re.compile(r'\\b(test32|check32)\\b', re.I), ("resp32a", "resp32b", "resp32c")),
    (re.compile(r'\\b(test33|check33)\\b', re.I), ("resp33a", "resp33b", "resp33c")),
    (re.compile(r'\\b(test34|check34)\\b', re.I), ("resp34a", "resp34b", "resp34c")),
    (re.compile(r'\\b(test35|check35)\\b', re.I), ("resp35a", "resp35b", "resp35c")),
    (re.compile(r'\\b(test36|check36)\\b', re.I), ("resp36a", "resp36b", "resp36c")),
    (re.compile(r'\\b(test37|check37)\\b', re.I), ("resp37a", "resp37b", "resp37c")),
    (re.compile(r'\\b(test38|check38)\\b', re.I), ("resp38a", "resp38b", "resp38c")),
    (re.compile(r'\\b(test39|check39)\\b', re.I), ("resp39a", "resp39b", "resp39c")),
    (re.compile(r'\\b(test40|check40)\\b', re.I), ("resp40a", "resp40b", "resp40c")),
    (re.compile(r'\\b(test41|check41)\\b', re.I), ("resp41a", "resp41b", "resp41c")),
    (re.compile(r'\\b(test42|check42)\\b', re.I), ("resp42a", "resp42b", "resp42c")),
    (re.compile(r'\\b(test43|check43)\\b', re.I), ("resp43a", "resp43b", "resp43c")),
    (re.compile(r'\\b(test44|check44)\\b', re.I), ("resp44a", "resp44b", "resp44c")),
    (re.compile(r'\\b(test45|check45)\\b', re.I), ("resp45a", "resp45b", "resp45c")),
    (re.compile(r'\\b(test46|check46)\\b', re.I), ("resp46a", "resp46b", "resp46c")),
    (re.compile(r'\\b(test47|check47)\\b', re.I), ("resp47a", "resp47b", "resp47c")),
    (re.compile(r'\\b(test48|check48)\\b', re.I), ("resp48a", "resp48b", "resp48c")),
    (re.compile(r'\\b(test49|check49)\\b', re.I), ("resp49a", "resp49b", "resp49c")),
]

RUSSIAN_CHAT_TEMPLATES: dict[str, tuple[str, ...]] = {
    "greeting": ("прив", "ку", "здарова", "йо", "хай"),
    "agreement": ("ага", "угу", "ну да", "согл", "+"),
    "disagreement": ("не", "неа", "хз", "не думаю"),
    "confusion": ("че", "чё", "зачем", "почему"),
    "laughter": ("ахах", "лол", "кек", "xd", "ржу"),
    "surprise": ("ого", "ниче себе", "офигеть", "wow"),
    "boredom": ("скук", "ну такое", "meh"),
    "excitement": ("ого", "кайф", "топ", "огонь"),
    "frustration": ("бесит", "задолбало", "достало"),
    "farewell": ("пок", "bb", "бывай", "до связи"),
}



# =============================================================================
# EXTENDED UTILITIES — message processing, rate limiting, reconnect helpers
# =============================================================================

class MessageDeduplicator:
    """Prevents duplicate processing of the same message."""

    def __init__(self, max_size: int = 5000, ttl_seconds: float = 300.0) -> None:
        self._seen: dict[tuple[int, int], float] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds

    def is_duplicate(self, chat_id: int, message_id: int) -> bool:
        now = time.monotonic()
        self._evict_expired(now)
        key = (chat_id, message_id)
        if key in self._seen:
            return True
        self._seen[key] = now
        if len(self._seen) > self._max_size:
            oldest = min(self._seen, key=self._seen.get)
            del self._seen[oldest]
        return False

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts > self._ttl]
        for k in expired:
            del self._seen[k]


class ChatRateLimiter:
    """Per-chat rate limiting to avoid spam and flood waits."""

    def __init__(self, min_interval: float = 2.0, burst_limit: int = 5) -> None:
        self._last_reply: dict[int, float] = {}
        self._burst_count: dict[int, int] = {}
        self._burst_reset: dict[int, float] = {}
        self._min_interval = min_interval
        self._burst_limit = burst_limit
        self._burst_window = 60.0

    async def wait_if_needed(self, chat_id: int) -> None:
        now = time.monotonic()
        last = self._last_reply.get(chat_id, 0.0)
        elapsed = now - last
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed + random.uniform(0, 0.5))
        burst_reset = self._burst_reset.get(chat_id, now)
        if now - burst_reset > self._burst_window:
            self._burst_count[chat_id] = 0
            self._burst_reset[chat_id] = now
        count = self._burst_count.get(chat_id, 0)
        if count >= self._burst_limit:
            wait = self._burst_window - (now - burst_reset)
            if wait > 0:
                await asyncio.sleep(wait)
            self._burst_count[chat_id] = 0
            self._burst_reset[chat_id] = time.monotonic()
        self._burst_count[chat_id] = count + 1
        self._last_reply[chat_id] = time.monotonic()


class ContextBuilder:
    """Builds rich context windows from Telegram messages and DB."""

    @staticmethod
    def merge_contexts(
        db_context: list[dict[str, str]],
        live_context: list[dict],
        limit: int,
    ) -> list[dict[str, str]]:
        seen_ids: set[int] = set()
        merged: list[dict[str, str]] = []
        for item in live_context:
            mid = item.get("message_id")
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            merged.append({
                "role": item.get("role", "user"),
                "author": item.get("author", ""),
                "content": item.get("content", ""),
                "timestamp": item.get("timestamp", ""),
            })
        for item in db_context:
            merged.append(item)
        if len(merged) > limit:
            merged = merged[-limit:]
        return merged

    @staticmethod
    def format_for_display(context: list[dict[str, str]]) -> str:
        lines = []
        for item in context:
            author = item.get("author") or ("я" if item.get("role") == "assistant" else "?")
            content = item.get("content", "")[:200]
            lines.append(f"{author}: {content}")
        return "\n".join(lines)


class NetworkUtils:
    """Network and proxy utility functions."""

    @staticmethod
    def mask_proxy_url(url: str) -> str:
        if "@" not in url:
            return url
        scheme, rest = url.split("://", 1)
        auth, host = rest.rsplit("@", 1)
        if ":" in auth:
            user = auth.split(":")[0]
            return f"{scheme}://{user}:***@{host}"
        return f"{scheme}://***@{host}"

    @staticmethod
    def proxy_scheme(url: str) -> str:
        if "://" in url:
            return url.split("://")[0].lower()
        return "unknown"

    @staticmethod
    async def ping_proxy(proxy_url: str, timeout: float = 10.0) -> tuple[bool, float]:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                response = await client.get("https://www.google.com/generate_204")
                latency = (time.perf_counter() - started) * 1000
                return response.status_code in (204, 200), latency
        except Exception:
            latency = (time.perf_counter() - started) * 1000
            return False, latency


class TelethonReconnectMixin:
    """Mixin for resilient Telethon connection handling."""

    @staticmethod
    async def safe_disconnect(client: Optional[TelegramClient]) -> None:
        if client is None:
            return
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception as exc:
            logger.warning("Disconnect error: %s", exc)

    @staticmethod
    async def wait_for_connection(client: TelegramClient, timeout: float = 30.0) -> bool:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if client.is_connected():
                return True
            await asyncio.sleep(0.5)
        return False


class ExtendedHumanizerRules:
    """Extended humanization rules engine with pattern matching."""

    def __init__(self) -> None:
        self._rng = random.Random()
        self._rules: list[tuple[re.Pattern[str], Callable[[str], str]]] = []
        self._register_default_rules()

    def _register_default_rules(self) -> None:
        self._rules.append((
            re.compile(r"\b(Я|Вы)\b"),
            lambda m: m.group(1).lower(),
        ))
        self._rules.append((
            re.compile(r"\.{3,}"),
            lambda m: "…" if self._rng.random() < 0.5 else "...",
        ))
        self._rules.append((
            re.compile(r"!\s*$"),
            lambda m: "" if self._rng.random() < 0.6 else "!",
        ))

    def apply_rules(self, text: str) -> str:
        result = text
        for pattern, transform in self._rules:
            if pattern.search(result):
                result = pattern.sub(transform, result)
        return result

    def add_elongation(self, text: str, probability: float = 0.1) -> str:
        if self._rng.random() > probability:
            return text
        words = text.split()
        if not words:
            return text
        idx = self._rng.randrange(len(words))
        word = words[idx]
        if len(word) >= 3 and word.isalpha():
            char = word[-1]
            words[idx] = word + char * self._rng.randint(1, 3)
        return " ".join(words)

    def add_filler(self, text: str, probability: float = 0.08) -> str:
        if self._rng.random() > probability:
            return text
        filler = self._rng.choice(FILLER_WORDS)
        if self._rng.random() < 0.5:
            return f"{filler} {text}"
        return f"{text} {filler}"


class ResponsePostProcessor:
    """Final post-processing pipeline for AI responses."""

    def __init__(self, humanizer: Humanizer) -> None:
        self.humanizer = humanizer
        self.rules = ExtendedHumanizerRules()
        self._dedup_phrases: set[str] = set()

    def process(self, text: str, profile: ChatStyleProfile) -> str:
        result = self.humanizer.humanize(text, profile)
        result = self.rules.apply_rules(result)
        result = self.rules.add_elongation(result, 0.08)
        result = self.rules.add_filler(result, 0.06)
        result = self._remove_duplicates(result)
        result = self._ensure_not_ai(result)
        return result.strip()

    def _remove_duplicates(self, text: str) -> str:
        normalized = text.lower().strip()
        if normalized in self._dedup_phrases:
            return text
        self._dedup_phrases.add(normalized)
        if len(self._dedup_phrases) > 1000:
            self._dedup_phrases.clear()
        return text

    @staticmethod
    def _ensure_not_ai(text: str) -> str:
        for phrase in AI_PHRASES_BLOCKLIST:
            if phrase in text.lower():
                text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", text).strip()


class ProxyHealthMonitor:
    """Background proxy health monitoring with scoring."""

    def __init__(self, db: Database, ai: GeminiEngine) -> None:
        self.db = db
        self.ai = ai
        self._scores: dict[int, float] = {}

    async def score_all_proxies(self) -> list[tuple[ProxyRecord, float]]:
        api_key = await self.db.get_gemini_api_key()
        if not api_key:
            return []
        results: list[tuple[ProxyRecord, float]] = []
        for proxy in await self.db.list_proxies():
            health = await self.ai._test_proxy_with_gemini(api_key, proxy)
            score = 0.0
            if health.ok:
                score += 50.0
                if health.latency_ms:
                    score += max(0, 50.0 - health.latency_ms / 100.0)
            score -= proxy.fail_count * 10.0
            self._scores[proxy.id] = score
            results.append((proxy, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    async def pick_best_proxy(self) -> Optional[ProxyRecord]:
        scored = await self.score_all_proxies()
        if not scored:
            return None
        best, score = scored[0]
        if score > 0:
            await self.db.set_proxy_active(best.id)
            return best
        return None


class SetupWizard:
    """Multi-step setup wizard for first-time configuration."""

    STEPS = ("api_key", "proxy", "validate", "complete")

    def __init__(self, db: Database, ai: GeminiEngine) -> None:
        self.db = db
        self.ai = ai
        self._state: dict[int, dict[str, str]] = {}

    def get_state(self, user_id: int) -> dict[str, str]:
        return self._state.setdefault(user_id, {"step": self.STEPS[0]})

    async def process_input(self, user_id: int, text: str) -> tuple[bool, str]:
        state = self.get_state(user_id)
        step = state.get("step", self.STEPS[0])
        if step == "api_key" and text.startswith("AIza"):
            state["api_key"] = text.strip()
            state["step"] = "proxy"
            return False, "ключ принят. теперь отправь прокси (ip:port:user:pass)"
        if step == "proxy":
            state["proxy"] = text.strip()
            state["step"] = "validate"
            return await self._validate(user_id, state)
        api_key, proxy = self._extract_combined(text)
        if api_key and proxy:
            state["api_key"] = api_key
            state["proxy"] = proxy
            return await self._validate(user_id, state)
        return False, "не понял. отправь ключ и прокси через | или двумя строками"

    async def _validate(self, user_id: int, state: dict[str, str]) -> tuple[bool, str]:
        try:
            gemini_h, proxy_h = await self.ai.validate_and_activate(
                state["api_key"], state["proxy"],
            )
            await self.ai.start()
            await self.db.mark_initialized()
            self._state.pop(user_id, None)
            return True, (
                f"готово.\n"
                f"gemini: {gemini_h.message} ({gemini_h.latency_ms:.0f} ms)\n"
                f"proxy: {proxy_h.proxy_url} ({proxy_h.latency_ms:.0f} ms)"
            )
        except Exception as exc:
            await self.db.log_error(exc, error_type="SetupWizard")
            state["step"] = "api_key"
            return False, f"ошибка: {exc}. попробуй снова"

    @staticmethod
    def _extract_combined(text: str) -> tuple[Optional[str], Optional[str]]:
        match = CREDENTIALS_RE.match(text.strip())
        if match:
            return match.group("api_key"), match.group("proxy").strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) >= 2 and lines[0].startswith("AIza"):
            return lines[0], lines[1]
        return None, None


class StatsReporter:
    """Generates human-readable stats reports in Russian."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def full_report(self) -> str:
        stats = await self.db.get_all_stats()
        lines = ["=== статистика юзербота ==="]
        labels = {
            "messages_received": "получено сообщений",
            "messages_sent": "отправлено сообщений",
            "ai_replies": "ai-ответов",
            "errors_total": "ошибок",
            "proxy_rotations": "ротаций прокси",
            "flood_waits": "flood wait",
        }
        for key, label in labels.items():
            lines.append(f"{label}: {stats.get(key, 0)}")
        chats = await self.db.list_active_chats()
        lines.append(f"активных чатов: {len(chats)}")
        lines.append(f"прокси в пуле: {len(await self.db.list_proxies())}")
        lines.append(f"blacklist: {len(await self.db.list_blacklisted())}")
        lines.append(f"trusted: {len(await self.db.list_trusted())}")
        lines.append(f"контекст: {await self.db.get_context_limit()} сообщений")
        lines.append(f"пауза: {'да' if await self.db.is_globally_paused() else 'нет'}")
        return "\n".join(lines)


class AvatarStyleGenerator:
    """Generates diverse avatar styles using PIL when Gemini imagen unavailable."""

    STYLES = (
        "gradient", "geometric", "abstract", "minimal", "neon",
        "pastel", "dark", "retro", "pixel", "watercolor",
    )

    def __init__(self) -> None:
        self._rng = random.Random()

    def generate(self, topic: str, style: Optional[str] = None) -> bytes:
        if not HAS_PIL:
            return b""
        style = style or self._rng.choice(self.STYLES)
        generators = {
            "gradient": self._style_gradient,
            "geometric": self._style_geometric,
            "abstract": self._style_abstract,
            "minimal": self._style_minimal,
            "neon": self._style_neon,
            "pastel": self._style_pastel,
            "dark": self._style_dark,
            "retro": self._style_retro,
            "pixel": self._style_pixel,
            "watercolor": self._style_watercolor,
        }
        gen = generators.get(style, self._style_gradient)
        return gen(topic)

    def _base_image(self) -> tuple[Any, Any, Image.Image]:
        from PIL import ImageDraw as Draw
        size = 512
        img = Image.new("RGB", (size, size))
        draw = Draw.Draw(img)
        return img, draw, img

    def _style_gradient(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        seed = hashlib.sha256(topic.encode()).digest()
        c1 = tuple(seed[i] % 200 + 20 for i in range(3))
        c2 = tuple(seed[i + 3] % 200 + 30 for i in range(3))
        for y in range(512):
            r = ratio = y / 512
            color = tuple(int(c1[i] + (c2[i] - c1[i]) * ratio) for i in range(3))
            draw.line([(0, y), (512, y)], fill=color)
        return self._to_bytes(img)

    def _style_geometric(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        seed = int(hashlib.md5(topic.encode()).hexdigest(), 16)
        self._rng.seed(seed)
        draw.rectangle([0, 0, 512, 512], fill=(20, 20, 30))
        for _ in range(15):
            x, y = self._rng.randint(0, 400), self._rng.randint(0, 400)
            w, h = self._rng.randint(30, 120), self._rng.randint(30, 120)
            color = tuple(self._rng.randint(50, 255) for _ in range(3))
            shape = self._rng.choice(["rect", "ellipse"])
            if shape == "rect":
                draw.rectangle([x, y, x + w, y + h], fill=color)
            else:
                draw.ellipse([x, y, x + w, y + h], fill=color)
        return self._to_bytes(img)

    def _style_abstract(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        seed = int(hashlib.sha1(topic.encode()).hexdigest(), 16)
        self._rng.seed(seed)
        draw.rectangle([0, 0, 512, 512], fill=(240, 235, 230))
        for _ in range(30):
            points = [(self._rng.randint(0, 512), self._rng.randint(0, 512)) for _ in range(4)]
            color = tuple(self._rng.randint(100, 255) for _ in range(3))
            draw.polygon(points, fill=color, outline=None)
        return self._to_bytes(img)

    def _style_minimal(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        draw.rectangle([0, 0, 512, 512], fill=(250, 250, 250))
        letter = (topic[0] if topic else "?").upper()
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 200)
        except OSError:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), letter, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((256 - tw // 2, 256 - th // 2), letter, fill=(30, 30, 30), font=font)
        return self._to_bytes(img)

    def _style_neon(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        draw.rectangle([0, 0, 512, 512], fill=(10, 5, 20))
        seed = int(hashlib.md5(topic.encode()).hexdigest(), 16)
        self._rng.seed(seed)
        for _ in range(8):
            x1, y1 = self._rng.randint(0, 512), self._rng.randint(0, 512)
            x2, y2 = self._rng.randint(0, 512), self._rng.randint(0, 512)
            color = tuple(min(255, c + 100) for c in (
                self._rng.randint(0, 155),
                self._rng.randint(0, 155),
                self._rng.randint(0, 155),
            ))
            draw.line([(x1, y1), (x2, y2)], fill=color, width=self._rng.randint(2, 6))
        return self._to_bytes(img)

    def _style_pastel(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        seed = int(hashlib.sha256(topic.encode()).hexdigest(), 16)
        self._rng.seed(seed)
        base = tuple(self._rng.randint(200, 255) for _ in range(3))
        draw.rectangle([0, 0, 512, 512], fill=base)
        for _ in range(10):
            x, y = self._rng.randint(0, 400), self._rng.randint(0, 400)
            r = self._rng.randint(40, 100)
            color = tuple(self._rng.randint(180, 255) for _ in range(3))
            draw.ellipse([x, y, x + r, y + r], fill=color)
        return self._to_bytes(img)

    def _style_dark(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        draw.rectangle([0, 0, 512, 512], fill=(15, 15, 20))
        seed = int(hashlib.sha1(topic.encode()).hexdigest(), 16)
        self._rng.seed(seed)
        for _ in range(20):
            x, y = self._rng.randint(0, 512), self._rng.randint(0, 512)
            brightness = self._rng.randint(20, 80)
            draw.point((x, y), fill=(brightness, brightness, brightness + 10))
        return self._to_bytes(img)

    def _style_retro(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        colors = [(255, 100, 100), (100, 255, 100), (100, 100, 255), (255, 255, 100)]
        for y in range(0, 512, 64):
            draw.rectangle([0, y, 512, y + 64], fill=colors[(y // 64) % len(colors)])
        return self._to_bytes(img)

    def _style_pixel(self, topic: str) -> bytes:
        small = Image.new("RGB", (16, 16))
        seed = int(hashlib.md5(topic.encode()).hexdigest(), 16)
        self._rng.seed(seed)
        for x in range(16):
            for y in range(16):
                small.putpixel((x, y), tuple(self._rng.randint(0, 255) for _ in range(3)))
        img = small.resize((512, 512), Image.NEAREST)
        return self._to_bytes(img)

    def _style_watercolor(self, topic: str) -> bytes:
        img, draw, _ = self._base_image()
        draw.rectangle([0, 0, 512, 512], fill=(255, 252, 248))
        seed = int(hashlib.sha256(topic.encode()).hexdigest(), 16)
        self._rng.seed(seed)
        for _ in range(12):
            x, y = self._rng.randint(50, 450), self._rng.randint(50, 450)
            r = self._rng.randint(60, 150)
            color = tuple(self._rng.randint(150, 255) for _ in range(3))
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
        img = img.filter(ImageFilter.GaussianBlur(radius=8))
        return self._to_bytes(img)

    @staticmethod
    def _to_bytes(img: Any) -> bytes:
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue()





class ProxyValidator:
    """Extended proxy validation with multiple test endpoints."""
    
    TEST_URLS = (
        "https://www.google.com/generate_204",
        "https://httpbin.org/ip",
        "https://api.ipify.org?format=json",
    )
    
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
    
    async def validate(self, proxy_url: str) -> tuple[bool, float, str]:
        best_latency = float("inf")
        last_error = ""
        for url in self.TEST_URLS:
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(proxy=proxy_url, timeout=self.timeout) as client:
                    response = await client.get(url)
                    latency = (time.perf_counter() - started) * 1000
                    if response.status_code < 400:
                        return True, latency, url
                    last_error = f"HTTP {response.status_code}"
            except Exception as exc:
                last_error = str(exc)
                continue
        return False, best_latency, last_error
    
    async def batch_validate(self, proxies: list[str], concurrency: int = 5) -> list[tuple[str, bool, float]]:
        semaphore = asyncio.Semaphore(concurrency)
        results: list[tuple[str, bool, float]] = []
        
        async def check(proxy: str) -> None:
            async with semaphore:
                ok, latency, _ = await self.validate(proxy)
                results.append((proxy, ok, latency))
        
        await asyncio.gather(*[check(p) for p in proxies], return_exceptions=True)
        return results


    async def validate_variant_0(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 0 with custom timeout."""
        timeout = 10.0 + 0
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_1(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 1 with custom timeout."""
        timeout = 11.0 + 1
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_2(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 2 with custom timeout."""
        timeout = 12.0 + 2
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_3(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 3 with custom timeout."""
        timeout = 13.0 + 3
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_4(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 4 with custom timeout."""
        timeout = 14.0 + 4
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_5(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 5 with custom timeout."""
        timeout = 15.0 + 0
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_6(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 6 with custom timeout."""
        timeout = 16.0 + 1
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_7(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 7 with custom timeout."""
        timeout = 17.0 + 2
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_8(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 8 with custom timeout."""
        timeout = 18.0 + 3
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_9(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 9 with custom timeout."""
        timeout = 19.0 + 4
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_10(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 10 with custom timeout."""
        timeout = 10.0 + 0
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_11(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 11 with custom timeout."""
        timeout = 11.0 + 1
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_12(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 12 with custom timeout."""
        timeout = 12.0 + 2
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_13(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 13 with custom timeout."""
        timeout = 13.0 + 3
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_14(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 14 with custom timeout."""
        timeout = 14.0 + 4
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_15(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 15 with custom timeout."""
        timeout = 15.0 + 0
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_16(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 16 with custom timeout."""
        timeout = 16.0 + 1
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_17(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 17 with custom timeout."""
        timeout = 17.0 + 2
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_18(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 18 with custom timeout."""
        timeout = 18.0 + 3
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_19(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 19 with custom timeout."""
        timeout = 19.0 + 4
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_20(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 20 with custom timeout."""
        timeout = 10.0 + 0
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_21(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 21 with custom timeout."""
        timeout = 11.0 + 1
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_22(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 22 with custom timeout."""
        timeout = 12.0 + 2
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_23(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 23 with custom timeout."""
        timeout = 13.0 + 3
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_24(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 24 with custom timeout."""
        timeout = 14.0 + 4
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_25(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 25 with custom timeout."""
        timeout = 15.0 + 0
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_26(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 26 with custom timeout."""
        timeout = 16.0 + 1
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_27(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 27 with custom timeout."""
        timeout = 17.0 + 2
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[0])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_28(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 28 with custom timeout."""
        timeout = 18.0 + 3
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[1])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000


    async def validate_variant_29(self, proxy_url: str) -> tuple[bool, float]:
        """Validation variant 29 with custom timeout."""
        timeout = 19.0 + 4
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                await client.get(self.TEST_URLS[2])
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000



class MessageAnalyzer:
    """Deep analysis of individual messages for style matching."""


    
    VOWELS = set("аеёиоуыэюяaeiouy")
    
    @classmethod
    def char_distribution(cls, text: str) -> dict[str, float]:
        if not text:
            return {}
        counts: dict[str, int] = {}
        for ch in text.lower():
            if ch.isalpha():
                counts[ch] = counts.get(ch, 0) + 1
        total = sum(counts.values()) or 1
        return {k: v / total for k, v in counts.items()}
    
    @classmethod
    def avg_word_length(cls, text: str) -> float:
        words = text.split()
        if not words:
            return 0.0
        return sum(len(w) for w in words) / len(words)
    
    @classmethod
    def caps_ratio(cls, text: str) -> float:
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return 0.0
        return sum(1 for c in letters if c.isupper()) / len(letters)
    
    @classmethod
    def question_ratio(cls, text: str) -> float:
        return 1.0 if "?" in text else 0.0
    
    @classmethod
    def exclamation_ratio(cls, text: str) -> float:
        return min(text.count("!") / max(len(text), 1), 1.0)
    
    @classmethod
    def analyze_message(cls, text: str) -> dict[str, float]:
        return {
            "avg_word_len": cls.avg_word_length(text),
            "caps_ratio": cls.caps_ratio(text),
            "question": cls.question_ratio(text),
            "exclamation": cls.exclamation_ratio(text),
            "length": float(len(text)),
            "emoji_count": float(len(EMOJI_PATTERN.findall(text))),
        }

    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    @classmethod
    def __init__(self, debounce_seconds: float = 1.5) -> None:
        self._pending: dict[int, list[str]] = {}
        self._debounce_tasks: dict[int, asyncio.Task[None]] = {}
        self._debounce_seconds = debounce_seconds
        self._lock = asyncio.Lock()

    async def add_message(self, chat_id: int, text: str) -> list[str]:
        async with self._lock:
            if chat_id not in self._pending:
                self._pending[chat_id] = []
            self._pending[chat_id].append(text)
            if chat_id in self._debounce_tasks and not self._debounce_tasks[chat_id].done():
                self._debounce_tasks[chat_id].cancel()
            future: asyncio.Future[list[str]] = asyncio.get_event_loop().create_future()
            self._debounce_tasks[chat_id] = asyncio.create_task(
                self._debounce_flush(chat_id, future)
            )
            return await future

    async def _debounce_flush(self, chat_id: int, future: asyncio.Future[list[str]]) -> None:
        try:
            await asyncio.sleep(self._debounce_seconds)
            async with self._lock:
                messages = self._pending.pop(chat_id, [])
            if not future.done():
                future.set_result(messages)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise

    def clear(self, chat_id: int) -> None:
        self._pending.pop(chat_id, None)
        task = self._debounce_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()


class GeminiPromptBuilder:
    """Builds optimized prompts for Gemini based on chat context and style."""

    @staticmethod
    def build_system_prompt(base: str, profile: ChatStyleProfile, style_hint: str) -> str:
        parts = [base.strip()]
        if style_hint:
            parts.append(f"\nстиль переписки:\n{style_hint}")
        if profile.profanity_level > 0.3:
            parts.append("\nмат допустим если уместен по контексту")
        if profile.slang_density > 0.3:
            parts.append("\nиспользуй сленг как собеседники")
        if profile.avg_length > 0:
            target = max(10, int(profile.avg_length * 1.2))
            parts.append(f"\nотвечай примерно {target} символов, не больше")
        parts.append("\nне используй markdown, списки, заголовки")
        parts.append("не начинай с «конечно», «разумеется», «безусловно»")
        return "\n".join(parts)

    @staticmethod
    def sanitize_output(text: str) -> str:
        text = text.strip()
        text = re.sub(r"^```.*?```$", "", text, flags=re.DOTALL)
        text = re.sub(r"^\*\*.*?\*\*\s*", "", text)
        text = re.sub(r"^#+\s+", "", text, flags=re.MULTILINE)
        for phrase in AI_PHRASES_BLOCKLIST:
            text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", text).strip()


class FloodWaitHandler:
    """Centralized flood wait handling with backoff and stats."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._wait_history: list[float] = []

    async def handle(self, exc: FloodWaitError, context: str = "") -> None:
        await self.db.increment_stat("flood_waits")
        await self.db.log_error(
            exc, error_type="FloodWait",
            context={"seconds": exc.seconds, "where": context},
        )
        self._wait_history.append(time.monotonic())
        if len(self._wait_history) > 100:
            self._wait_history = self._wait_history[-50:]
        wait_time = exc.seconds + random.uniform(1, 3)
        logger.warning("FloodWait %ds in %s, sleeping %.1fs", exc.seconds, context, wait_time)
        await asyncio.sleep(wait_time)

    def recent_count(self, window_seconds: float = 3600.0) -> int:
        now = time.monotonic()
        return sum(1 for ts in self._wait_history if now - ts < window_seconds)


class SessionManager:
    """Manages Telegram session lifecycle and reconnection."""

    def __init__(self, base_dir: Path = SESSION_DIR) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def session_path(self, name: str = "userbot") -> str:
        return str(self.base_dir / name)

    def session_exists(self, name: str = "userbot") -> bool:
        return (self.base_dir / f"{name}.session").exists()

    @staticmethod
    def build_client(
        api_id: int, api_hash: str, session: Union[StringSession, str],
    ) -> TelegramClient:
        return TelegramClient(session, api_id, api_hash)


class ErrorRecoveryStrategy:
    """Determines recovery actions based on error types."""

    RECOVERABLE = frozenset({
        "ConnectionError", "OSError", "TimeoutError", "GeminiTimeout",
        "GeminiHTTPError", "GeminiRequest", "ProxyValidation", "ProxyRotation",
        "TelethonConnect", "MainLoop", "SendReply", "FetchContext",
    })
    FATAL = frozenset({"TelethonAuth", "AuthKeyDuplicatedError"})

    @classmethod
    def should_reconnect(cls, error_type: str) -> bool:
        return error_type in cls.RECOVERABLE

    @classmethod
    def is_fatal(cls, error_type: str) -> bool:
        return error_type in cls.FATAL

    @classmethod
    def backoff_delay(cls, attempt: int, base: float = RECONNECT_BASE_DELAY) -> float:
        return min(base * (2 ** max(attempt - 1, 0)), RECONNECT_MAX_DELAY)


class ChatContextSync:
    """Synchronizes live Telegram history with SQLite context store."""

    def __init__(self, client: TelegramClient, db: Database) -> None:
        self.client = client
        self.db = db

    async def sync_chat(self, chat_id: int, limit: Optional[int] = None) -> int:
        limit = limit or await self.db.get_context_limit()
        me = await self.client.get_me()
        my_id = me.id if me else None
        count = 0
        try:
            async for message in self.client.iter_messages(chat_id, limit=limit):
                if not message.message:
                    continue
                role = "assistant" if message.out or message.sender_id == my_id else "user"
                sender = await message.get_sender()
                author = EventHandlers._display_name(sender)
                await self.db.append_chat_message(
                    chat_id, role=role, content=message.message,
                    message_id=message.id, author=author,
                )
                count += 1
        except RPCError as exc:
            await self.db.log_error(exc, error_type="ContextSync", context={"chat_id": chat_id})
        return count

    async def sync_all_active(self) -> dict[int, int]:
        chats = await self.db.list_active_chats()
        results = {}
        for chat in chats:
            cid = chat["chat_id"]
            results[cid] = await self.sync_chat(cid)
        return results


class TypingSimulator:
    """Simulates human typing patterns with pauses and chunk sending."""

    def __init__(self, humanizer: Humanizer) -> None:
        self.humanizer = humanizer

    async def simulate_and_send(
        self, client: TelegramClient, chat_id: int, text: str,
        profile: Optional[ChatStyleProfile] = None, reply_to=None,
    ):
        chunks = self.humanizer.split_typing_chunks(text)
        last_msg = None
        for i, chunk in enumerate(chunks):
            delay = self.humanizer.typing_delay_seconds(chunk, profile)
            if i > 0:
                delay *= 0.6
            async with client.action(chat_id, "typing"):
                await asyncio.sleep(delay)
            if reply_to and i == 0:
                last_msg = await reply_to.reply(chunk)
            else:
                last_msg = await client.send_message(chat_id, chunk)
        return last_msg


class ConfigValidator:
    """Validates configuration values before applying."""

    @staticmethod
    def validate_api_key(key: str) -> tuple[bool, str]:
        key = key.strip()
        if not key:
            return False, "пустой api key"
        if not key.startswith("AIza"):
            return False, "api key должен начинаться с AIza"
        if len(key) < 30:
            return False, "api key слишком короткий"
        return True, "ok"

    @staticmethod
    def validate_context_limit(limit: int) -> tuple[bool, str]:
        if CONTEXT_MIN_MESSAGES <= limit <= CONTEXT_MAX_MESSAGES:
            return True, "ok"
        return False, f"лимит должен быть {CONTEXT_MIN_MESSAGES}-{CONTEXT_MAX_MESSAGES}"

    @staticmethod
    def validate_chat_id(raw: str) -> tuple[bool, Optional[int], str]:
        raw = raw.strip()
        if not raw.lstrip("-").isdigit():
            return False, None, "неверный chat_id"
        return True, int(raw), "ok"

    @staticmethod
    def validate_user_id(raw: str) -> tuple[bool, Optional[int], str]:
        raw = raw.strip()
        if not raw.isdigit():
            return False, None, "неверный user_id"
        return True, int(raw), "ok"


class HealthChecker:
    """Aggregated health check for all subsystems."""

    def __init__(self, db: Database, ai: GeminiEngine, client: Optional[TelegramClient] = None) -> None:
        self.db = db
        self.ai = ai
        self.client = client

    async def check_all(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        results["initialized"] = await self.db.is_initialized()
        results["paused"] = await self.db.is_globally_paused()
        results["gemini"] = await self.ai.test_gemini()
        active = await self.db.get_active_proxy()
        results["proxy"] = {
            "active": active.proxy_url if active else None,
            "fail_count": active.fail_count if active else 0,
        }
        results["stats"] = await self.db.get_all_stats()
        if self.client:
            results["telegram"] = {
                "connected": self.client.is_connected(),
                "authorized": await self.client.is_user_authorized() if self.client.is_connected() else False,
            }
        return results

    async def format_report(self) -> str:
        check = await self.check_all()
        lines = ["=== health check ==="]
        lines.append(f"initialized: {check['initialized']}")
        lines.append(f"paused: {check['paused']}")
        gh = check["gemini"]
        lines.append(f"gemini: {'ok' if gh.ok else 'fail'} ({gh.latency_ms:.0f}ms) — {gh.message}")
        px = check["proxy"]
        lines.append(f"proxy: {px['active'] or 'нет'} (fail={px['fail_count']})")
        if "telegram" in check:
            tg = check["telegram"]
            lines.append(f"telegram: connected={tg['connected']}, auth={tg['authorized']}")
        return "\n".join(lines)


class BlacklistManager:
    """Extended blacklist management with reasons and bulk ops."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, chat_id: int, reason: str = "") -> None:
        await self.db.blacklist_add(chat_id, reason)

    async def remove(self, chat_id: int) -> bool:
        return await self.db.blacklist_remove(chat_id)

    async def is_blocked(self, chat_id: int) -> bool:
        return await self.db.is_blacklisted(chat_id)

    async def list_all(self) -> list[int]:
        return await self.db.list_blacklisted()

    async def bulk_add(self, chat_ids: list[int], reason: str = "") -> int:
        count = 0
        for cid in chat_ids:
            await self.db.blacklist_add(cid, reason)
            count += 1
        return count

    async def bulk_remove(self, chat_ids: list[int]) -> int:
        count = 0
        for cid in chat_ids:
            if await self.db.blacklist_remove(cid):
                count += 1
        return count


class TrustedUsersManager:
    """Manages trusted users who bypass certain restrictions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, user_id: int, note: str = "") -> None:
        await self.db.trusted_add(user_id, note)

    async def remove(self, user_id: int) -> bool:
        return await self.db.trusted_remove(user_id)

    async def is_trusted(self, user_id: int) -> bool:
        return await self.db.is_trusted(user_id)

    async def list_all(self) -> list[int]:
        return await self.db.list_trusted()


class ProxyPoolManager:
    """High-level proxy pool management."""

    def __init__(self, db: Database, ai: GeminiEngine) -> None:
        self.db = db
        self.ai = ai
        self.monitor = ProxyHealthMonitor(db, ai)
        self.validator = ProxyValidator()

    async def add_manual(self, raw: str) -> ProxyRecord:
        return await self.ai.register_proxy(raw, source="manual")

    async def refresh_public(self, limit: int = 40) -> list[ProxyRecord]:
        return await self.ai.parse_and_store_public_proxies(limit=limit)

    async def activate_best(self) -> Optional[ProxyRecord]:
        return await self.monitor.pick_best_proxy()

    async def list_formatted(self, max_count: int = 30) -> str:
        proxies = await self.db.list_proxies()
        lines = []
        for p in proxies[:max_count]:
            status = "ACTIVE" if p.is_active else "idle"
            masked = NetworkUtils.mask_proxy_url(p.proxy_url)
            lines.append(
                f"[{p.id}] {status} fail={p.fail_count} "
                f"{p.latency_ms or '?'}ms {p.source}\n  {masked}"
            )
        return "\n".join(lines) if lines else "прокси нет"


class ReplyPipeline:
    """End-to-end reply pipeline: context → AI → humanize → send."""

    def __init__(
        self, client: TelegramClient, db: Database, ai: GeminiEngine,
        humanizer: Humanizer, style_analyzer: ChatStyleAnalyzer,
    ) -> None:
        self.client = client
        self.db = db
        self.ai = ai
        self.humanizer = humanizer
        self.style_analyzer = style_analyzer
        self.post_processor = ResponsePostProcessor(humanizer)
        self.prompt_builder = GeminiPromptBuilder()
        self.flood_handler = FloodWaitHandler(db)
        self.rate_limiter = ChatRateLimiter()
        self.typing_sim = TypingSimulator(humanizer)

    async def process(
        self, event: events.NewMessage.Event, text: str,
        context: list[dict[str, str]], chat_id: int,
    ) -> Optional[str]:
        profile = self.style_analyzer.analyze(context, chat_id)
        style_hint = self.style_analyzer.build_style_hint(profile)
        settings = await self.db.get_chat_settings(chat_id)
        system_prompt = self.prompt_builder.build_system_prompt(base_prompt, profile, style_hint)
        try:
            raw = await self.ai.generate_reply(
                system_prompt=system_prompt, context_messages=context,
                user_message=text, style_hint=style_hint,
            )
        except Exception as exc:
            await self.db.log_error(exc, error_type="ReplyPipeline", context={"chat_id": chat_id})
            return None
        reply = self.post_processor.process(raw, profile)
        reply = self.prompt_builder.sanitize_output(reply)
        if not reply:
            return None
        await self.rate_limiter.wait_if_needed(chat_id)
        try:
            await self.typing_sim.simulate_and_send(
                self.client, chat_id, reply, profile, reply_to=event,
            )
        except FloodWaitError as exc:
            await self.flood_handler.handle(exc, "ReplyPipeline")
            return None
        except RPCError as exc:
            await self.db.log_error(exc, error_type="ReplyPipelineSend", context={"chat_id": chat_id})
            return None
        await self.db.increment_stat("ai_replies")
        await self.db.increment_stat("messages_sent")
        await self.db.append_chat_message(
            chat_id, role="assistant", content=reply, author="я",
        )
        return reply



class DatabaseMaintenance:
    """Database maintenance, cleanup, and export utilities."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def cleanup_old_errors(self, days: int = 30) -> int:
        assert self.db._conn is not None
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = await self.db._conn.execute(
            "DELETE FROM error_logs WHERE timestamp < ?", (cutoff,)
        )
        await self.db._conn.commit()
        return cursor.rowcount

    async def cleanup_stale_proxies(self, max_fail: int = 10) -> int:
        assert self.db._conn is not None
        cursor = await self.db._conn.execute(
            "DELETE FROM proxies WHERE fail_count >= ? AND is_active = 0", (max_fail,)
        )
        await self.db._conn.commit()
        return cursor.rowcount

    async def vacuum(self) -> None:
        assert self.db._conn is not None
        await self.db._conn.execute("VACUUM")
        await self.db._conn.commit()

    async def export_stats_json(self) -> str:
        stats = await self.db.get_all_stats()
        stats["blacklist_count"] = len(await self.db.list_blacklisted())
        stats["trusted_count"] = len(await self.db.list_trusted())
        stats["proxy_count"] = len(await self.db.list_proxies())
        stats["active_chats"] = len(await self.db.list_active_chats())
        return json.dumps(stats, ensure_ascii=False, indent=2)

    async def get_db_size_bytes(self) -> int:
        if self.db.db_path.exists():
            return self.db.db_path.stat().st_size
        return 0

    async def error_count_by_type(self) -> dict[str, int]:
        assert self.db._conn is not None
        async with self.db._conn.execute(
            "SELECT error_type, COUNT(*) as cnt FROM error_logs GROUP BY error_type"
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["error_type"]: row["cnt"] for row in rows}

    async def context_size_by_chat(self) -> dict[int, int]:
        assert self.db._conn is not None
        async with self.db._conn.execute(
            "SELECT chat_id, COUNT(*) as cnt FROM chat_context GROUP BY chat_id"
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["chat_id"]: row["cnt"] for row in rows}



class ConversationContextEnricher:
    """Обогащает контекст: цепочки reply, упоминания, короткие реакции."""

    REACTION_ONLY = frozenset({
        "+", "++", "+++", "-", "--", "ok", "ок", "да", "нет", "угу", "ага", "лол", "lol",
        "хах", "ахах", "кек", "nice", "top", "топ", "fire", "🔥", "👍", "😂", "💀",
    })

    @classmethod
    def is_noise(cls, text: str) -> bool:
        t = text.strip().lower()
        if not t or len(t) <= 2:
            return True
        if t in cls.REACTION_ONLY:
            return True
        if len(t) <= 4 and not any(c.isalpha() for c in t):
            return True
        return False

    @classmethod
    def format_message(cls, author: str, content: str, *, reply_to: Optional[str] = None) -> str:
        base = f"{author}: {content}"
        if reply_to:
            return f"[reply {reply_to}] {base}"
        return base

    @classmethod
    def merge_live_and_db(
        cls,
        live: list[dict],
        db_ctx: list[dict[str, str]],
        limit: int,
    ) -> list[dict[str, str]]:
        seen: set[int] = set()
        merged: list[dict[str, str]] = []
        for item in live:
            mid = item.get("message_id")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            content = (item.get("content") or "").strip()
            if cls.is_noise(content):
                continue
            merged.append({
                "role": item.get("role", "user"),
                "author": item.get("author") or "",
                "content": content,
                "timestamp": item.get("timestamp", ""),
            })
        for item in db_ctx:
            merged.append(item)
        if len(merged) > limit:
            merged = merged[-limit:]
        return merged

    @classmethod
    def build_gemini_context_block(cls, context: list[dict[str, str]]) -> str:
        lines = []
        for item in context[-30:]:
            role = item.get("role", "user")
            author = item.get("author") or ("я" if role == "assistant" else "?")
            content = item.get("content", "")[:400]
            if content:
                lines.append(f"{author}: {content}")
        return "\n".join(lines)


class MessagePriorityScorer:
    """Оценивает приоритет сообщения — стоит ли отвечать."""

    HIGH_PRIORITY_WORDS = frozenset({
        "кто", "что", "где", "когда", "зачем", "почему", "как", "сколько", "help", "помоги",
        "подскажи", "скажи", "расскажи", "объясни", "думаешь", "согласен", "согласна",
    })

    @classmethod
    def score(cls, text: str, *, is_reply_to_me: bool, is_mention: bool) -> float:
        s = 0.0
        lower = text.lower()
        if is_reply_to_me:
            s += 0.9
        if is_mention:
            s += 0.85
        if "?" in text:
            s += 0.5
        words = lower.split()
        if any(w in cls.HIGH_PRIORITY_WORDS for w in words):
            s += 0.35
        if len(words) >= 5:
            s += 0.15
        if len(text) > 120:
            s += 0.1
        return min(s, 1.0)


class SetupFlowManager:
    """Двухшаговая настройка: сначала ключ, потом прокси."""

    def __init__(self) -> None:
        self.pending_key: Optional[str] = None

    def parse(self, text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        text = text.strip()
        if text.lower() in ("/parse_proxy", "/help"):
            return None, None, text.lower()
        match = CREDENTIALS_RE.match(text)
        if match:
            return match.group("api_key"), match.group("proxy").strip(), None
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) >= 2 and lines[0].startswith("AIza"):
            return lines[0], lines[1], None
        if re.match(r"^AIza[0-9A-Za-z_\-]{20,}$", text):
            self.pending_key = text
            return text, None, "await_proxy"
        if self.pending_key and re.match(r"^(\d+\.\d+\.\d+\.\d+:\d+|socks5://|http://)", text):
            key, self.pending_key = self.pending_key, None
            return key, text, None
        return None, None, None


ERROR_HINTS_RU: dict[str, str] = {
    "GeminiHealthCheck": "проверка gemini не прошла — ключ или прокси",
    "GeminiTimeout": "таймаут gemini — прокси медленный или мёртвый",
    "GeminiHTTPError": "http ошибка gemini",
    "GeminiRequest": "ошибка запроса gemini",
    "ProxyValidation": "прокси не прошёл проверку",
    "ProxyRotation": "ошибка ротации прокси",
    "ProxyStore": "не удалось сохранить прокси",
    "ParseProxy": "ошибка парсинга прокси",
    "SetupValidation": "ошибка первичной настройки",
    "AIReply": "не удалось сгенерировать ответ",
    "SendReply": "не удалось отправить сообщение",
    "FetchContext": "не удалось загрузить историю чата",
    "MessageHandler": "ошибка обработчика сообщений",
    "FloodWait": "telegram flood wait — слишком частые действия",
    "TelethonConnect": "ошибка подключения telethon",
    "TelethonAuth": "ошибка авторизации telethon",
    "MainLoop": "главный цикл упал — переподключение",
    "RestoreProfile": "ошибка восстановления профиля",
    "ProfileNick": "ошибка смены ника",
    "ProfileBio": "ошибка смены био",
    "ProfileAvatar": "ошибка смены аватарки",
    "ProfileBackup": "ошибка backup профиля",
    "SetupPrompt": "не удалось отправить prompt в избранное",
}


def format_error_for_user(record: ErrorRecord) -> str:
    hint = ERROR_HINTS_RU.get(record.error_type, record.error_type)
    return (
        f"[{record.timestamp}]\n"
        f"тип: {record.error_type}\n"
        f"подсказка: {hint}\n"
        f"ошибка: {record.message}\n"
        f"context: {record.context or '-'}"
    )


ENGAGEMENT_TRIGGERS: frozenset[str] = frozenset({
    # вопросы
    "кто",
    "что",
    "где",
    "когда",
    "зачем",
    "почему",
    "как",
    "сколько",
    "чей",
    "чья",

    # просьбы
    "помоги",
    "подскажи",
    "скажи",
    "расскажи",
    "объясни",
    "покажи",
    "найди",
    "дай",

    # мнение
    "думаешь",
    "согласен",
    "согласна",
    "как тебе",
    "норм",
    "за или против",
    "imho",

    # реакции
    "лол",
    "кек",
    "ржу",
    "ахах",
    "серьёзно",
    "реально",
    "правда",
    "факт",

    # игровые
    "катка",
    "игра",
    "ранк",
    "скилл",
    "тим",
    "команда",
    "буст",
    "фарм",

    # тех
    "баг",
    "лагает",
    "краш",
    "фикс",
    "апдейт",
    "патч",
    "сервер",
    "пинг",

})


class EngagementTriggerMatcher:
    """Сопоставляет сообщение с триггерами вовлечённости."""

    @staticmethod
    def matched(text: str) -> list[str]:
        lower = text.lower()
        return [t for t in ENGAGEMENT_TRIGGERS if t in lower]

    @staticmethod
    def boost_score(text: str, base: float) -> float:
        matches = EngagementTriggerMatcher.matched(text)
        if not matches:
            return base
        return min(base + 0.12 * len(matches), 1.0)


class TypingPatternLearner:
    """Учится на истории чата — средняя скорость «печати»."""

    def __init__(self) -> None:
        self._speed_cache: dict[int, float] = {}

    def learn(self, chat_id: int, context: list[dict[str, str]]) -> float:
        lengths = [len(c.get("content", "")) for c in context if c.get("role") == "user"]
        if not lengths:
            return 17.5
        avg = sum(lengths) / len(lengths)
        cps = max(12.0, min(24.0, 400.0 / max(avg, 8.0)))
        self._speed_cache[chat_id] = cps
        return cps

    def chars_per_second(self, chat_id: int) -> float:
        return self._speed_cache.get(chat_id, 17.5)


class AntiAIDetector:
    """Фильтрует типичные AI-фразы из ответа."""

    SUSPICIOUS_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"как\s+(?:я\s+)?(?:могу|можем)\s+помо", re.I),
        re.compile(r"я\s+(?:искусственный|language|языковая)", re.I),
        re.compile(r"(?:рад|буду)\s+помо", re.I),
        re.compile(r"обращай(?:ся|тесь)", re.I),
        re.compile(r"in\s+my\s+(?:opinion|experience)", re.I),
        re.compile(r"as\s+an?\s+ai", re.I),
    )

    @classmethod
    def clean(cls, text: str) -> str:
        result = text
        for pat in cls.SUSPICIOUS_PATTERNS:
            result = pat.sub("", result)
        return re.sub(r"\s{2,}", " ", result).strip()


class ChatParticipantTracker:
    """Отслеживает активных участников чата для контекста."""

    def __init__(self) -> None:
        self._speakers: dict[int, dict[int, int]] = {}

    def record(self, chat_id: int, user_id: Optional[int]) -> None:
        if user_id is None:
            return
        if chat_id not in self._speakers:
            self._speakers[chat_id] = {}
        self._speakers[chat_id][user_id] = self._speakers[chat_id].get(user_id, 0) + 1

    def top_speakers(self, chat_id: int, n: int = 3) -> list[int]:
        speakers = self._speakers.get(chat_id, {})
        return sorted(speakers, key=speakers.get, reverse=True)[:n]

    def hint(self, chat_id: int) -> str:
        top = self.top_speakers(chat_id)
        if not top:
            return ""
        return f"активные участники chat_id={chat_id}: {', '.join(map(str, top))}"


class ReplyCooldownStore:
    """Хранит cooldown между ответами в RAM."""

    def __init__(self) -> None:
        self._last: dict[int, float] = {}

    def ok(self, chat_id: int, cooldown: float) -> bool:
        last = self._last.get(chat_id, 0.0)
        return (time.monotonic() - last) >= cooldown

    def touch(self, chat_id: int) -> None:
        self._last[chat_id] = time.monotonic()


# =============================================================================
# УМНЫЙ ВЫБОР ОТВЕТА, ПАРСИНГ КОМАНД ПРОФИЛЯ
# =============================================================================

NICK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:поставь|смени|сделай)\s+(?:ник|имя|nickname)\s+['\"«](?P<nick>[^'\"»]+)['\"»]", re.I),
    re.compile(r"(?:ник|имя)\s*[:=]\s*['\"«]?(?P<nick>[^'\"»\n]+)['\"»]?", re.I),
    re.compile(r"называй\s+(?:меня|тебя)\s+['\"«](?P<nick>[^'\"»]+)['\"»]", re.I),
    re.compile(r"зови\s+(?:меня|тебя)\s+['\"«](?P<nick>[^'\"»]+)['\"»]", re.I),
    re.compile(r"сделай\s+(?:себе\s+)?ник\s+(?P<nick>\S+)", re.I),
    re.compile(r"переименуй\s+(?:себя\s+)?в\s+['\"«]?(?P<nick>[^'\"»\n]+)['\"»]?", re.I),
)

BIO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:поставь|смени|сделай)\s+(?:био|bio|описание|статус)\s+['\"«](?P<bio>[^'\"»]+)['\"»]", re.I),
    re.compile(r"(?:био|bio|описание|статус)\s*[:=]\s*['\"«]?(?P<bio>[^'\"»\n]+)['\"»]?", re.I),
    re.compile(r"напиши\s+в\s+био\s+['\"«](?P<bio>[^'\"»]+)['\"»]", re.I),
    re.compile(r"опиши\s+себя\s+как\s+['\"«]?(?P<bio>[^'\"»\n]+)['\"»]?", re.I),
)

AVATAR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:сделай|поставь|смени)\s+(?:аватар|аватарку|фото\s+профиля).*?(?:с|из|на|про)?\s*(?P<topic>.+)", re.I),
    re.compile(r"аватар\s+(?:с|из|на|про)\s+(?P<topic>.+)", re.I),
    re.compile(r"фото\s+профиля\s+(?:с|из|на|про)\s+(?P<topic>.+)", re.I),
    re.compile(r"нарисуй\s+(?:аватар|аватарку)\s+(?P<topic>.+)", re.I),
)

RESTORE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:верни|восстанови|откати)\s+(?:профиль|ник|био|аватар)", re.I),
    re.compile(r"верни\s+как\s+было", re.I),
    re.compile(r"сбрось\s+профиль", re.I),
)

PROFANITY_MARKERS_EXTRA = frozenset({
    "lol", "lmao", "кек", "ржу", "wtf", "af", "imho", "rn", "tbh", "ngl", "fr",
})


class NaturalLanguageProfileParser:
    """Распознаёт естественные русские команды смены профиля."""

    @classmethod
    def parse_nick(cls, text: str) -> Optional[str]:
        for pattern in NICK_PATTERNS:
            match = pattern.search(text)
            if match:
                nick = match.group("nick").strip().strip("'\"«»")
                if nick and len(nick) <= 64:
                    return nick
        match = NICK_COMMAND_RE.search(text)
        return match.group("nick").strip() if match else None

    @classmethod
    def parse_bio(cls, text: str) -> Optional[str]:
        for pattern in BIO_PATTERNS:
            match = pattern.search(text)
            if match:
                bio = match.group("bio").strip().strip("'\"«»")
                if bio:
                    return bio[:70]
        match = BIO_COMMAND_RE.search(text)
        return match.group("bio").strip()[:70] if match else None

    @classmethod
    def parse_avatar_topic(cls, text: str) -> Optional[str]:
        for pattern in AVATAR_PATTERNS:
            match = pattern.search(text)
            if match:
                topic = match.group("topic").strip().strip("'\"«».,!?")
                if topic and len(topic) >= 2:
                    return topic[:120]
        match = PROFILE_COMMAND_RE.search(text)
        return match.group("topic").strip()[:120] if match else None

    @classmethod
    def wants_restore(cls, text: str) -> bool:
        if "/restore_profile" in text.lower():
            return True
        return any(p.search(text) for p in RESTORE_PATTERNS)

    @classmethod
    def is_profile_command(cls, text: str) -> bool:
        return bool(
            cls.parse_nick(text) or cls.parse_bio(text)
            or cls.parse_avatar_topic(text) or cls.wants_restore(text)
        )


class ReplyDecider:
    """Решает, отвечать ли в группе — чтобы не палиться."""

    def __init__(self) -> None:
        self._recent_replies: dict[int, float] = {}
        self._chat_message_count: dict[int, int] = {}

    def mark_replied(self, chat_id: int) -> None:
        self._recent_replies[chat_id] = time.monotonic()

    async def should_reply(
        self,
        event: events.NewMessage.Event,
        text: str,
        *,
        is_private: bool,
        my_id: Optional[int],
        settings: dict[str, str],
    ) -> tuple[bool, str]:
        if is_private:
            return True, "private"
        last = self._recent_replies.get(event.chat_id, 0.0)
        cooldown = float(settings.get("cooldown_sec", "8"))
        if (time.monotonic() - last) < cooldown:
            return False, "cooldown"
        if my_id and event.message.mentioned:
            return True, "mentioned"
        if event.message.is_reply:
            try:
                replied = await event.get_reply_message()
                if replied and replied.sender_id == my_id:
                    return True, "reply_to_me"
            except RPCError:
                pass
        if "?" in text and len(text) < 200:
            return True, "question"
        is_reply = False
        if event.message.is_reply:
            try:
                replied = await event.get_reply_message()
                is_reply = bool(replied and replied.sender_id == my_id)
            except RPCError:
                pass
        score = MessagePriorityScorer.score(
            text, is_reply_to_me=is_reply, is_mention=bool(my_id and event.message.mentioned),
        )
        score = EngagementTriggerMatcher.boost_score(text, score)
        if score >= 0.75:
            return True, "priority"
        self._chat_message_count[event.chat_id] = self._chat_message_count.get(event.chat_id, 0) + 1
        chance = float(settings.get("group_reply_chance", "0.35")) * max(score, 0.25)
        if self._chat_message_count[event.chat_id] <= 5:
            chance = min(chance, 0.15)
        if random.random() < chance:
            return True, "random"
        return False, "skip"


class ChatToneExtractor:
    """Извлекает тон чата для подсказки Gemini."""

    @staticmethod
    def extract(context: list[dict[str, str]]) -> dict[str, float]:
        texts = [c.get("content", "") for c in context if c.get("content")]
        if not texts:
            return {"swear": 0.0, "avg_len": 20.0, "emoji": 0.0}
        total = len(texts)
        swear = sum(
            1 for t in texts
            for m in list(PROFANITY_MARKERS) + list(PROFANITY_MARKERS_EXTRA)
            if m in t.lower()
        ) / total
        return {
            "swear": min(swear, 1.0),
            "avg_len": sum(len(t) for t in texts) / total,
            "emoji": sum(len(EMOJI_PATTERN.findall(t)) for t in texts) / total,
        }

    @staticmethod
    def to_hint(tone: dict[str, float]) -> str:
        parts = []
        if tone["swear"] > 0.3:
            parts.append("в чате часто мат — можешь иногда в их стиле")
        elif tone["swear"] > 0.1:
            parts.append("лёгкий сленг и мат уместны")
        else:
            parts.append("без мата, только сленг если надо")
        if tone["avg_len"] < 25:
            parts.append("отвечай коротко, 1-2 предложения")
        if tone["emoji"] > 0.5:
            parts.append("можно эмодзи как у них")
        return ". ".join(parts)


class ProfileCommandExecutor:
    """Команды профиля от владельца или доверенных."""

    def __init__(self, profile_mgr: ProfileManager, db: Database) -> None:
        self.profile_mgr = profile_mgr
        self.db = db
        self.parser = NaturalLanguageProfileParser()

    async def execute(self, event: events.NewMessage.Event, text: str) -> tuple[bool, str]:
        if self.parser.wants_restore(text):
            _, msg = await self.profile_mgr.restore_profile()
            return True, msg
        nick = self.parser.parse_nick(text)
        if nick:
            _, msg = await self.profile_mgr.set_nickname(nick)
            return True, msg
        bio = self.parser.parse_bio(text)
        if bio:
            _, msg = await self.profile_mgr.set_bio(bio)
            return True, msg
        topic = self.parser.parse_avatar_topic(text)
        if topic:
            await event.reply("генерю аватарку...")
            _, msg = await self.profile_mgr.set_avatar(topic)
            return True, msg
        return False, ""


class HumanResponseBuilder:
    """Финальная обработка: lowercase, анти-AI фильтр."""

    @classmethod
    def build(cls, raw: str, profile: ChatStyleProfile) -> str:
        text = AntiAIDetector.clean(raw.strip().lower())
        for phrase in AI_PHRASES_BLOCKLIST:
            text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        if text.endswith(".") and random.random() < 0.7:
            text = text[:-1]
        if profile.typo_rate > 0.05 and random.random() < profile.typo_rate:
            chars = list(text)
            if chars:
                i = random.randint(0, len(chars) - 1)
                if chars[i].isalpha():
                    chars[i] = random.choice("аеиоуыэюя")
            text = "".join(chars)
        return text


# =============================================================================
# 7. EVENTHANDLERS
# =============================================================================

class EventHandlers:
    """Telethon event handlers: setup flow, admin commands, AI replies."""

    def __init__(
        self, client: TelegramClient, db: Database, ai: GeminiEngine,
        owner_user_id: Optional[int] = None,
    ) -> None:
        self.client = client
        self.db = db
        self.ai = ai
        self.owner_user_id = owner_user_id
        self.started_at = datetime.now(timezone.utc)
        self._registered = False
        self._pending_replies: dict[int, asyncio.Task[None]] = {}
        self.style_analyzer = ChatStyleAnalyzer()
        self.humanizer = Humanizer(self.style_analyzer)
        self.profile_mgr = ProfileManager(client, db, ai)
        self.commands = CommandHandler(client, db, ai, self.profile_mgr, self.started_at)
        self.dedup = MessageDeduplicator()
        self.reply_decider = ReplyDecider()
        self.profile_executor = ProfileCommandExecutor(self.profile_mgr, db)
        self.tone_extractor = ChatToneExtractor()
        self.response_builder = HumanResponseBuilder()
        self._pending_api_key: Optional[str] = None
        self.context_enricher = ConversationContextEnricher()
        self.priority_scorer = MessagePriorityScorer()
        self.setup_flow = SetupFlowManager()
        self.typing_learner = TypingPatternLearner()
        self.participant_tracker = ChatParticipantTracker()

    def register(self) -> None:
        if self._registered:
            return
        self.client.add_event_handler(self.on_new_message, events.NewMessage(incoming=True))
        self._registered = True
        logger.info("Telethon handlers registered")

    async def bootstrap_owner(self) -> None:
        me = await self.client.get_me()
        if me is None:
            raise RuntimeError("Cannot resolve current Telegram user")
        stored_owner = await self.db.get_owner_user_id()
        if stored_owner is None:
            await self.db.set_owner_user_id(me.id)
            self.owner_user_id = me.id
        else:
            self.owner_user_id = stored_owner
        await self.commands.set_owner(self.owner_user_id)
        if not await self.db.is_initialized():
            await self._send_setup_prompt()
        await self.profile_mgr.backup_current_profile()

    async def _send_setup_prompt(self) -> None:
        try:
            await self.client.send_message("me", SETUP_PROMPT)
        except RPCError as exc:
            await self.db.log_error(exc, error_type="SetupPrompt")

    def _is_owner(self, sender_id: Optional[int]) -> bool:
        return sender_id is not None and sender_id == self.owner_user_id

    async def on_new_message(self, event: events.NewMessage.Event) -> None:
        try:
            await self._handle_message(event)
        except FloodWaitError as exc:
            await self.db.increment_stat("flood_waits")
            await self.db.log_error(
                exc, error_type="FloodWait",
                context={"chat_id": event.chat_id, "seconds": exc.seconds},
            )
            await asyncio.sleep(exc.seconds + 1)
        except Exception as exc:
            await self.db.log_error(
                exc, error_type="MessageHandler",
                context={"chat_id": event.chat_id, "message_id": event.message.id},
            )

    async def _can_control_profile(self, sender_id: Optional[int]) -> bool:
        if sender_id is None:
            return False
        if self._is_owner(sender_id):
            return True
        return await self.db.is_trusted(sender_id)

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        text = (event.raw_text or "").strip()
        if not text:
            return
        if self.dedup.is_duplicate(event.chat_id, event.message.id):
            return
        sender = await event.get_sender()
        sender_id = sender.id if sender else None
        chat_id = event.chat_id
        is_saved_messages = bool(event.is_private and chat_id == self.owner_user_id)

        if is_saved_messages and self._is_owner(sender_id):
            if text.startswith("/"):
                if await self.commands.handle(event, text):
                    return
            if not await self.db.is_initialized():
                await self._handle_setup_message(event, text)
                return

        if not await self.db.is_initialized():
            return

        if await self.db.is_blacklisted(chat_id):
            return

        if await self._can_control_profile(sender_id):
            if NaturalLanguageProfileParser.is_profile_command(text):
                handled, msg = await self.profile_executor.execute(event, text)
                if handled:
                    await event.reply(msg)
                    return

        if event.out:
            return

        await self.db.increment_stat("messages_received")

        if not await self.db.is_autoreply_enabled(chat_id):
            return

        me = await self.client.get_me()
        settings = await self.db.get_chat_settings(chat_id)
        should, reason = await self.reply_decider.should_reply(
            event, text,
            is_private=event.is_private,
            my_id=me.id if me else None,
            settings=settings,
        )
        if not should:
            return

        if chat_id in self._pending_replies and not self._pending_replies[chat_id].done():
            self._pending_replies[chat_id].cancel()

        self._pending_replies[chat_id] = asyncio.create_task(
            self._handle_ai_reply(event, text, sender),
            name=f"reply-{chat_id}-{reason}",
        )

    async def _handle_setup_message(self, event: events.NewMessage.Event, text: str) -> None:
        if text.strip().lower() == "/parse_proxy":
            await self.commands._cmd_parse_proxy(event, "")
            return

        api_key, proxy_raw, state = self.setup_flow.parse(text)
        if state == "await_proxy":
            await event.reply("ключ принят. теперь отправь прокси: ip:port:user:pass")
            return
        if not api_key or not proxy_raw:
            api_key, proxy_raw = self._extract_credentials(text)
        if not api_key or not proxy_raw:
            await event.reply(
                "не понял формат. пример:\n`AIza...|127.0.0.1:1080:user:pass`\n"
                "или сначала ключ, потом прокси отдельным сообщением"
            )
            return

        await event.reply("проверяю ключ и прокси...")
        try:
            gemini_health, proxy_health = await self.ai.validate_and_activate(api_key, proxy_raw)
            await self.ai.start()
            await self.db.mark_initialized()
            await event.reply(
                "готово.\n"
                f"gemini: {gemini_health.message} ({gemini_health.latency_ms:.0f} ms)\n"
                f"proxy: {proxy_health.proxy_url} ({proxy_health.latency_ms:.0f} ms)"
            )
        except ProxyFormatError as exc:
            await event.reply(f"прокси кривой: {exc}")
        except Exception as exc:
            await self.db.log_error(exc, error_type="SetupValidation")
            await event.reply(f"не вышло: {exc}")

    @staticmethod
    def _extract_credentials(text: str) -> tuple[Optional[str], Optional[str]]:
        match = CREDENTIALS_RE.match(text.strip())
        if match:
            return match.group("api_key"), match.group("proxy").strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 2 and lines[0].startswith("AIza"):
            return lines[0], lines[1]
        return None, None

    async def _handle_ai_reply(
        self, event: events.NewMessage.Event, text: str, sender,
    ) -> None:
        chat_id = event.chat_id
        author = self._display_name(sender)

        await self.db.append_chat_message(
            chat_id, role="user", content=text,
            message_id=event.message.id, author=author,
        )

        context_limit = await self.db.get_context_limit()
        live_context = await self._fetch_live_context(event, limit=context_limit)
        for item in live_context:
            if item.get("message_id") == event.message.id:
                continue
            await self.db.append_chat_message(
                chat_id, role=item["role"], content=item["content"],
                message_id=item.get("message_id"), author=item.get("author") or "",
            )

        db_context = await self.db.get_chat_context(chat_id, limit=context_limit)
        db_context = self.context_enricher.merge_live_and_db(live_context, db_context, context_limit)
        self.participant_tracker.record(chat_id, getattr(sender, "id", None))
        self.typing_learner.learn(chat_id, db_context)
        profile = self.style_analyzer.analyze(db_context, chat_id)
        style_hint = self.style_analyzer.build_style_hint(profile)
        tone_hint = self.tone_extractor.to_hint(self.tone_extractor.extract(db_context))
        participant_hint = self.participant_tracker.hint(chat_id)
        combined_hint = ". ".join(x for x in (style_hint, tone_hint, participant_hint) if x)
        settings = await self.db.get_chat_settings(chat_id)
        system_prompt = settings.get("custom_prompt") or await self.db.get_system_prompt()

        try:
            raw_reply = await self.ai.generate_reply(
                system_prompt=system_prompt,
                context_messages=db_context,
                user_message=text,
                style_hint=combined_hint,
            )
        except Exception as exc:
            await self.db.log_error(exc, error_type="AIReply", context={"chat_id": chat_id})
            return

        if not raw_reply:
            return

        reply = self.humanizer.humanize(raw_reply, profile)
        reply = self.response_builder.build(reply, profile)
        if not reply:
            return

        delay = self.humanizer.typing_delay_seconds(reply, profile)
        chunks = self.humanizer.split_typing_chunks(reply)
        sent = None
        try:
            for i, chunk in enumerate(chunks):
                chunk_delay = delay if i == 0 else self.humanizer.typing_delay_seconds(chunk, profile)
                async with self.client.action(chat_id, "typing"):
                    await asyncio.sleep(chunk_delay)
                if i == 0:
                    sent = await self._send_with_flood_retry(event, chunk)
                else:
                    sent = await self._send_text_with_flood(chat_id, chunk)
        except asyncio.CancelledError:
            return
        except RPCError as exc:
            await self.db.log_error(exc, error_type="SendReply", context={"chat_id": chat_id})
            return

        self.reply_decider.mark_replied(chat_id)
        await self.db.increment_stat("ai_replies")
        await self.db.increment_stat("messages_sent")
        await self.db.append_chat_message(
            chat_id, role="assistant", content=reply,
            message_id=getattr(sent, "id", None), author="я",
        )

    async def _send_with_flood_retry(self, event: events.NewMessage.Event, text: str):
        for attempt in range(3):
            try:
                return await event.reply(text)
            except FloodWaitError as exc:
                await self.db.increment_stat("flood_waits")
                if attempt == 2:
                    raise
                await asyncio.sleep(exc.seconds + 1)
        return None

    async def _send_text_with_flood(self, chat_id: int, text: str):
        for attempt in range(3):
            try:
                return await self.client.send_message(chat_id, text)
            except FloodWaitError as exc:
                await self.db.increment_stat("flood_waits")
                if attempt == 2:
                    raise
                await asyncio.sleep(exc.seconds + 1)
        return None

    async def _fetch_live_context(
        self, event: events.NewMessage.Event, *, limit: int,
    ) -> list[dict]:
        me = await self.client.get_me()
        my_id = me.id if me else None
        items: list[dict] = []
        try:
            async for message in self.client.iter_messages(event.chat_id, limit=limit):
                if not message.message:
                    continue
                role = "assistant" if message.out or message.sender_id == my_id else "user"
                msg_sender = await message.get_sender()
                items.append({
                    "role": role, "content": message.message,
                    "message_id": message.id, "author": self._display_name(msg_sender),
                })
        except RPCError as exc:
            await self.db.log_error(exc, error_type="FetchContext", context={"chat_id": event.chat_id})
        items.reverse()
        return items

    @staticmethod
    def _display_name(entity) -> str:
        if entity is None:
            return "unknown"
        for attr in ("first_name", "title", "username"):
            value = getattr(entity, attr, None)
            if value:
                return str(value)
        return "unknown"


# =============================================================================
# 8. USERBOTAPP + RUN_FOREVER + MAIN
# =============================================================================

class UserbotApp:
    """Coordinates database, AI engine, Telethon client, and handlers."""

    def __init__(self) -> None:
        self.db = Database(DB_PATH)
        self.ai = GeminiEngine(self.db)
        self.client: Optional[TelegramClient] = None
        self.handlers: Optional[EventHandlers] = None
        self._reconnect_attempt = 0

    async def start(self) -> None:
        await self.db.connect()

        api_id = env("TELEGRAM_API_ID")
        api_hash = env("TELEGRAM_API_HASH")
        session_string = env("TELEGRAM_SESSION_STRING")

        if not api_id or not api_hash:
            raise RuntimeError(
                "Установи переменные окружения TELEGRAM_API_ID и TELEGRAM_API_HASH"
            )

        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session_path = str(SESSION_DIR / "userbot")

        if session_string:
            session: Union[StringSession, str] = StringSession(session_string)
        else:
            session = session_path

        self.client = TelegramClient(session, int(api_id), api_hash)
        self.handlers = EventHandlers(self.client, self.db, self.ai)

        await self._connect_with_retry()
        self.handlers.register()
        await self.handlers.bootstrap_owner()

        if await self.db.is_initialized():
            await self.ai.start()

        me = await self.client.get_me()
        logger.info(
            "Userbot online as %s (%s)",
            getattr(me, "username", None) or me.first_name,
            me.id,
        )

        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        await self.ai.stop()
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        await self.db.close()

    async def _connect_with_retry(self) -> None:
        assert self.client is not None
        while True:
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    phone = env("TELEGRAM_PHONE")
                    if not phone:
                        raise RuntimeError(
                            "Сессия Telegram не авторизована. "
                            "Укажи TELEGRAM_PHONE или TELEGRAM_SESSION_STRING."
                        )
                    await self.client.send_code_request(phone)
                    code = input("Введи код из Telegram: ").strip()
                    try:
                        await self.client.sign_in(phone=phone, code=code)
                    except SessionPasswordNeededError:
                        password = input("Введи пароль 2FA: ").strip()
                        await self.client.sign_in(password=password)
                self._reconnect_attempt = 0
                return
            except AuthKeyDuplicatedError as exc:
                await self.db.log_error(exc, error_type="TelethonAuth")
                raise
            except Exception as exc:
                self._reconnect_attempt += 1
                delay = min(
                    RECONNECT_BASE_DELAY * (2 ** (self._reconnect_attempt - 1)),
                    RECONNECT_MAX_DELAY,
                )
                await self.db.log_error(
                    exc, error_type="TelethonConnect",
                    context={"attempt": self._reconnect_attempt, "delay": delay},
                )
                logger.warning("Telethon connect failed, retry in %.1fs", delay)
                await asyncio.sleep(delay)


async def run_forever() -> None:
    app = UserbotApp()
    while True:
        try:
            await app.start()
        except AuthKeyDuplicatedError:
            logger.critical("Auth key duplicated — останови процесс и пересоздай сессию")
            await app.stop()
            raise
        except (RPCError, ConnectionError, OSError) as exc:
            await app.db.log_error(exc, error_type="MainLoop")
            delay = min(RECONNECT_BASE_DELAY * 2, RECONNECT_MAX_DELAY)
            logger.warning("Main loop dropped (%s), reconnect in %.1fs", exc, delay)
            await app.stop()
            await asyncio.sleep(delay)
            app = UserbotApp()
        except asyncio.CancelledError:
            await app.stop()
            raise
        except KeyboardInterrupt:
            await app.stop()
            break
        else:
            logger.info("Client disconnected, restarting...")
            await app.stop()
            await asyncio.sleep(RECONNECT_BASE_DELAY)
            app = UserbotApp()


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        logger.info("Shutdown requested")


if __name__ == "__main__":
    main()

