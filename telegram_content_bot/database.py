"""
Модуль базы данных.
SQLite хранилище для отслеживания постов, очереди публикаций, статистики и дедупликации.
"""

import asyncio
import hashlib
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PostStatus(str, Enum):
    QUEUED = "queued"
    POSTED = "posted"
    SKIPPED = "skipped"
    FAILED = "failed"
    DUPLICATE = "duplicate"


@dataclass
class PostRecord:
    id: Optional[int] = None
    donor_channel_id: int = 0
    donor_channel_name: str = ""
    original_message_id: int = 0
    media_type: str = ""
    media_path: str = ""
    caption: str = ""
    text_hash: str = ""
    media_hash: str = ""
    status: str = PostStatus.QUEUED.value
    target_message_id: Optional[int] = None
    views_at_capture: int = 0
    created_at: str = ""
    posted_at: Optional[str] = None
    error_message: str = ""
    metadata: str = ""


@dataclass
class DonorRecord:
    id: Optional[int] = None
    channel_id: int = 0
    channel_username: str = ""
    channel_title: str = ""
    enabled: bool = True
    added_at: str = ""
    last_post_id: int = 0
    total_captured: int = 0
    total_posted: int = 0
    total_skipped: int = 0


@dataclass
class StatsSnapshot:
    total_captured: int = 0
    total_posted: int = 0
    total_skipped: int = 0
    total_failed: int = 0
    total_duplicates: int = 0
    queue_size: int = 0
    posts_today: int = 0
    posts_this_week: int = 0
    donors_active: int = 0
    uptime_hours: float = 0.0
    last_post_time: Optional[str] = None
    avg_delay_minutes: float = 0.0
    top_donors: List[Dict[str, Any]] = field(default_factory=list)


class Database:
    """Асинхронная обёртка над SQLite для хранения данных бота."""

    def __init__(self, db_path: str = "bot_database.db"):
        self._db_path = Path(db_path)
        self._lock = asyncio.Lock()
        self._start_time = time.time()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Инициализирует таблицы базы данных."""
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    donor_channel_id INTEGER NOT NULL,
                    donor_channel_name TEXT DEFAULT '',
                    original_message_id INTEGER NOT NULL,
                    media_type TEXT DEFAULT '',
                    media_path TEXT DEFAULT '',
                    caption TEXT DEFAULT '',
                    text_hash TEXT DEFAULT '',
                    media_hash TEXT DEFAULT '',
                    status TEXT DEFAULT 'queued',
                    target_message_id INTEGER,
                    views_at_capture INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    posted_at TEXT,
                    error_message TEXT DEFAULT '',
                    metadata TEXT DEFAULT '',
                    UNIQUE(donor_channel_id, original_message_id)
                );

                CREATE TABLE IF NOT EXISTS donors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER UNIQUE NOT NULL,
                    channel_username TEXT DEFAULT '',
                    channel_title TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    added_at TEXT NOT NULL,
                    last_post_id INTEGER DEFAULT 0,
                    total_captured INTEGER DEFAULT 0,
                    total_posted INTEGER DEFAULT 0,
                    total_skipped INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS text_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hash TEXT UNIQUE NOT NULL,
                    donor_channel_id INTEGER,
                    original_message_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hash TEXT UNIQUE NOT NULL,
                    donor_channel_id INTEGER,
                    original_message_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS error_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    error_type TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    context TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
                CREATE INDEX IF NOT EXISTS idx_posts_donor ON posts(donor_channel_id);
                CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
                CREATE INDEX IF NOT EXISTS idx_posts_text_hash ON posts(text_hash);
                CREATE INDEX IF NOT EXISTS idx_posts_media_hash ON posts(media_hash);
                CREATE INDEX IF NOT EXISTS idx_text_hashes_hash ON text_hashes(hash);
                CREATE INDEX IF NOT EXISTS idx_media_hashes_hash ON media_hashes(hash);
                CREATE INDEX IF NOT EXISTS idx_error_log_created ON error_log(created_at);
            """)
        logger.info("База данных инициализирована: %s", self._db_path)

    async def add_post(self, post: PostRecord) -> int:
        """Добавляет пост в базу данных. Возвращает ID записи."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._add_post_sync, post
            )

    def _add_post_sync(self, post: PostRecord) -> int:
        with self._connection() as conn:
            now = datetime.utcnow().isoformat()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO posts
                (donor_channel_id, donor_channel_name, original_message_id,
                 media_type, media_path, caption, text_hash, media_hash,
                 status, views_at_capture, created_at, error_message, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.donor_channel_id,
                    post.donor_channel_name,
                    post.original_message_id,
                    post.media_type,
                    post.media_path,
                    post.caption,
                    post.text_hash,
                    post.media_hash,
                    post.status,
                    post.views_at_capture,
                    now,
                    post.error_message,
                    post.metadata,
                ),
            )
            return cursor.lastrowid or 0

    async def update_post_status(
        self,
        post_id: int,
        status: PostStatus,
        target_message_id: Optional[int] = None,
        error_message: str = "",
    ) -> None:
        """Обновляет статус поста."""
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._update_post_status_sync,
                post_id,
                status,
                target_message_id,
                error_message,
            )

    def _update_post_status_sync(
        self,
        post_id: int,
        status: PostStatus,
        target_message_id: Optional[int],
        error_message: str,
    ) -> None:
        with self._connection() as conn:
            now = datetime.utcnow().isoformat()
            posted_at = now if status == PostStatus.POSTED else None
            conn.execute(
                """
                UPDATE posts SET status = ?, target_message_id = ?,
                posted_at = COALESCE(?, posted_at),
                error_message = ? WHERE id = ?
                """,
                (status.value, target_message_id, posted_at, error_message, post_id),
            )

    async def get_queue(self, limit: int = 50) -> List[PostRecord]:
        """Возвращает посты из очереди."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._get_queue_sync, limit
            )

    def _get_queue_sync(self, limit: int) -> List[PostRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM posts WHERE status = ? ORDER BY created_at ASC LIMIT ?",
                (PostStatus.QUEUED.value, limit),
            ).fetchall()
            return [self._row_to_post(row) for row in rows]

    async def get_next_queued_post(self) -> Optional[PostRecord]:
        """Возвращает следующий пост из очереди."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._get_next_queued_post_sync
            )

    def _get_next_queued_post_sync(self) -> Optional[PostRecord]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM posts WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                (PostStatus.QUEUED.value,),
            ).fetchone()
            return self._row_to_post(row) if row else None

    async def get_queue_size(self) -> int:
        """Возвращает количество постов в очереди."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._get_queue_size_sync
            )

    def _get_queue_size_sync(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM posts WHERE status = ?",
                (PostStatus.QUEUED.value,),
            ).fetchone()
            return row["cnt"] if row else 0

    async def post_exists(self, donor_channel_id: int, message_id: int) -> bool:
        """Проверяет, существует ли уже пост с данным donor_channel_id + message_id."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._post_exists_sync, donor_channel_id, message_id
            )

    def _post_exists_sync(self, donor_channel_id: int, message_id: int) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM posts WHERE donor_channel_id = ? AND original_message_id = ?",
                (donor_channel_id, message_id),
            ).fetchone()
            return row is not None

    async def is_text_duplicate(self, text_hash: str) -> bool:
        """Проверяет, является ли текст дубликатом по хешу."""
        if not text_hash:
            return False
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._is_text_duplicate_sync, text_hash
            )

    def _is_text_duplicate_sync(self, text_hash: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM text_hashes WHERE hash = ?", (text_hash,)
            ).fetchone()
            return row is not None

    async def add_text_hash(
        self, text_hash: str, donor_channel_id: int, message_id: int
    ) -> None:
        """Добавляет хеш текста для дедупликации."""
        if not text_hash:
            return
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None, self._add_text_hash_sync, text_hash, donor_channel_id, message_id
            )

    def _add_text_hash_sync(
        self, text_hash: str, donor_channel_id: int, message_id: int
    ) -> None:
        with self._connection() as conn:
            now = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO text_hashes (hash, donor_channel_id, original_message_id, created_at) VALUES (?, ?, ?, ?)",
                (text_hash, donor_channel_id, message_id, now),
            )

    async def is_media_duplicate(self, media_hash: str) -> bool:
        """Проверяет, является ли медиафайл дубликатом."""
        if not media_hash:
            return False
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._is_media_duplicate_sync, media_hash
            )

    def _is_media_duplicate_sync(self, media_hash: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM media_hashes WHERE hash = ?", (media_hash,)
            ).fetchone()
            return row is not None

    async def add_media_hash(
        self, media_hash: str, donor_channel_id: int, message_id: int
    ) -> None:
        """Добавляет хеш медиафайла для дедупликации."""
        if not media_hash:
            return
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._add_media_hash_sync,
                media_hash,
                donor_channel_id,
                message_id,
            )

    def _add_media_hash_sync(
        self, media_hash: str, donor_channel_id: int, message_id: int
    ) -> None:
        with self._connection() as conn:
            now = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO media_hashes (hash, donor_channel_id, original_message_id, created_at) VALUES (?, ?, ?, ?)",
                (media_hash, donor_channel_id, message_id, now),
            )

    # --- Доноры ---

    async def add_donor(self, donor: DonorRecord) -> int:
        """Добавляет канал-донор."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._add_donor_sync, donor
            )

    def _add_donor_sync(self, donor: DonorRecord) -> int:
        with self._connection() as conn:
            now = datetime.utcnow().isoformat()
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO donors
                (channel_id, channel_username, channel_title, enabled, added_at,
                 last_post_id, total_captured, total_posted, total_skipped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    donor.channel_id,
                    donor.channel_username,
                    donor.channel_title,
                    int(donor.enabled),
                    donor.added_at or now,
                    donor.last_post_id,
                    donor.total_captured,
                    donor.total_posted,
                    donor.total_skipped,
                ),
            )
            return cursor.lastrowid or 0

    async def get_donor(self, channel_id: int) -> Optional[DonorRecord]:
        """Получает донора по ID канала."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._get_donor_sync, channel_id
            )

    def _get_donor_sync(self, channel_id: int) -> Optional[DonorRecord]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM donors WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            return self._row_to_donor(row) if row else None

    async def get_all_donors(self, enabled_only: bool = False) -> List[DonorRecord]:
        """Возвращает список всех доноров."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._get_all_donors_sync, enabled_only
            )

    def _get_all_donors_sync(self, enabled_only: bool) -> List[DonorRecord]:
        with self._connection() as conn:
            if enabled_only:
                rows = conn.execute(
                    "SELECT * FROM donors WHERE enabled = 1 ORDER BY added_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM donors ORDER BY added_at"
                ).fetchall()
            return [self._row_to_donor(row) for row in rows]

    async def update_donor_last_post(
        self, channel_id: int, last_post_id: int
    ) -> None:
        """Обновляет last_post_id донора."""
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None, self._update_donor_last_post_sync, channel_id, last_post_id
            )

    def _update_donor_last_post_sync(
        self, channel_id: int, last_post_id: int
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE donors SET last_post_id = ? WHERE channel_id = ?",
                (last_post_id, channel_id),
            )

    async def increment_donor_stats(
        self,
        channel_id: int,
        captured: int = 0,
        posted: int = 0,
        skipped: int = 0,
    ) -> None:
        """Увеличивает счётчики статистики донора."""
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._increment_donor_stats_sync,
                channel_id,
                captured,
                posted,
                skipped,
            )

    def _increment_donor_stats_sync(
        self, channel_id: int, captured: int, posted: int, skipped: int
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE donors SET
                    total_captured = total_captured + ?,
                    total_posted = total_posted + ?,
                    total_skipped = total_skipped + ?
                WHERE channel_id = ?
                """,
                (captured, posted, skipped, channel_id),
            )

    async def remove_donor(self, channel_id: int) -> bool:
        """Удаляет канал-донор."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._remove_donor_sync, channel_id
            )

    def _remove_donor_sync(self, channel_id: int) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM donors WHERE channel_id = ?", (channel_id,)
            )
            return cursor.rowcount > 0

    async def toggle_donor(self, channel_id: int, enabled: bool) -> bool:
        """Включает/выключает донора."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._toggle_donor_sync, channel_id, enabled
            )

    def _toggle_donor_sync(self, channel_id: int, enabled: bool) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE donors SET enabled = ? WHERE channel_id = ?",
                (int(enabled), channel_id),
            )
            return cursor.rowcount > 0

    # --- Настройки ---

    async def get_setting(self, key: str, default: str = "") -> str:
        """Получает значение настройки."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._get_setting_sync, key, default
            )

    def _get_setting_sync(self, key: str, default: str) -> str:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        """Устанавливает значение настройки."""
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None, self._set_setting_sync, key, value
            )

    def _set_setting_sync(self, key: str, value: str) -> None:
        with self._connection() as conn:
            now = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )

    # --- Ошибки ---

    async def log_error(
        self, error_type: str, error_message: str, context: str = ""
    ) -> None:
        """Записывает ошибку в лог."""
        async with self._lock:
            await asyncio.get_event_loop().run_in_executor(
                None, self._log_error_sync, error_type, error_message, context
            )

    def _log_error_sync(
        self, error_type: str, error_message: str, context: str
    ) -> None:
        with self._connection() as conn:
            now = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT INTO error_log (error_type, error_message, context, created_at) VALUES (?, ?, ?, ?)",
                (error_type, error_message, context, now),
            )

    async def get_recent_errors(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Возвращает последние ошибки."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._get_recent_errors_sync, limit
            )

    def _get_recent_errors_sync(self, limit: int) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM error_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    # --- Статистика ---

    async def get_stats(self) -> StatsSnapshot:
        """Возвращает снимок статистики."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._get_stats_sync
            )

    def _get_stats_sync(self) -> StatsSnapshot:
        with self._connection() as conn:
            stats = StatsSnapshot()
            now = datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            week_start = (now - timedelta(days=7)).isoformat()

            row = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM posts GROUP BY status"
            ).fetchall()
            for r in row:
                s = r["status"]
                c = r["cnt"]
                if s == PostStatus.QUEUED.value:
                    stats.queue_size = c
                    stats.total_captured += c
                elif s == PostStatus.POSTED.value:
                    stats.total_posted = c
                    stats.total_captured += c
                elif s == PostStatus.SKIPPED.value:
                    stats.total_skipped = c
                    stats.total_captured += c
                elif s == PostStatus.FAILED.value:
                    stats.total_failed = c
                    stats.total_captured += c
                elif s == PostStatus.DUPLICATE.value:
                    stats.total_duplicates = c
                    stats.total_captured += c

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM posts WHERE status = ? AND posted_at >= ?",
                (PostStatus.POSTED.value, today_start),
            ).fetchone()
            stats.posts_today = row["cnt"] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM posts WHERE status = ? AND posted_at >= ?",
                (PostStatus.POSTED.value, week_start),
            ).fetchone()
            stats.posts_this_week = row["cnt"] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM donors WHERE enabled = 1"
            ).fetchone()
            stats.donors_active = row["cnt"] if row else 0

            stats.uptime_hours = round(
                (time.time() - self._start_time) / 3600, 2
            )

            row = conn.execute(
                "SELECT posted_at FROM posts WHERE status = ? ORDER BY posted_at DESC LIMIT 1",
                (PostStatus.POSTED.value,),
            ).fetchone()
            stats.last_post_time = row["posted_at"] if row else None

            top = conn.execute(
                """
                SELECT d.channel_title, d.channel_username, d.total_captured,
                       d.total_posted, d.total_skipped
                FROM donors d WHERE d.enabled = 1
                ORDER BY d.total_posted DESC LIMIT 5
                """
            ).fetchall()
            stats.top_donors = [dict(r) for r in top]

            return stats

    # --- Очистка ---

    async def cleanup_old_records(self, days: int = 30) -> int:
        """Удаляет старые записи. Возвращает количество удалённых записей."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._cleanup_sync, days
            )

    def _cleanup_sync(self, days: int) -> int:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        total = 0
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM posts WHERE status IN (?, ?, ?) AND created_at < ?",
                (
                    PostStatus.POSTED.value,
                    PostStatus.SKIPPED.value,
                    PostStatus.DUPLICATE.value,
                    cutoff,
                ),
            )
            total += cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM text_hashes WHERE created_at < ?", (cutoff,)
            )
            total += cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM media_hashes WHERE created_at < ?", (cutoff,)
            )
            total += cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM error_log WHERE created_at < ?", (cutoff,)
            )
            total += cursor.rowcount
        logger.info("Очистка: удалено %d старых записей (старше %d дней)", total, days)
        return total

    async def clear_queue(self) -> int:
        """Очищает очередь постов."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._clear_queue_sync
            )

    def _clear_queue_sync(self) -> int:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM posts WHERE status = ?", (PostStatus.QUEUED.value,)
            )
            return cursor.rowcount

    # --- Вспомогательные ---

    @staticmethod
    def compute_text_hash(text: str) -> str:
        """Вычисляет хеш текста для дедупликации."""
        if not text:
            return ""
        normalized = " ".join(text.lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def compute_media_hash(data: bytes) -> str:
        """Вычисляет хеш медиафайла."""
        if not data:
            return ""
        return hashlib.sha256(data).hexdigest()[:32]

    def _row_to_post(self, row: sqlite3.Row) -> PostRecord:
        return PostRecord(
            id=row["id"],
            donor_channel_id=row["donor_channel_id"],
            donor_channel_name=row["donor_channel_name"],
            original_message_id=row["original_message_id"],
            media_type=row["media_type"],
            media_path=row["media_path"],
            caption=row["caption"],
            text_hash=row["text_hash"],
            media_hash=row["media_hash"],
            status=row["status"],
            target_message_id=row["target_message_id"],
            views_at_capture=row["views_at_capture"],
            created_at=row["created_at"],
            posted_at=row["posted_at"],
            error_message=row["error_message"],
            metadata=row["metadata"],
        )

    def _row_to_donor(self, row: sqlite3.Row) -> DonorRecord:
        return DonorRecord(
            id=row["id"],
            channel_id=row["channel_id"],
            channel_username=row["channel_username"],
            channel_title=row["channel_title"],
            enabled=bool(row["enabled"]),
            added_at=row["added_at"],
            last_post_id=row["last_post_id"],
            total_captured=row["total_captured"],
            total_posted=row["total_posted"],
            total_skipped=row["total_skipped"],
        )
