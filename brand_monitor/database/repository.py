"""Async SQLite repository for brand monitoring entities."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import aiosqlite

from brand_monitor.database.models import Agent, InteractionLog, Keyword, KnowledgeEntry

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

CREATE TABLE IF NOT EXISTS interaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    keyword_id INTEGER,
    knowledge_base_id INTEGER,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chat_id, message_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (keyword_id) REFERENCES monitored_keywords(id) ON DELETE SET NULL,
    FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_base(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_interaction_chat_msg
    ON interaction_log(chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_keywords_active
    ON monitored_keywords(is_active);
CREATE INDEX IF NOT EXISTS idx_agents_status
    ON agents(status);
"""


class Database:
    """Thin async wrapper around aiosqlite with domain helpers."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
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

    # ------------------------------------------------------------------ agents
    def _row_to_agent(self, row: aiosqlite.Row) -> Agent:
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
        )

    async def get_active_agents(self) -> list[Agent]:
        cursor = await self.conn.execute(
            "SELECT * FROM agents WHERE status = 'active' AND session_string != ''"
        )
        rows = await cursor.fetchall()
        return [self._row_to_agent(r) for r in rows]

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
        cursor = await self.conn.execute(
            """
            INSERT INTO agents (
                phone, session_string, api_id, api_hash,
                proxy_type, proxy_host, proxy_port, proxy_username, proxy_password,
                work_window_start, work_window_end, status, display_name, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(phone) DO UPDATE SET
                session_string=excluded.session_string,
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
            ),
        )
        await self.conn.commit()
        if cursor.lastrowid:
            return int(cursor.lastrowid)
        agent = await self.get_agent_by_phone(phone)
        assert agent is not None
        return agent.id

    async def get_agent_by_phone(self, phone: str) -> Optional[Agent]:
        cursor = await self.conn.execute("SELECT * FROM agents WHERE phone = ?", (phone,))
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def update_agent_status(
        self,
        agent_id: int,
        status: str,
        last_error: Optional[str] = None,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE agents
            SET status = ?, last_error = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (status, last_error, agent_id),
        )
        await self.conn.commit()

    async def update_agent_session(self, agent_id: int, session_string: str) -> None:
        await self.conn.execute(
            """
            UPDATE agents
            SET session_string = ?, status = 'active', last_error = NULL,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (session_string, agent_id),
        )
        await self.conn.commit()

    async def update_agent_schedule(
        self,
        agent_id: int,
        work_window_start: str,
        work_window_end: str,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE agents
            SET work_window_start = ?, work_window_end = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (work_window_start, work_window_end, agent_id),
        )
        await self.conn.commit()

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
        cursor = await self.conn.execute(
            """
            INSERT INTO monitored_keywords (keyword, category, knowledge_base_id)
            VALUES (?, ?, ?)
            """,
            (keyword.strip(), category, knowledge_base_id),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def delete_keyword(self, keyword_id: int) -> None:
        await self.conn.execute("DELETE FROM monitored_keywords WHERE id = ?", (keyword_id,))
        await self.conn.commit()

    async def set_keyword_active(self, keyword_id: int, is_active: bool) -> None:
        await self.conn.execute(
            "UPDATE monitored_keywords SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, keyword_id),
        )
        await self.conn.commit()

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
        cursor = await self.conn.execute(
            """
            INSERT INTO knowledge_base (title, response_template, category)
            VALUES (?, ?, ?)
            """,
            (title, response_template, category),
        )
        await self.conn.commit()
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
        entry = await self.get_knowledge(entry_id)
        # Allow updating inactive entries too
        cursor = await self.conn.execute(
            "SELECT * FROM knowledge_base WHERE id = ?", (entry_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Knowledge entry {entry_id} not found")

        await self.conn.execute(
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
                response_template if response_template is not None else row["response_template"],
                category if category is not None else row["category"],
                (1 if is_active else 0) if is_active is not None else row["is_active"],
                entry_id,
            ),
        )
        await self.conn.commit()
        _ = entry  # silence unused in some type checkers

    async def delete_knowledge(self, entry_id: int) -> None:
        await self.conn.execute("DELETE FROM knowledge_base WHERE id = ?", (entry_id,))
        await self.conn.commit()

    # ---------------------------------------------------------- interaction log
    async def is_message_processed(self, chat_id: int, message_id: int) -> bool:
        cursor = await self.conn.execute(
            """
            SELECT 1 FROM interaction_log
            WHERE chat_id = ? AND message_id = ?
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
    ) -> bool:
        """Atomically claim a message for processing.

        Returns True if this agent won the claim (inserted the row),
        False if another agent already processed it.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            await self.conn.execute(
                """
                INSERT INTO interaction_log
                    (chat_id, message_id, agent_id, keyword_id, knowledge_base_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, message_id, agent_id, keyword_id, knowledge_base_id, timestamp),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

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
        return [
            InteractionLog(
                id=r["id"],
                chat_id=r["chat_id"],
                message_id=r["message_id"],
                agent_id=r["agent_id"],
                keyword_id=r["keyword_id"],
                knowledge_base_id=r["knowledge_base_id"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    async def seed_defaults(self) -> None:
        """Insert demo keyword / KB rows when tables are empty."""
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
                    "Опишите проблему — поможем разобраться.}"
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
