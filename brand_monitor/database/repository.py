"""Async SQLite repository for brand monitoring entities."""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Sequence

import aiosqlite

from brand_monitor.database.models import (
    Agent,
    InteractionLog,
    Keyword,
    KnowledgeEntry,
    StopWord,
)
from brand_monitor.utils.fingerprint import generate_fingerprint

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    session_string TEXT NOT NULL DEFAULT '',
    api_id INTEGER NOT NULL,
    api_hash TEXT NOT NULL,
    proxy_type TEXT,
    proxy_host TEXT,
    proxy_port INTEGER,
    proxy_username TEXT,
    proxy_password TEXT,
    work_window_start TEXT NOT NULL DEFAULT '09:00',
    work_window_end TEXT NOT NULL DEFAULT '21:00',
    status TEXT NOT NULL DEFAULT 'inactive',
    display_name TEXT,
    last_error TEXT,
    device_model TEXT,
    system_version TEXT,
    app_version TEXT,
    lang_code TEXT DEFAULT 'ru',
    cooldown_until TEXT,
    last_action_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS knowledge_base (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    response_template TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS monitored_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category TEXT NOT NULL DEFAULT 'general',
    is_active INTEGER NOT NULL DEFAULT 1,
    knowledge_base_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_base(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS stop_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    keyword_id INTEGER,
    knowledge_base_id INTEGER,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'pending',
    trigger_keyword TEXT,
    reply_text TEXT,
    source_text TEXT,
    UNIQUE(chat_id, message_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (keyword_id) REFERENCES monitored_keywords(id) ON DELETE SET NULL,
    FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_base(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_interaction_chat_msg
    ON interaction_log(chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_interaction_ts
    ON interaction_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_interaction_agent_ts
    ON interaction_log(agent_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_keywords_active
    ON monitored_keywords(is_active);
CREATE INDEX IF NOT EXISTS idx_agents_status
    ON agents(status);
CREATE INDEX IF NOT EXISTS idx_stop_words_active
    ON stop_words(is_active);
"""

_AGENT_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("device_model", "TEXT"),
    ("system_version", "TEXT"),
    ("app_version", "TEXT"),
    ("lang_code", "TEXT DEFAULT 'ru'"),
    ("cooldown_until", "TEXT"),
    ("last_action_at", "TEXT"),
)

_INTERACTION_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("trigger_keyword", "TEXT"),
    ("reply_text", "TEXT"),
    ("source_text", "TEXT"),
)


class Database:
    """Thin async wrapper around aiosqlite with domain helpers."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = __import__("asyncio").Lock()

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._migrate()
        await self._conn.commit()
        logger.info("Database ready at %s", self.db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize critical writes inside an explicit transaction."""
        async with self._write_lock:
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def _table_columns(self, table: str) -> set[str]:
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return {r["name"] for r in rows}

    async def _ensure_columns(self, table: str, columns: Sequence[tuple[str, str]]) -> None:
        existing = await self._table_columns(table)
        for name, decl in columns:
            if name not in existing:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                logger.info("Migrated %s.%s", table, name)

    async def _migrate(self) -> None:
        await self._ensure_columns("agents", _AGENT_EXTRA_COLUMNS)
        await self._ensure_columns("interaction_log", _INTERACTION_EXTRA_COLUMNS)

    # ------------------------------------------------------------------ agents
    def _row_to_agent(self, row: aiosqlite.Row) -> Agent:
        keys = set(row.keys())
        return Agent(
            id=row["id"],
            phone=row["phone"],
            session_string=row["session_string"] or "",
            api_id=row["api_id"],
            api_hash=row["api_hash"],
            proxy_type=row["proxy_type"],
            proxy_host=row["proxy_host"],
            proxy_port=row["proxy_port"],
            proxy_username=row["proxy_username"],
            proxy_password=row["proxy_password"],
            work_window_start=row["work_window_start"],
            work_window_end=row["work_window_end"],
            status=row["status"],
            display_name=row["display_name"],
            last_error=row["last_error"],
            device_model=row["device_model"] if "device_model" in keys else None,
            system_version=row["system_version"] if "system_version" in keys else None,
            app_version=row["app_version"] if "app_version" in keys else None,
            lang_code=row["lang_code"] if "lang_code" in keys else "ru",
            cooldown_until=row["cooldown_until"] if "cooldown_until" in keys else None,
            last_action_at=row["last_action_at"] if "last_action_at" in keys else None,
        )

    async def get_runnable_agents(self) -> list[Agent]:
        cursor = await self.conn.execute(
            """
            SELECT * FROM agents
            WHERE status IN ('active', 'cooldown')
              AND session_string != ''
            """
        )
        rows = await cursor.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def get_active_agents(self) -> list[Agent]:
        return await self.get_runnable_agents()

    async def get_agent(self, agent_id: int) -> Optional[Agent]:
        cursor = await self.conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def list_agents(self) -> list[Agent]:
        cursor = await self.conn.execute("SELECT * FROM agents ORDER BY id")
        rows = await cursor.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def upsert_agent(
        self,
        *,
        phone: str,
        api_id: int,
        api_hash: str,
        session_string: str = "",
        proxy_type: Optional[str] = None,
        proxy_host: Optional[str] = None,
        proxy_port: Optional[int] = None,
        proxy_username: Optional[str] = None,
        proxy_password: Optional[str] = None,
        work_window_start: str = "09:00",
        work_window_end: str = "21:00",
        status: str = "inactive",
        display_name: Optional[str] = None,
    ) -> int:
        fp = generate_fingerprint()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO agents (
                    phone, session_string, api_id, api_hash,
                    proxy_type, proxy_host, proxy_port, proxy_username, proxy_password,
                    work_window_start, work_window_end, status, display_name,
                    device_model, system_version, app_version, lang_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(phone) DO UPDATE SET
                    session_string=CASE
                        WHEN excluded.session_string != '' THEN excluded.session_string
                        ELSE agents.session_string END,
                    api_id=excluded.api_id,
                    api_hash=excluded.api_hash,
                    proxy_type=excluded.proxy_type,
                    proxy_host=excluded.proxy_host,
                    proxy_port=excluded.proxy_port,
                    proxy_username=excluded.proxy_username,
                    proxy_password=excluded.proxy_password,
                    work_window_start=excluded.work_window_start,
                    work_window_end=excluded.work_window_end,
                    status=excluded.status,
                    display_name=COALESCE(excluded.display_name, agents.display_name),
                    updated_at=datetime('now')
                """,
                (
                    phone,
                    session_string,
                    api_id,
                    api_hash,
                    proxy_type,
                    proxy_host,
                    proxy_port,
                    proxy_username,
                    proxy_password,
                    work_window_start,
                    work_window_end,
                    status,
                    display_name,
                    fp.device_model,
                    fp.system_version,
                    fp.app_version,
                    fp.lang_code,
                ),
            )
            if cursor.lastrowid:
                agent_id = int(cursor.lastrowid)
            else:
                row = await (
                    await conn.execute("SELECT id FROM agents WHERE phone = ?", (phone,))
                ).fetchone()
                agent_id = int(row["id"])

        # Ensure fingerprint exists for pre-existing rows
        await self.ensure_fingerprint(agent_id)
        return agent_id

    async def get_agent_by_phone(self, phone: str) -> Optional[Agent]:
        cursor = await self.conn.execute("SELECT * FROM agents WHERE phone = ?", (phone,))
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def ensure_fingerprint(self, agent_id: int) -> Agent:
        agent = await self.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"Agent {agent_id} not found")
        if agent.has_fingerprint:
            return agent
        fp = generate_fingerprint()
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE agents
                SET device_model = ?, system_version = ?, app_version = ?,
                    lang_code = ?, updated_at = datetime('now')
                WHERE id = ? AND (device_model IS NULL OR device_model = '')
                """,
                (fp.device_model, fp.system_version, fp.app_version, fp.lang_code, agent_id),
            )
        refreshed = await self.get_agent(agent_id)
        assert refreshed is not None
        return refreshed

    async def update_agent_status(
        self,
        agent_id: int,
        status: str,
        last_error: Optional[str] = None,
        cooldown_until: Optional[str] = None,
    ) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE agents
                SET status = ?, last_error = ?, cooldown_until = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (status, last_error, cooldown_until, agent_id),
            )

    async def update_agent_session(self, agent_id: int, session_string: str) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE agents
                SET session_string = ?, status = 'active', last_error = NULL,
                    cooldown_until = NULL, updated_at = datetime('now')
                WHERE id = ?
                """,
                (session_string, agent_id),
            )
        await self.ensure_fingerprint(agent_id)

    async def update_agent_schedule(
        self,
        agent_id: int,
        work_window_start: str,
        work_window_end: str,
    ) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE agents
                SET work_window_start = ?, work_window_end = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (work_window_start, work_window_end, agent_id),
            )

    async def touch_last_action(self, agent_id: int) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE agents
                SET last_action_at = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (ts, agent_id),
            )

    async def set_all_agents_paused(self, paused: bool) -> int:
        async with self.transaction() as conn:
            if paused:
                cursor = await conn.execute(
                    """
                    UPDATE agents
                    SET status = 'paused', updated_at = datetime('now')
                    WHERE status IN ('active', 'cooldown')
                    """
                )
            else:
                cursor = await conn.execute(
                    """
                    UPDATE agents
                    SET status = 'active', last_error = NULL, updated_at = datetime('now')
                    WHERE status = 'paused' AND session_string != ''
                    """
                )
            return cursor.rowcount or 0

    # ---------------------------------------------------------------- keywords
    async def get_active_keywords(self) -> list[Keyword]:
        cursor = await self.conn.execute(
            "SELECT * FROM monitored_keywords WHERE is_active = 1 ORDER BY length(keyword) DESC"
        )
        rows = await cursor.fetchall()
        return [
            Keyword(
                id=r["id"],
                keyword=r["keyword"],
                category=r["category"],
                is_active=bool(r["is_active"]),
                knowledge_base_id=r["knowledge_base_id"],
            )
            for r in rows
        ]

    async def list_keywords(self) -> list[Keyword]:
        cursor = await self.conn.execute("SELECT * FROM monitored_keywords ORDER BY id")
        rows = await cursor.fetchall()
        return [
            Keyword(
                id=r["id"],
                keyword=r["keyword"],
                category=r["category"],
                is_active=bool(r["is_active"]),
                knowledge_base_id=r["knowledge_base_id"],
            )
            for r in rows
        ]

    async def add_keyword(
        self,
        keyword: str,
        category: str = "general",
        knowledge_base_id: Optional[int] = None,
    ) -> int:
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO monitored_keywords (keyword, category, knowledge_base_id)
                VALUES (?, ?, ?)
                """,
                (keyword.strip(), category, knowledge_base_id),
            )
            return int(cursor.lastrowid)

    async def delete_keyword(self, keyword_id: int) -> None:
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM monitored_keywords WHERE id = ?", (keyword_id,))

    async def set_keyword_active(self, keyword_id: int, is_active: bool) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE monitored_keywords SET is_active = ? WHERE id = ?",
                (1 if is_active else 0, keyword_id),
            )

    # ------------------------------------------------------------ stop words
    async def get_active_stop_words(self) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT word FROM stop_words WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        return [r["word"] for r in rows]

    async def list_stop_words(self) -> list[StopWord]:
        cursor = await self.conn.execute("SELECT * FROM stop_words ORDER BY id")
        rows = await cursor.fetchall()
        return [
            StopWord(id=r["id"], word=r["word"], is_active=bool(r["is_active"]))
            for r in rows
        ]

    async def add_stop_word(self, word: str) -> int:
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO stop_words (word) VALUES (?)",
                (word.strip().lower(),),
            )
            return int(cursor.lastrowid)

    async def delete_stop_word(self, stop_id: int) -> None:
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM stop_words WHERE id = ?", (stop_id,))

    # ------------------------------------------------------------ knowledge base
    async def list_knowledge(self) -> list[KnowledgeEntry]:
        cursor = await self.conn.execute("SELECT * FROM knowledge_base ORDER BY id")
        rows = await cursor.fetchall()
        return [
            KnowledgeEntry(
                id=r["id"],
                title=r["title"],
                response_template=r["response_template"],
                category=r["category"],
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    async def get_knowledge(self, entry_id: int) -> Optional[KnowledgeEntry]:
        cursor = await self.conn.execute(
            "SELECT * FROM knowledge_base WHERE id = ? AND is_active = 1",
            (entry_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return KnowledgeEntry(
            id=row["id"],
            title=row["title"],
            response_template=row["response_template"],
            category=row["category"],
            is_active=bool(row["is_active"]),
        )

    async def get_knowledge_by_category(self, category: str) -> list[KnowledgeEntry]:
        cursor = await self.conn.execute(
            """
            SELECT * FROM knowledge_base
            WHERE is_active = 1 AND category = ?
            ORDER BY id
            """,
            (category,),
        )
        rows = await cursor.fetchall()
        return [
            KnowledgeEntry(
                id=r["id"],
                title=r["title"],
                response_template=r["response_template"],
                category=r["category"],
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    async def add_knowledge(
        self,
        title: str,
        response_template: str,
        category: str = "general",
    ) -> int:
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO knowledge_base (title, response_template, category)
                VALUES (?, ?, ?)
                """,
                (title, response_template, category),
            )
            return int(cursor.lastrowid)

    async def update_knowledge(
        self,
        entry_id: int,
        *,
        title: Optional[str] = None,
        response_template: Optional[str] = None,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> None:
        cursor = await self.conn.execute(
            "SELECT * FROM knowledge_base WHERE id = ?", (entry_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Knowledge entry {entry_id} not found")

        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE knowledge_base
                SET title = ?,
                    response_template = ?,
                    category = ?,
                    is_active = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    title if title is not None else row["title"],
                    response_template
                    if response_template is not None
                    else row["response_template"],
                    category if category is not None else row["category"],
                    (1 if is_active else 0) if is_active is not None else row["is_active"],
                    entry_id,
                ),
            )

    async def delete_knowledge(self, entry_id: int) -> None:
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM knowledge_base WHERE id = ?", (entry_id,))

    # ---------------------------------------------------------- interaction log
    def _row_to_interaction(self, r: aiosqlite.Row) -> InteractionLog:
        keys = set(r.keys())
        return InteractionLog(
            id=r["id"],
            chat_id=r["chat_id"],
            message_id=r["message_id"],
            agent_id=r["agent_id"],
            keyword_id=r["keyword_id"],
            knowledge_base_id=r["knowledge_base_id"],
            timestamp=r["timestamp"],
            status=r["status"] if "status" in keys else "sent",
            trigger_keyword=r["trigger_keyword"] if "trigger_keyword" in keys else None,
            reply_text=r["reply_text"] if "reply_text" in keys else None,
            source_text=r["source_text"] if "source_text" in keys else None,
        )

    async def is_message_processed(self, chat_id: int, message_id: int) -> bool:
        cursor = await self.conn.execute(
            """
            SELECT 1 FROM interaction_log
            WHERE chat_id = ? AND message_id = ?
              AND status IN ('pending', 'sent')
            LIMIT 1
            """,
            (chat_id, message_id),
        )
        row = await cursor.fetchone()
        return row is not None

    async def try_claim_message(
        self,
        chat_id: int,
        message_id: int,
        agent_id: int,
        keyword_id: Optional[int] = None,
        knowledge_base_id: Optional[int] = None,
        trigger_keyword: Optional[str] = None,
        source_text: Optional[str] = None,
    ) -> bool:
        """Atomically claim a message as pending for this agent."""
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            async with self.transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO interaction_log (
                        chat_id, message_id, agent_id, keyword_id, knowledge_base_id,
                        timestamp, status, trigger_keyword, source_text
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        chat_id,
                        message_id,
                        agent_id,
                        keyword_id,
                        knowledge_base_id,
                        timestamp,
                        trigger_keyword,
                        source_text,
                    ),
                )
            return True
        except aiosqlite.IntegrityError:
            return False

    async def mark_interaction_sent(
        self,
        chat_id: int,
        message_id: int,
        agent_id: int,
        reply_text: str,
    ) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE interaction_log
                SET status = 'sent', reply_text = ?, timestamp = ?
                WHERE chat_id = ? AND message_id = ? AND agent_id = ?
                """,
                (
                    reply_text,
                    datetime.now(timezone.utc).isoformat(),
                    chat_id,
                    message_id,
                    agent_id,
                ),
            )

    async def mark_interaction_cancelled(
        self,
        chat_id: int,
        message_id: int,
        agent_id: int,
    ) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE interaction_log
                SET status = 'cancelled'
                WHERE chat_id = ? AND message_id = ? AND agent_id = ?
                  AND status = 'pending'
                """,
                (chat_id, message_id, agent_id),
            )

    async def count_agent_replies_since(
        self,
        agent_id: int,
        since_iso: str,
    ) -> int:
        cursor = await self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM interaction_log
            WHERE agent_id = ? AND status = 'sent' AND timestamp >= ?
            """,
            (agent_id, since_iso),
        )
        row = await cursor.fetchone()
        return int(row["c"])

    async def get_recent_interactions(self, limit: int = 50) -> list[InteractionLog]:
        cursor = await self.conn.execute(
            """
            SELECT * FROM interaction_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_interaction(r) for r in rows]

    async def get_interactions_between(
        self,
        date_from: str,
        date_to: str,
    ) -> list[InteractionLog]:
        cursor = await self.conn.execute(
            """
            SELECT * FROM interaction_log
            WHERE timestamp >= ? AND timestamp <= ?
              AND status = 'sent'
            ORDER BY timestamp ASC
            """,
            (date_from, date_to),
        )
        rows = await cursor.fetchall()
        return [self._row_to_interaction(r) for r in rows]

    async def stats_by_chat(self, days: int = 7) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            """
            SELECT chat_id, COUNT(*) AS replies
            FROM interaction_log
            WHERE status = 'sent'
              AND timestamp >= datetime('now', ?)
            GROUP BY chat_id
            ORDER BY replies DESC
            LIMIT 30
            """,
            (f"-{int(days)} days",),
        )
        rows = await cursor.fetchall()
        return [{"chat_id": r["chat_id"], "replies": r["replies"]} for r in rows]

    async def stats_by_hour(self, days: int = 7) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            """
            SELECT strftime('%H', timestamp) AS hour, COUNT(*) AS replies
            FROM interaction_log
            WHERE status = 'sent'
              AND timestamp >= datetime('now', ?)
            GROUP BY hour
            ORDER BY hour
            """,
            (f"-{int(days)} days",),
        )
        rows = await cursor.fetchall()
        return [{"hour": r["hour"], "replies": r["replies"]} for r in rows]

    async def export_interactions_csv(
        self,
        date_from: str,
        date_to: str,
    ) -> str:
        rows = await self.get_interactions_between(date_from, date_to)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["timestamp", "agent_id", "chat_id", "message_id", "trigger", "reply_text", "status"]
        )
        for r in rows:
            writer.writerow(
                [
                    r.timestamp,
                    r.agent_id,
                    r.chat_id,
                    r.message_id,
                    r.trigger_keyword or "",
                    (r.reply_text or "").replace("\u200b", ""),
                    r.status,
                ]
            )
        return buf.getvalue()

    async def seed_defaults(self) -> None:
        """Insert demo keyword / KB / stop-word rows when tables are empty."""
        cursor = await self.conn.execute("SELECT COUNT(*) AS c FROM knowledge_base")
        kb_count = (await cursor.fetchone())["c"]
        if kb_count == 0:
            kb_id = await self.add_knowledge(
                title="Brand support greeting",
                category="support",
                response_template=(
                    "{Здравствуйте|Добрый день|Приветствуем}! "
                    "{Я из официальной поддержки|Это официальная поддержка}. "
                    "{Чем могу помочь?|Расскажите, пожалуйста, в чём вопрос?|"
                    "Опишите проблему — {поможем разобраться|подскажем решение}.}"
                ),
            )
        else:
            kb_id = None

        cursor = await self.conn.execute("SELECT COUNT(*) AS c FROM monitored_keywords")
        kw_count = (await cursor.fetchone())["c"]
        if kw_count == 0:
            defaults: Sequence[tuple[str, str]] = (
                ("нужна помощь", "support"),
                ("техподдержка", "support"),
                ("support", "support"),
                ("помогите", "support"),
            )
            for word, category in defaults:
                await self.add_keyword(word, category=category, knowledge_base_id=kb_id)

        cursor = await self.conn.execute("SELECT COUNT(*) AS c FROM stop_words")
        sw_count = (await cursor.fetchone())["c"]
        if sw_count == 0:
            for word in (
                "скам",
                "обман",
                "реклама",
                "продам",
                "работа",
                "казино",
                "ставки",
                "крипта бесплатно",
            ):
                try:
                    await self.add_stop_word(word)
                except aiosqlite.IntegrityError:
                    pass
