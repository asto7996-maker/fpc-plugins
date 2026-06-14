"""
SQLite persistence layer for the Telegram AI userbot.

Stores configuration, proxy pool, chat blacklists, error logs,
profile backups, and rolling chat context windows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "userbot.db"
ERROR_LOG_FILE = Path(__file__).resolve().parent / "userbot_errors.log"

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

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklisted_chats (
    chat_id   INTEGER PRIMARY KEY,
    added_at  TEXT NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_error_logs_ts ON error_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_chat_context_chat ON chat_context(chat_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_proxies_active ON proxies(is_active, fail_count);
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


class Database:
    """Async SQLite wrapper with schema bootstrap and typed helpers."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
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
        defaults = {
            "initialized": "0",
            "gemini_api_key": "",
            "active_proxy_id": "",
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "owner_user_id": "",
            "context_message_limit": "25",
        }
        for key, value in defaults.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value),
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
            """
            INSERT INTO config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self._conn.commit()

    async def is_initialized(self) -> bool:
        return await self.get_config("initialized") == "1"

    async def mark_initialized(self) -> None:
        await self.set_config("initialized", "1")

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

    async def get_active_proxy_id(self) -> Optional[int]:
        raw = await self.get_config("active_proxy_id")
        return int(raw) if raw.isdigit() else None

    async def set_active_proxy_id(self, proxy_id: Optional[int]) -> None:
        await self.set_config("active_proxy_id", str(proxy_id or ""))

    async def add_proxy(self, proxy_url: str, source: str = "manual") -> int:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT OR IGNORE INTO proxies (proxy_url, source)
            VALUES (?, ?)
            """,
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
        self,
        proxy_id: int,
        *,
        latency_ms: Optional[float] = None,
        failed: bool = False,
    ) -> None:
        assert self._conn is not None
        now = _utc_now()
        if failed:
            await self._conn.execute(
                """
                UPDATE proxies
                SET fail_count = fail_count + 1, last_check = ?, latency_ms = NULL
                WHERE id = ?
                """,
                (now, proxy_id),
            )
        else:
            await self._conn.execute(
                """
                UPDATE proxies
                SET fail_count = 0, last_check = ?, latency_ms = ?
                WHERE id = ?
                """,
                (now, latency_ms, proxy_id),
            )
        await self._conn.commit()

    async def deactivate_proxy(self, proxy_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE proxies SET is_active = 0 WHERE id = ?", (proxy_id,)
        )
        await self._conn.commit()
        active_id = await self.get_active_proxy_id()
        if active_id == proxy_id:
            await self.set_active_proxy_id(None)

    async def blacklist_add(self, chat_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT OR IGNORE INTO blacklisted_chats (chat_id, added_at) VALUES (?, ?)",
            (chat_id, _utc_now()),
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
            row = await cursor.fetchone()
        return row is not None

    async def list_blacklisted(self) -> list[int]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT chat_id FROM blacklisted_chats ORDER BY chat_id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [int(row["chat_id"]) for row in rows]

    async def save_profile_backup(
        self,
        first_name: Optional[str],
        last_name: Optional[str],
        about: Optional[str],
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO profile_backup (id, first_name, last_name, about, saved_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                about = excluded.about,
                saved_at = excluded.saved_at
            """,
            (first_name, last_name, about, _utc_now()),
        )
        await self._conn.commit()

    async def get_profile_backup(self) -> Optional[dict[str, Optional[str]]]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT first_name, last_name, about FROM profile_backup WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "about": row["about"],
        }

    async def append_chat_message(
        self,
        chat_id: int,
        *,
        role: str,
        content: str,
        message_id: Optional[int] = None,
        author: Optional[str] = None,
    ) -> None:
        assert self._conn is not None
        limit = int(await self.get_config("context_message_limit", "25"))
        await self._conn.execute(
            """
            INSERT INTO chat_context (chat_id, message_id, role, author, content, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, message_id, role, author, content, _utc_now()),
        )
        await self._conn.execute(
            """
            DELETE FROM chat_context
            WHERE chat_id = ?
              AND id NOT IN (
                  SELECT id FROM chat_context
                  WHERE chat_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (chat_id, chat_id, limit),
        )
        await self._conn.commit()

    async def get_chat_context(self, chat_id: int, limit: int = 25) -> list[dict[str, str]]:
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT role, author, content, timestamp
            FROM chat_context
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        rows = list(reversed(rows))
        return [
            {
                "role": row["role"],
                "author": row["author"] or "",
                "content": row["content"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    async def log_error(
        self,
        exc: BaseException,
        *,
        error_type: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> int:
        assert self._conn is not None
        ts = _utc_now()
        etype = error_type or type(exc).__name__
        message = str(exc) or repr(exc)
        tb = traceback.format_exc()
        ctx_json = json.dumps(context, ensure_ascii=False) if context else None

        cursor = await self._conn.execute(
            """
            INSERT INTO error_logs (timestamp, error_type, message, traceback, context)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ts, etype, message, tb, ctx_json),
        )
        await self._conn.commit()
        error_id = int(cursor.lastrowid)

        line = (
            f"[{ts}] {etype}: {message}\n"
            f"context={ctx_json or '{}'}\n"
            f"{tb}\n{'-' * 80}\n"
        )
        ERROR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line)

        logger.error("%s: %s", etype, message, exc_info=exc)
        return error_id

    async def get_recent_errors(self, limit: int = 50) -> list[ErrorRecord]:
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT id, timestamp, error_type, message, traceback, context
            FROM error_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ErrorRecord(
                id=row["id"],
                timestamp=row["timestamp"],
                error_type=row["error_type"],
                message=row["message"],
                traceback=row["traceback"],
                context=row["context"],
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_proxy(row: aiosqlite.Row) -> ProxyRecord:
        return ProxyRecord(
            id=int(row["id"]),
            proxy_url=row["proxy_url"],
            is_active=bool(row["is_active"]),
            last_check=row["last_check"],
            latency_ms=row["latency_ms"],
            fail_count=int(row["fail_count"]),
            source=row["source"],
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
