"""
Async SQLite database layer using aiosqlite.
Handles all persistence: accounts, parsed users, invites, reposted content, settings.
"""

import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Telegram user-level accounts (Telethon sessions)
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name    TEXT    UNIQUE NOT NULL,
    phone           TEXT,
    username        TEXT,
    first_name      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    invites_today   INTEGER NOT NULL DEFAULT 0,
    dms_today       INTEGER NOT NULL DEFAULT 0,
    errors_streak   INTEGER NOT NULL DEFAULT 0,
    last_used_at    TEXT,
    last_reset_at   TEXT,
    proxy_id        INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);

-- Proxy pool
CREATE TABLE IF NOT EXISTS proxies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_type  TEXT NOT NULL DEFAULT 'socks5',
    host        TEXT NOT NULL,
    port        INTEGER NOT NULL,
    username    TEXT,
    password    TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    last_check  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Donor channels for content machine
CREATE TABLE IF NOT EXISTS donor_channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER,
    username        TEXT UNIQUE NOT NULL,
    title           TEXT,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    posts_scraped   INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Content that has already been reposted (deduplication)
CREATE TABLE IF NOT EXISTS reposted_content (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    donor_channel   TEXT NOT NULL,
    original_msg_id INTEGER NOT NULL,
    file_unique_id  TEXT,
    target_msg_id   INTEGER,
    reposted_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(donor_channel, original_msg_id)
);

-- Users collected by parser
CREATE TABLE IF NOT EXISTS parsed_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER UNIQUE NOT NULL,
    username        TEXT,
    first_name      TEXT,
    last_name       TEXT,
    phone           TEXT,
    is_bot          INTEGER NOT NULL DEFAULT 0,
    last_seen       TEXT,
    source_group    TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    parsed_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Track every invite / DM sent to a user
CREATE TABLE IF NOT EXISTS sent_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    username        TEXT,
    action_type     TEXT NOT NULL,          -- 'invite' | 'dm'
    account_session TEXT,
    status          TEXT NOT NULL DEFAULT 'sent',   -- 'sent' | 'failed' | 'blocked'
    error_text      TEXT,
    sent_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Source groups for parser
CREATE TABLE IF NOT EXISTS source_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER,
    username    TEXT UNIQUE NOT NULL,
    title       TEXT,
    member_count INTEGER,
    is_active   INTEGER NOT NULL DEFAULT 1,
    last_parsed TEXT,
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Key-value settings store
CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- System event log
CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT NOT NULL,
    module      TEXT,
    message     TEXT NOT NULL,
    logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Indexes for frequent queries
CREATE INDEX IF NOT EXISTS idx_parsed_users_status   ON parsed_users(status);
CREATE INDEX IF NOT EXISTS idx_parsed_users_last_seen ON parsed_users(last_seen);
CREATE INDEX IF NOT EXISTS idx_sent_actions_user_id  ON sent_actions(user_id);
CREATE INDEX IF NOT EXISTS idx_sent_actions_sent_at  ON sent_actions(sent_at);
CREATE INDEX IF NOT EXISTS idx_reposted_donor        ON reposted_content(donor_channel, original_msg_id);
CREATE INDEX IF NOT EXISTS idx_event_log_logged_at   ON event_log(logged_at);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    """
    Async SQLite database wrapper.
    All public methods are coroutines and safe to call from asyncio tasks.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Open connection and create schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        logger.info(f"Database initialised at {self.db_path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute(
        self, sql: str, params: Tuple = ()
    ) -> aiosqlite.Cursor:
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            await self._conn.commit()
            return cursor

    async def _fetchone(
        self, sql: str, params: Tuple = ()
    ) -> Optional[Dict[str, Any]]:
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    async def _fetchall(
        self, sql: str, params: Tuple = ()
    ) -> List[Dict[str, Any]]:
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        row = await self._fetchone(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, KeyError):
            return row["value"]

    async def set_setting(self, key: str, value: Any) -> None:
        serialised = json.dumps(value) if not isinstance(value, str) else value
        await self._execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, serialised),
        )

    async def get_all_settings(self) -> Dict[str, Any]:
        rows = await self._fetchall("SELECT key, value FROM settings")
        result = {}
        for r in rows:
            try:
                result[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, KeyError):
                result[r["key"]] = r["value"]
        return result

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    async def upsert_account(
        self,
        session_name: str,
        phone: Optional[str] = None,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        proxy_id: Optional[int] = None,
    ) -> int:
        cursor = await self._execute(
            """
            INSERT INTO accounts(session_name, phone, username, first_name, proxy_id)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(session_name) DO UPDATE SET
                phone      = COALESCE(excluded.phone, accounts.phone),
                username   = COALESCE(excluded.username, accounts.username),
                first_name = COALESCE(excluded.first_name, accounts.first_name),
                proxy_id   = COALESCE(excluded.proxy_id, accounts.proxy_id)
            """,
            (session_name, phone, username, first_name, proxy_id),
        )
        return cursor.lastrowid

    async def get_active_accounts(self) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM accounts WHERE is_active=1 AND is_banned=0 ORDER BY last_used_at ASC"
        )

    async def get_account(self, session_name: str) -> Optional[Dict]:
        return await self._fetchone(
            "SELECT * FROM accounts WHERE session_name=?", (session_name,)
        )

    async def increment_account_invites(self, session_name: str) -> None:
        await self._execute(
            "UPDATE accounts SET invites_today=invites_today+1, last_used_at=datetime('now') WHERE session_name=?",
            (session_name,),
        )

    async def increment_account_dms(self, session_name: str) -> None:
        await self._execute(
            "UPDATE accounts SET dms_today=dms_today+1, last_used_at=datetime('now') WHERE session_name=?",
            (session_name,),
        )

    async def increment_account_error(self, session_name: str) -> int:
        """Increment error streak counter. Returns new streak value."""
        await self._execute(
            "UPDATE accounts SET errors_streak=errors_streak+1 WHERE session_name=?",
            (session_name,),
        )
        row = await self._fetchone(
            "SELECT errors_streak FROM accounts WHERE session_name=?", (session_name,)
        )
        return row["errors_streak"] if row else 0

    async def reset_account_errors(self, session_name: str) -> None:
        await self._execute(
            "UPDATE accounts SET errors_streak=0 WHERE session_name=?",
            (session_name,),
        )

    async def ban_account(self, session_name: str) -> None:
        await self._execute(
            "UPDATE accounts SET is_banned=1, is_active=0 WHERE session_name=?",
            (session_name,),
        )

    async def reset_daily_counters(self) -> None:
        """Reset daily invite/DM counters. Should be called at midnight."""
        await self._execute(
            "UPDATE accounts SET invites_today=0, dms_today=0, last_reset_at=datetime('now')"
        )
        logger.info("Daily account counters reset")

    # ------------------------------------------------------------------
    # Proxies
    # ------------------------------------------------------------------

    async def add_proxy(
        self,
        proxy_type: str,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> int:
        cursor = await self._execute(
            """
            INSERT OR IGNORE INTO proxies(proxy_type, host, port, username, password)
            VALUES(?, ?, ?, ?, ?)
            """,
            (proxy_type, host, port, username, password),
        )
        return cursor.lastrowid

    async def get_active_proxies(self) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM proxies WHERE is_active=1 ORDER BY fail_count ASC"
        )

    async def mark_proxy_failed(self, proxy_id: int) -> None:
        await self._execute(
            "UPDATE proxies SET fail_count=fail_count+1, last_check=datetime('now') WHERE id=?",
            (proxy_id,),
        )

    async def reset_proxy_fails(self, proxy_id: int) -> None:
        await self._execute(
            "UPDATE proxies SET fail_count=0, last_check=datetime('now') WHERE id=?",
            (proxy_id,),
        )

    # ------------------------------------------------------------------
    # Donor channels
    # ------------------------------------------------------------------

    async def add_donor_channel(self, username: str, channel_id: int = None, title: str = None) -> int:
        cursor = await self._execute(
            """
            INSERT OR IGNORE INTO donor_channels(username, channel_id, title)
            VALUES(?, ?, ?)
            """,
            (username, channel_id, title),
        )
        return cursor.lastrowid

    async def remove_donor_channel(self, username: str) -> None:
        await self._execute(
            "UPDATE donor_channels SET is_active=0 WHERE username=?", (username,)
        )

    async def get_active_donor_channels(self) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM donor_channels WHERE is_active=1"
        )

    async def update_donor_last_message(self, username: str, message_id: int) -> None:
        await self._execute(
            """
            UPDATE donor_channels
            SET last_message_id=?, posts_scraped=posts_scraped+1
            WHERE username=?
            """,
            (message_id, username),
        )

    # ------------------------------------------------------------------
    # Reposted content
    # ------------------------------------------------------------------

    async def is_already_reposted(self, donor_channel: str, original_msg_id: int) -> bool:
        row = await self._fetchone(
            "SELECT id FROM reposted_content WHERE donor_channel=? AND original_msg_id=?",
            (donor_channel, original_msg_id),
        )
        return row is not None

    async def mark_as_reposted(
        self,
        donor_channel: str,
        original_msg_id: int,
        file_unique_id: Optional[str] = None,
        target_msg_id: Optional[int] = None,
    ) -> None:
        await self._execute(
            """
            INSERT OR IGNORE INTO reposted_content
                (donor_channel, original_msg_id, file_unique_id, target_msg_id)
            VALUES(?, ?, ?, ?)
            """,
            (donor_channel, original_msg_id, file_unique_id, target_msg_id),
        )

    async def get_repost_count_today(self) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS cnt FROM reposted_content WHERE date(reposted_at)=date('now')"
        )
        return row["cnt"] if row else 0

    # ------------------------------------------------------------------
    # Parsed users
    # ------------------------------------------------------------------

    async def upsert_parsed_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
        last_seen: Optional[str],
        source_group: Optional[str],
        is_bot: bool = False,
    ) -> None:
        await self._execute(
            """
            INSERT INTO parsed_users
                (user_id, username, first_name, last_name, last_seen, source_group, is_bot)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username    = COALESCE(excluded.username, parsed_users.username),
                first_name  = COALESCE(excluded.first_name, parsed_users.first_name),
                last_name   = COALESCE(excluded.last_name, parsed_users.last_name),
                last_seen   = excluded.last_seen,
                source_group= excluded.source_group,
                parsed_at   = datetime('now')
            """,
            (user_id, username, first_name, last_name, last_seen, source_group, int(is_bot)),
        )

    async def get_new_users(self, limit: int = 100) -> List[Dict]:
        """Return users that haven't been invited or DM'd yet."""
        return await self._fetchall(
            """
            SELECT pu.* FROM parsed_users pu
            WHERE pu.status = 'new'
              AND pu.is_bot = 0
              AND pu.user_id NOT IN (
                  SELECT DISTINCT user_id FROM sent_actions
                  WHERE status IN ('sent', 'blocked')
              )
            ORDER BY pu.parsed_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    async def get_parsed_users_count(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS cnt FROM parsed_users WHERE is_bot=0")
        return row["cnt"] if row else 0

    async def mark_user_status(self, user_id: int, status: str) -> None:
        await self._execute(
            "UPDATE parsed_users SET status=? WHERE user_id=?", (status, user_id)
        )

    # ------------------------------------------------------------------
    # Sent actions
    # ------------------------------------------------------------------

    async def record_action(
        self,
        user_id: int,
        username: Optional[str],
        action_type: str,
        account_session: Optional[str],
        status: str = "sent",
        error_text: Optional[str] = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO sent_actions
                (user_id, username, action_type, account_session, status, error_text)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, action_type, account_session, status, error_text),
        )

    async def was_user_contacted(self, user_id: int, action_type: str) -> bool:
        row = await self._fetchone(
            "SELECT id FROM sent_actions WHERE user_id=? AND action_type=? AND status='sent'",
            (user_id, action_type),
        )
        return row is not None

    async def get_actions_count_today(self, action_type: str) -> int:
        row = await self._fetchone(
            """
            SELECT COUNT(*) AS cnt FROM sent_actions
            WHERE action_type=? AND date(sent_at)=date('now') AND status='sent'
            """,
            (action_type,),
        )
        return row["cnt"] if row else 0

    # ------------------------------------------------------------------
    # Source groups
    # ------------------------------------------------------------------

    async def add_source_group(
        self, username: str, group_id: int = None, title: str = None
    ) -> int:
        cursor = await self._execute(
            """
            INSERT OR IGNORE INTO source_groups(username, group_id, title)
            VALUES(?, ?, ?)
            """,
            (username, group_id, title),
        )
        return cursor.lastrowid

    async def remove_source_group(self, username: str) -> None:
        await self._execute(
            "UPDATE source_groups SET is_active=0 WHERE username=?", (username,)
        )

    async def get_active_source_groups(self) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM source_groups WHERE is_active=1"
        )

    async def update_source_group_parsed(self, username: str) -> None:
        await self._execute(
            "UPDATE source_groups SET last_parsed=datetime('now') WHERE username=?",
            (username,),
        )

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    async def log_event(self, level: str, module: str, message: str) -> None:
        await self._execute(
            "INSERT INTO event_log(level, module, message) VALUES(?, ?, ?)",
            (level, module, message),
        )
        # Keep log table manageable — purge entries older than 7 days
        await self._execute(
            "DELETE FROM event_log WHERE logged_at < datetime('now', '-7 days')"
        )

    async def get_recent_events(self, limit: int = 50) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM event_log ORDER BY logged_at DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def get_stats(self) -> Dict[str, Any]:
        """Collect aggregate statistics for the admin dashboard."""
        parsed_total = await self._fetchone(
            "SELECT COUNT(*) AS c FROM parsed_users WHERE is_bot=0"
        )
        invites_total = await self._fetchone(
            "SELECT COUNT(*) AS c FROM sent_actions WHERE action_type='invite' AND status='sent'"
        )
        dms_total = await self._fetchone(
            "SELECT COUNT(*) AS c FROM sent_actions WHERE action_type='dm' AND status='sent'"
        )
        reposts_total = await self._fetchone(
            "SELECT COUNT(*) AS c FROM reposted_content"
        )
        reposts_today = await self._fetchone(
            "SELECT COUNT(*) AS c FROM reposted_content WHERE date(reposted_at)=date('now')"
        )
        invites_today = await self._fetchone(
            "SELECT COUNT(*) AS c FROM sent_actions WHERE action_type='invite' AND status='sent' AND date(sent_at)=date('now')"
        )
        dms_today = await self._fetchone(
            "SELECT COUNT(*) AS c FROM sent_actions WHERE action_type='dm' AND status='sent' AND date(sent_at)=date('now')"
        )
        accounts_active = await self._fetchone(
            "SELECT COUNT(*) AS c FROM accounts WHERE is_active=1 AND is_banned=0"
        )
        accounts_banned = await self._fetchone(
            "SELECT COUNT(*) AS c FROM accounts WHERE is_banned=1"
        )
        return {
            "parsed_total": parsed_total["c"] if parsed_total else 0,
            "invites_total": invites_total["c"] if invites_total else 0,
            "dms_total": dms_total["c"] if dms_total else 0,
            "reposts_total": reposts_total["c"] if reposts_total else 0,
            "reposts_today": reposts_today["c"] if reposts_today else 0,
            "invites_today": invites_today["c"] if invites_today else 0,
            "dms_today": dms_today["c"] if dms_today else 0,
            "accounts_active": accounts_active["c"] if accounts_active else 0,
            "accounts_banned": accounts_banned["c"] if accounts_banned else 0,
        }
