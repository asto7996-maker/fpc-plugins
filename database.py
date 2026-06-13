"""
SQLite-хранилище: пользователи Telegram, заказы, автовыдача, уведомления.
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite

from config import DB_PATH, Settings


class Database:
    """Асинхронная обёртка над SQLite."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or str(DB_PATH)

    async def init(self) -> None:
        """Создаёт таблицы при первом запуске."""
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tg_users (
                    user_id INTEGER PRIMARY KEY,
                    authorized INTEGER DEFAULT 0,
                    failed_attempts INTEGER DEFAULT 0,
                    blocked_until INTEGER DEFAULT 0,
                    notify_orders INTEGER DEFAULT 1,
                    notify_chats INTEGER DEFAULT 1,
                    notify_bump INTEGER DEFAULT 1,
                    notify_auth INTEGER DEFAULT 1,
                    notify_delivery INTEGER DEFAULT 1,
                    language TEXT DEFAULT 'ru',
                    created_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS orders_notified (
                    order_id TEXT PRIMARY KEY,
                    account_name TEXT DEFAULT 'default',
                    notified_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS orders_status (
                    order_id TEXT PRIMARY KEY,
                    status TEXT,
                    account_name TEXT DEFAULT 'default',
                    updated_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS orders_reviewed (
                    order_id TEXT PRIMARY KEY,
                    account_name TEXT DEFAULT 'default',
                    reviewed_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS chat_last_notified (
                    chat_id TEXT PRIMARY KEY,
                    message_id TEXT,
                    account_name TEXT DEFAULT 'default'
                );

                CREATE TABLE IF NOT EXISTS chat_last_user_message (
                    chat_id TEXT PRIMARY KEY,
                    ts INTEGER DEFAULT 0,
                    account_name TEXT DEFAULT 'default'
                );

                CREATE TABLE IF NOT EXISTS chat_ai_cooldown (
                    chat_id TEXT PRIMARY KEY,
                    last_reply_at INTEGER DEFAULT 0,
                    account_name TEXT DEFAULT 'default'
                );

                CREATE TABLE IF NOT EXISTS autodelivery_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product TEXT NOT NULL,
                    content TEXT NOT NULL,
                    used INTEGER DEFAULT 0,
                    created_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS message_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    content TEXT NOT NULL,
                    created_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS feature_flags (
                    key TEXT PRIMARY KEY,
                    value INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    starvell_user_id INTEGER,
                    block_delivery INTEGER DEFAULT 1,
                    block_response INTEGER DEFAULT 1,
                    block_notify INTEGER DEFAULT 0,
                    created_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS auto_response_cmds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT UNIQUE NOT NULL,
                    response TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    notify INTEGER DEFAULT 0,
                    created_at INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS chat_welcomed (
                    chat_id TEXT PRIMARY KEY,
                    welcomed_at INTEGER DEFAULT 0
                );
                """
            )
            await db.commit()

    # ── Пользователи Telegram ─────────────────────────────────────────────

    async def get_user(self, user_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM tg_users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if row:
                return dict(row)
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO tg_users (user_id, created_at) VALUES (?, ?)",
                (user_id, now),
            )
            await db.commit()
        return {
            "user_id": user_id,
            "authorized": 0,
            "failed_attempts": 0,
            "blocked_until": 0,
            "notify_orders": 1,
            "notify_chats": 1,
            "notify_bump": 1,
            "notify_auth": 1,
            "notify_delivery": 1,
            "language": "ru",
        }

    async def set_authorized(self, user_id: int, authorized: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE tg_users SET authorized = ? WHERE user_id = ?",
                (1 if authorized else 0, user_id),
            )
            await db.commit()

    async def increment_failed(self, user_id: int) -> int:
        user = await self.get_user(user_id)
        attempts = int(user.get("failed_attempts", 0)) + 1
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE tg_users SET failed_attempts = ? WHERE user_id = ?",
                (attempts, user_id),
            )
            await db.commit()
        return attempts

    async def reset_failed(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE tg_users SET failed_attempts = 0 WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()

    async def set_blocked_until(self, user_id: int, ts: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE tg_users SET blocked_until = ? WHERE user_id = ?",
                (ts, user_id),
            )
            await db.commit()

    async def toggle_notify(self, user_id: int, field: str) -> bool:
        allowed = {"notify_orders", "notify_chats", "notify_bump", "notify_auth", "notify_delivery"}
        if field not in allowed:
            raise ValueError(f"Unknown notify field: {field}")
        user = await self.get_user(user_id)
        new_val = 0 if int(user.get(field, 1)) else 1
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE tg_users SET {field} = ? WHERE user_id = ?",
                (new_val, user_id),
            )
            await db.commit()
        return bool(new_val)

    async def get_authorized_users(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id FROM tg_users WHERE authorized = 1")
            rows = await cur.fetchall()
        return [int(r[0]) for r in rows]

    # ── Заказы ──────────────────────────────────────────────────────────

    async def is_order_notified(self, order_id: str, account: str = "default") -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM orders_notified WHERE order_id = ? AND account_name = ?",
                (order_id, account),
            )
            return await cur.fetchone() is not None

    async def mark_order_notified(self, order_id: str, account: str = "default") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO orders_notified (order_id, account_name, notified_at) VALUES (?, ?, ?)",
                (order_id, account, int(time.time())),
            )
            await db.commit()

    async def get_order_status(self, order_id: str, account: str = "default") -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT status FROM orders_status WHERE order_id = ? AND account_name = ?",
                (order_id, account),
            )
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_order_status(self, order_id: str, status: str, account: str = "default") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO orders_status (order_id, status, account_name, updated_at) VALUES (?, ?, ?, ?)",
                (order_id, status, account, int(time.time())),
            )
            await db.commit()

    async def is_order_reviewed(self, order_id: str, account: str = "default") -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM orders_reviewed WHERE order_id = ? AND account_name = ?",
                (order_id, account),
            )
            return await cur.fetchone() is not None

    async def mark_order_reviewed(self, order_id: str, account: str = "default") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO orders_reviewed (order_id, account_name, reviewed_at) VALUES (?, ?, ?)",
                (order_id, account, int(time.time())),
            )
            await db.commit()

    # ── Чаты ────────────────────────────────────────────────────────────

    async def get_last_notified_message(self, chat_id: str, account: str = "default") -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT message_id FROM chat_last_notified WHERE chat_id = ? AND account_name = ?",
                (chat_id, account),
            )
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_last_notified_message(self, chat_id: str, message_id: str, account: str = "default") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO chat_last_notified (chat_id, message_id, account_name) VALUES (?, ?, ?)",
                (chat_id, message_id, account),
            )
            await db.commit()

    async def get_chat_last_user_message_at(self, chat_id: str, account: str = "default") -> int | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT ts FROM chat_last_user_message WHERE chat_id = ? AND account_name = ?",
                (chat_id, account),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def set_chat_last_user_message_at(self, chat_id: str, ts: int, account: str = "default") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO chat_last_user_message (chat_id, ts, account_name) VALUES (?, ?, ?)",
                (chat_id, ts, account),
            )
            await db.commit()

    async def get_ai_cooldown(self, chat_id: str, account: str = "default") -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT last_reply_at FROM chat_ai_cooldown WHERE chat_id = ? AND account_name = ?",
                (chat_id, account),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def set_ai_cooldown(self, chat_id: str, account: str = "default") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO chat_ai_cooldown (chat_id, last_reply_at, account_name) VALUES (?, ?, ?)",
                (chat_id, int(time.time()), account),
            )
            await db.commit()

    # ── Автовыдача ──────────────────────────────────────────────────────

    async def add_autodelivery_items(self, product: str, values: list[str]) -> int:
        now = int(time.time())
        added = 0
        async with aiosqlite.connect(self.path) as db:
            for val in values:
                val = val.strip()
                if not val:
                    continue
                await db.execute(
                    "INSERT INTO autodelivery_items (product, content, created_at) VALUES (?, ?, ?)",
                    (product.strip(), val, now),
                )
                added += 1
            await db.commit()
        return added

    async def pop_autodelivery_item(self, product: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, content FROM autodelivery_items WHERE product = ? AND used = 0 ORDER BY id LIMIT 1",
                (product.strip(),),
            )
            row = await cur.fetchone()
            if not row:
                return None
            await db.execute("UPDATE autodelivery_items SET used = 1 WHERE id = ?", (row["id"],))
            await db.commit()
            return str(row["content"])

    async def count_autodelivery(self, product: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM autodelivery_items WHERE product = ? AND used = 0",
                (product.strip(),),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_autodelivery_products(self) -> list[tuple[str, int]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT product, COUNT(*) FROM autodelivery_items
                WHERE used = 0 GROUP BY product ORDER BY product
                """
            )
            rows = await cur.fetchall()
        return [(str(r[0]), int(r[1])) for r in rows]

    async def delete_autodelivery_product(self, product: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM autodelivery_items WHERE product = ? AND used = 0",
                (product.strip(),),
            )
            await db.commit()
            return cur.rowcount

    # ── Флаги функций (глобальные переключатели) ─────────────────────────

    async def sync_feature_flags(self, settings: Settings) -> None:
        flags = {
            "auto_delivery": settings.auto_delivery_enabled,
            "auto_bump": settings.auto_bump_enabled,
            "auto_welcome": settings.auto_welcome_enabled,
            "auto_review": settings.auto_review_enabled,
            "ai_replies": settings.ai_replies_enabled,
            "auto_response": settings.auto_response_enabled,
            "order_confirm": settings.order_confirm_enabled,
        }
        async with aiosqlite.connect(self.path) as db:
            for key, val in flags.items():
                await db.execute(
                    "INSERT OR REPLACE INTO feature_flags (key, value) VALUES (?, ?)",
                    (key, 1 if val else 0),
                )
            await db.commit()

    async def get_feature_flag(self, key: str, default: bool = True) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM feature_flags WHERE key = ?", (key,))
            row = await cur.fetchone()
        if row is None:
            return default
        return bool(row[0])

    async def toggle_feature_flag(self, key: str) -> bool:
        current = await self.get_feature_flag(key)
        new_val = not current
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO feature_flags (key, value) VALUES (?, ?)",
                (key, 1 if new_val else 0),
            )
            await db.commit()
        return new_val

    # ── Чёрный список ─────────────────────────────────────────────────────

    async def add_blacklist(self, username: str = "", starvell_user_id: int | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO blacklist (username, starvell_user_id, created_at) VALUES (?, ?, ?)",
                (username.strip().lower(), starvell_user_id, int(time.time())),
            )
            await db.commit()

    async def remove_blacklist(self, username: str = "", starvell_user_id: int | None = None) -> int:
        async with aiosqlite.connect(self.path) as db:
            if starvell_user_id:
                cur = await db.execute("DELETE FROM blacklist WHERE starvell_user_id = ?", (starvell_user_id,))
            else:
                cur = await db.execute("DELETE FROM blacklist WHERE username = ?", (username.strip().lower(),))
            await db.commit()
            return cur.rowcount

    async def list_blacklist(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM blacklist ORDER BY id")
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def is_blacklisted(
        self, username: str = "", starvell_user_id: int | None = None, check: str = "block_response"
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            if starvell_user_id:
                cur = await db.execute(
                    f"SELECT 1 FROM blacklist WHERE starvell_user_id = ? AND {check} = 1",
                    (starvell_user_id,),
                )
                if await cur.fetchone():
                    return True
            if username:
                cur = await db.execute(
                    f"SELECT 1 FROM blacklist WHERE username = ? AND {check} = 1",
                    (username.strip().lower(),),
                )
                return await cur.fetchone() is not None
        return False

    # ── Автоответчик ──────────────────────────────────────────────────────

    async def add_ar_command(self, command: str, response: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO auto_response_cmds (command, response, enabled, created_at) VALUES (?, ?, 1, ?)",
                (command.strip().lower(), response.strip(), int(time.time())),
            )
            await db.commit()

    async def list_ar_commands(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM auto_response_cmds ORDER BY command")
            return [dict(r) for r in await cur.fetchall()]

    async def delete_ar_command(self, cmd_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM auto_response_cmds WHERE id = ?", (cmd_id,))
            await db.commit()

    async def toggle_ar_command(self, cmd_id: int, field: str) -> bool:
        if field not in ("enabled", "notify"):
            raise ValueError(field)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM auto_response_cmds WHERE id = ?", (cmd_id,))
            row = await cur.fetchone()
            if not row:
                return False
            new_val = 0 if row[field] else 1
            await db.execute(f"UPDATE auto_response_cmds SET {field} = ? WHERE id = ?", (new_val, cmd_id))
            await db.commit()
            return bool(new_val)

    async def find_ar_response(self, text: str) -> dict[str, Any] | None:
        text_l = text.strip().lower()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM auto_response_cmds WHERE enabled = 1")
            for row in await cur.fetchall():
                cmd = row["command"]
                if cmd in text_l or text_l.startswith(cmd):
                    return dict(row)
        return None

    # ── Шаблоны ───────────────────────────────────────────────────────────

    async def list_templates(self, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM message_templates ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in await cur.fetchall()]

    async def add_template(self, content: str, title: str = "") -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO message_templates (title, content, created_at) VALUES (?, ?, ?)",
                (title, content, int(time.time())),
            )
            await db.commit()
            return cur.lastrowid or 0

    async def get_template(self, tpl_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM message_templates WHERE id = ?", (tpl_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_template(self, tpl_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM message_templates WHERE id = ?", (tpl_id,))
            await db.commit()

    async def is_chat_welcomed(self, chat_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT 1 FROM chat_welcomed WHERE chat_id = ?", (chat_id,))
            return await cur.fetchone() is not None

    async def mark_chat_welcomed(self, chat_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO chat_welcomed (chat_id, welcomed_at) VALUES (?, ?)",
                (chat_id, int(time.time())),
            )
            await db.commit()
