"""
database.py — слой работы с SQLite.

Хранит:
  • настройки автопостинга (интервал, лимит, шаблон текста, флаг паузы);
  • progress_id — ID последнего обработанного поста в источнике;
  • состояние планировщика (когда следующий цикл, последняя ошибка);
  • историю успешно пересланных постов.

К базе обращаются два потока (админ-бот aiogram и юзербот Pyrogram),
поэтому включён WAL и все обращения защищены блокировкой + ретраями.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# Ключи настроек в таблице settings
SETTING_CAPTION = "caption_template"
SETTING_INTERVAL_HOURS = "interval_hours"  # legacy, миграция → interval_seconds
SETTING_INTERVAL_SECONDS = "interval_seconds"
SETTING_POSTS_PER_CYCLE = "posts_per_cycle"
SETTING_IS_RUNNING = "is_running"
SETTING_PROGRESS_ID = "progress_id"
SETTING_SOURCE_CHANNEL = "source_channel"
SETTING_TARGET_CHANNEL = "target_channel"
SETTING_START_LINK = "start_link"
SETTING_CATCHUP = "catchup_enabled"
SETTING_CATCHUP_SECONDS = "catchup_interval_seconds"
SETTING_NOTIFY_CYCLES = "notify_cycles"
SETTING_CLEAN_TRANSFER = "clean_transfer"
SETTING_ACTIVE_JOB = "active_job_id"

# Состояние планировщика (не настройки — сбрасывается по ходу работы)
STATE_NEXT_RUN_AT = "next_run_at"
STATE_LAST_CYCLE_AT = "last_cycle_at"
STATE_LAST_PUBLISHED_AT = "last_published_at"
STATE_LAST_ERROR = "last_error"
STATE_LAST_ERROR_AT = "last_error_at"
STATE_LATEST_SOURCE_ID = "latest_source_id"
STATE_NEXT_RUN_REASON = "next_run_reason"
STATE_SCHEDULER_TICK = "scheduler_tick_at"

DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_CATCHUP_SECONDS = 60.0
# Ниже этого интервала Telegram начинает ограничивать аккаунт
MIN_INTERVAL_SECONDS = 5.0
# Сколько независимых пар «источник → назначение» можно держать сразу
MAX_JOBS = 16

# Поля окна ↔ ключи settings (зеркало активного окна + миграция)
_JOB_KV = {
    "caption_template": SETTING_CAPTION,
    "interval_seconds": SETTING_INTERVAL_SECONDS,
    "posts_per_cycle": SETTING_POSTS_PER_CYCLE,
    "is_running": SETTING_IS_RUNNING,
    "progress_id": SETTING_PROGRESS_ID,
    "source_channel": SETTING_SOURCE_CHANNEL,
    "target_channel": SETTING_TARGET_CHANNEL,
    "start_link": SETTING_START_LINK,
    "catchup_enabled": SETTING_CATCHUP,
    "catchup_seconds": SETTING_CATCHUP_SECONDS,
    "notify_cycles": SETTING_NOTIFY_CYCLES,
    "clean_transfer": SETTING_CLEAN_TRANSFER,
    "next_run_at": STATE_NEXT_RUN_AT,
    "next_run_reason": STATE_NEXT_RUN_REASON,
    "last_cycle_at": STATE_LAST_CYCLE_AT,
    "last_published_at": STATE_LAST_PUBLISHED_AT,
    "last_error": STATE_LAST_ERROR,
    "last_error_at": STATE_LAST_ERROR_AT,
    "latest_source_id": STATE_LATEST_SOURCE_ID,
}
_KV_JOB = {v: k for k, v in _JOB_KV.items()}

_BUSY_TIMEOUT_MS = 15000
_SQLITE_TIMEOUT = 30.0


@dataclass
class Settings:
    """Снимок всех настроек автопостинга."""

    caption_template: str
    interval_seconds: float
    posts_per_cycle: int
    is_running: bool
    progress_id: int
    source_channel: str
    target_channel: str
    start_link: str
    catchup_enabled: bool
    catchup_seconds: float
    notify_cycles: bool
    clean_transfer: bool
    job_id: int = 1
    title: str = ""
    next_run_at: float = 0.0
    next_run_reason: str = ""
    last_cycle_at: float = 0.0
    last_published_at: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    latest_source_id: int = 0

    @property
    def interval_hours(self) -> float:
        return self.interval_seconds / 3600.0

    def pair_label(self) -> str:
        """Короткая подпись окна: @src → @dst."""
        from links import format_channel_label

        src = format_channel_label(self.source_channel) if self.source_channel else "—"
        dst = format_channel_label(self.target_channel) if self.target_channel else "—"
        return f"{src} → {dst}"

    def window_title(self) -> str:
        return (self.title or "").strip() or f"Окно {self.job_id}"

    def is_ready(self) -> bool:
        """Можно стартовать: есть оба канала и точка прогресса."""
        return bool(self.source_channel and self.target_channel and self.progress_id >= 0)


class Database:
    """Простая обёртка над SQLite без внешних ORM."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """
        Соединение с row_factory=Row.

        Блокировка процесса + WAL + busy_timeout: к базе одновременно
        обращаются admin-loop (aiogram) и worker-loop (Pyrogram) из разных
        потоков, иначе легко получить «database is locked».
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=_SQLITE_TIMEOUT)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        """Создать таблицы при первом запуске."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_message_id INTEGER NOT NULL,
                    target_message_id INTEGER,
                    grouped_id        TEXT,
                    status            TEXT NOT NULL DEFAULT 'ok',
                    error             TEXT,
                    created_at        TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_history_source
                    ON history(source_message_id);
                CREATE INDEX IF NOT EXISTS idx_history_group
                    ON history(grouped_id);
                CREATE INDEX IF NOT EXISTS idx_history_status
                    ON history(status);

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    caption_template TEXT NOT NULL DEFAULT '',
                    interval_seconds REAL NOT NULL DEFAULT 3600,
                    posts_per_cycle INTEGER NOT NULL DEFAULT 3,
                    is_running INTEGER NOT NULL DEFAULT 0,
                    progress_id INTEGER NOT NULL DEFAULT 0,
                    source_channel TEXT NOT NULL DEFAULT '',
                    target_channel TEXT NOT NULL DEFAULT '',
                    start_link TEXT NOT NULL DEFAULT '',
                    catchup_enabled INTEGER NOT NULL DEFAULT 0,
                    catchup_seconds REAL NOT NULL DEFAULT 60,
                    notify_cycles INTEGER NOT NULL DEFAULT 0,
                    clean_transfer INTEGER NOT NULL DEFAULT 1,
                    next_run_at REAL NOT NULL DEFAULT 0,
                    next_run_reason TEXT NOT NULL DEFAULT '',
                    last_cycle_at REAL NOT NULL DEFAULT 0,
                    last_published_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_error_at REAL NOT NULL DEFAULT 0,
                    latest_source_id INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS lobby_mailings (
                    peer_id INTEGER PRIMARY KEY,
                    sent_at TEXT NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS lobby_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    tariff TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    price REAL NOT NULL,
                    receipt_file_id TEXT NOT NULL DEFAULT '',
                    receipt_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reject_reason TEXT NOT NULL DEFAULT '',
                    granted_days INTEGER NOT NULL DEFAULT 0,
                    shop_id TEXT NOT NULL DEFAULT '',
                    forensic_notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lobby_claims_user
                    ON lobby_claims(user_id);
                CREATE INDEX IF NOT EXISTS idx_lobby_claims_status
                    ON lobby_claims(status);

                CREATE TABLE IF NOT EXISTS shop_tariffs (
                    shop_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    short_name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS support_sessions (
                    peer_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    shop_id TEXT NOT NULL DEFAULT '',
                    tariff TEXT NOT NULL DEFAULT '',
                    duration_days INTEGER NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    last_msg_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            cols = [r[1] for r in conn.execute("PRAGMA table_info(history)")]
            if "job_id" not in cols:
                conn.execute(
                    "ALTER TABLE history ADD COLUMN job_id INTEGER NOT NULL DEFAULT 1"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_job_source "
                "ON history(job_id, source_message_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_job_group "
                "ON history(job_id, grouped_id)"
            )
            claim_cols = [r[1] for r in conn.execute("PRAGMA table_info(lobby_claims)")]
            if claim_cols and "shop_id" not in claim_cols:
                conn.execute(
                    "ALTER TABLE lobby_claims ADD COLUMN shop_id TEXT NOT NULL DEFAULT ''"
                )
            if claim_cols and "forensic_notes" not in claim_cols:
                conn.execute(
                    "ALTER TABLE lobby_claims ADD COLUMN forensic_notes TEXT NOT NULL DEFAULT ''"
                )

    # ------------------------------------------------------------------
    # Низкоуровневые get / set
    # ------------------------------------------------------------------

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
        col = _KV_JOB.get(key)
        if col and self._job_count():
            self._update_job_col(self._current_job_id(), col, value)

    def get_float(self, key: str, default: float) -> float:
        raw = self.get(key)
        try:
            return float(raw) if raw not in (None, "") else float(default)
        except (TypeError, ValueError):
            return float(default)

    def get_int(self, key: str, default: int) -> int:
        raw = self.get(key)
        try:
            return int(float(raw)) if raw not in (None, "") else int(default)
        except (TypeError, ValueError):
            return int(default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        raw = (self.get(key) or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        return default

    def ensure_defaults(
        self,
        *,
        caption: str,
        posts_per_cycle: int,
        source_channel: str,
        target_channel: str,
        interval_seconds: Optional[float] = None,
        interval_hours: Optional[float] = None,
        catchup_seconds: float = DEFAULT_CATCHUP_SECONDS,
    ) -> None:
        """Записать значения по умолчанию, если ключ ещё не существует."""
        if interval_seconds is None:
            interval_seconds = (
                float(interval_hours) * 3600.0
                if interval_hours is not None
                else DEFAULT_INTERVAL_SECONDS
            )
        defaults = {
            SETTING_CAPTION: caption,
            SETTING_INTERVAL_SECONDS: str(float(interval_seconds)),
            SETTING_POSTS_PER_CYCLE: str(posts_per_cycle),
            SETTING_IS_RUNNING: "0",
            SETTING_PROGRESS_ID: "0",
            SETTING_SOURCE_CHANNEL: source_channel,
            SETTING_TARGET_CHANNEL: target_channel,
            SETTING_START_LINK: "",
            SETTING_CATCHUP: "0",
            SETTING_CATCHUP_SECONDS: str(float(catchup_seconds)),
            SETTING_NOTIFY_CYCLES: "0",
            SETTING_CLEAN_TRANSFER: "1",
        }
        with self._connect() as conn:
            for key, value in defaults.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO settings(key, value)
                    VALUES(?, ?)
                    """,
                    (key, value),
                )
        self.migrate_interval()
        self.migrate_jobs()

    def migrate_interval(self) -> None:
        """Старая настройка interval_hours → секунды (одноразово)."""
        legacy = self.get(SETTING_INTERVAL_HOURS)
        if legacy in (None, ""):
            return
        try:
            hours = float(legacy)
        except (TypeError, ValueError):
            hours = 0.0
        if hours > 0:
            seconds = max(MIN_INTERVAL_SECONDS, hours * 3600.0)
            self.set(SETTING_INTERVAL_SECONDS, seconds)
            logger.info(
                "Миграция интервала: %.4f ч → %.0f сек", hours, seconds
            )
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM settings WHERE key = ?", (SETTING_INTERVAL_HOURS,)
            )

    # ------------------------------------------------------------------
    # Окна перелива (несколько пар источник → назначение)
    # ------------------------------------------------------------------

    def _job_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()
        return int(row["c"]) if row else 0

    def _current_job_id(self) -> int:
        override = getattr(self._tls, "job_id", None)
        if override is not None:
            return int(override)
        raw = self.get(SETTING_ACTIVE_JOB)
        try:
            jid = int(raw) if raw not in (None, "") else 0
        except (TypeError, ValueError):
            jid = 0
        if jid and self._job_exists(jid):
            return jid
        ids = self.list_job_ids()
        return ids[0] if ids else 1

    def _job_exists(self, job_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return row is not None

    def list_job_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM jobs ORDER BY id").fetchall()
        return [int(r["id"]) for r in rows]

    @contextmanager
    def job_scope(self, job_id: int) -> Iterator[None]:
        prev = getattr(self._tls, "job_id", None)
        self._tls.job_id = int(job_id)
        try:
            yield
        finally:
            if prev is None:
                if hasattr(self._tls, "job_id"):
                    delattr(self._tls, "job_id")
            else:
                self._tls.job_id = prev

    def _update_job_col(self, job_id: int, column: str, value: Any) -> None:
        if column not in _JOB_KV:
            return
        raw = value
        if column in (
            "is_running",
            "catchup_enabled",
            "notify_cycles",
            "clean_transfer",
        ):
            if isinstance(value, bool):
                raw = 1 if value else 0
            elif str(value).strip().lower() in ("1", "true", "yes", "on"):
                raw = 1
            elif str(value).strip().lower() in ("0", "false", "no", "off", ""):
                raw = 0
        with self._connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {column} = ? WHERE id = ?",
                (raw, int(job_id)),
            )

    def _settings_from_job_row(self, row: sqlite3.Row) -> Settings:
        def _b(val: Any, default: bool = False) -> bool:
            if val in (None, ""):
                return default
            if isinstance(val, (int, float)):
                return bool(int(val))
            raw = str(val).strip().lower()
            if raw in ("1", "true", "yes", "on"):
                return True
            if raw in ("0", "false", "no", "off"):
                return False
            return default

        def _f(val: Any, default: float) -> float:
            try:
                return float(val) if val not in (None, "") else float(default)
            except (TypeError, ValueError):
                return float(default)

        def _i(val: Any, default: int) -> int:
            try:
                return int(float(val)) if val not in (None, "") else int(default)
            except (TypeError, ValueError):
                return int(default)

        return Settings(
            caption_template=row["caption_template"] or "",
            interval_seconds=max(
                MIN_INTERVAL_SECONDS, _f(row["interval_seconds"], DEFAULT_INTERVAL_SECONDS)
            ),
            posts_per_cycle=max(1, _i(row["posts_per_cycle"], 5)),
            is_running=_b(row["is_running"], False),
            progress_id=_i(row["progress_id"], 0),
            source_channel=row["source_channel"] or "",
            target_channel=row["target_channel"] or "",
            start_link=row["start_link"] or "",
            catchup_enabled=_b(row["catchup_enabled"], False),
            catchup_seconds=max(
                MIN_INTERVAL_SECONDS, _f(row["catchup_seconds"], DEFAULT_CATCHUP_SECONDS)
            ),
            notify_cycles=_b(row["notify_cycles"], False),
            clean_transfer=_b(row["clean_transfer"], True),
            job_id=int(row["id"]),
            title=row["title"] or "",
            next_run_at=_f(row["next_run_at"], 0.0),
            next_run_reason=row["next_run_reason"] or "",
            last_cycle_at=_f(row["last_cycle_at"], 0.0),
            last_published_at=_f(row["last_published_at"], 0.0),
            last_error=row["last_error"] or "",
            last_error_at=_f(row["last_error_at"], 0.0),
            latest_source_id=_i(row["latest_source_id"], 0),
        )

    def _mirror_job_to_kv(self, job: Settings) -> None:
        """Зеркало активного окна в settings — для старых ключей и тестов."""
        pairs = {
            SETTING_CAPTION: job.caption_template,
            SETTING_INTERVAL_SECONDS: str(float(job.interval_seconds)),
            SETTING_POSTS_PER_CYCLE: str(job.posts_per_cycle),
            SETTING_IS_RUNNING: "1" if job.is_running else "0",
            SETTING_PROGRESS_ID: str(job.progress_id),
            SETTING_SOURCE_CHANNEL: job.source_channel,
            SETTING_TARGET_CHANNEL: job.target_channel,
            SETTING_START_LINK: job.start_link,
            SETTING_CATCHUP: "1" if job.catchup_enabled else "0",
            SETTING_CATCHUP_SECONDS: str(float(job.catchup_seconds)),
            SETTING_NOTIFY_CYCLES: "1" if job.notify_cycles else "0",
            SETTING_CLEAN_TRANSFER: "1" if job.clean_transfer else "0",
            STATE_NEXT_RUN_AT: f"{job.next_run_at:.0f}",
            STATE_NEXT_RUN_REASON: job.next_run_reason,
            STATE_LAST_CYCLE_AT: f"{job.last_cycle_at:.0f}",
            STATE_LAST_PUBLISHED_AT: f"{job.last_published_at:.0f}",
            STATE_LAST_ERROR: job.last_error,
            STATE_LAST_ERROR_AT: f"{job.last_error_at:.0f}",
            STATE_LATEST_SOURCE_ID: str(job.latest_source_id),
            SETTING_ACTIVE_JOB: str(job.job_id),
        }
        with self._connect() as conn:
            for key, value in pairs.items():
                conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, str(value)),
                )

    def migrate_jobs(self) -> None:
        """Первый запуск / апгрейд: одно текущее окно из старых settings."""
        if self._job_count():
            return
        kv = {k: self.get(k) for k in _KV_JOB}
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs(
                    title, caption_template, interval_seconds, posts_per_cycle,
                    is_running, progress_id, source_channel, target_channel,
                    start_link, catchup_enabled, catchup_seconds, notify_cycles,
                    clean_transfer, next_run_at, next_run_reason, last_cycle_at,
                    last_published_at, last_error, last_error_at, latest_source_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Окно 1",
                    kv.get(SETTING_CAPTION) or "",
                    float(kv.get(SETTING_INTERVAL_SECONDS) or DEFAULT_INTERVAL_SECONDS),
                    int(float(kv.get(SETTING_POSTS_PER_CYCLE) or 3)),
                    1 if (kv.get(SETTING_IS_RUNNING) or "0") in ("1", "true", "yes", "on") else 0,
                    int(float(kv.get(SETTING_PROGRESS_ID) or 0)),
                    kv.get(SETTING_SOURCE_CHANNEL) or "",
                    kv.get(SETTING_TARGET_CHANNEL) or "",
                    kv.get(SETTING_START_LINK) or "",
                    1 if (kv.get(SETTING_CATCHUP) or "0") in ("1", "true", "yes", "on") else 0,
                    float(kv.get(SETTING_CATCHUP_SECONDS) or DEFAULT_CATCHUP_SECONDS),
                    1 if (kv.get(SETTING_NOTIFY_CYCLES) or "0") in ("1", "true", "yes", "on") else 0,
                    0 if (kv.get(SETTING_CLEAN_TRANSFER) or "1") in ("0", "false", "no", "off") else 1,
                    float(kv.get(STATE_NEXT_RUN_AT) or 0),
                    kv.get(STATE_NEXT_RUN_REASON) or "",
                    float(kv.get(STATE_LAST_CYCLE_AT) or 0),
                    float(kv.get(STATE_LAST_PUBLISHED_AT) or 0),
                    kv.get(STATE_LAST_ERROR) or "",
                    float(kv.get(STATE_LAST_ERROR_AT) or 0),
                    int(float(kv.get(STATE_LATEST_SOURCE_ID) or 0)),
                ),
            )
            job_id = int(cur.lastrowid or 1)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (SETTING_ACTIVE_JOB, str(job_id)),
            )
        logger.info("Окна перелива: создано окно %s из текущих настроек", job_id)

    def list_jobs(self) -> list[Settings]:
        self.migrate_jobs()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [self._settings_from_job_row(r) for r in rows]

    def get_job(self, job_id: int) -> Optional[Settings]:
        self.migrate_jobs()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return self._settings_from_job_row(row) if row else None

    def set_active_job(self, job_id: int) -> Settings:
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Нет окна {job_id}")
        self._mirror_job_to_kv(job)
        return job

    def any_job_running(self) -> bool:
        return any(j.is_running for j in self.list_jobs())

    def due_jobs(self, now: float) -> list[Settings]:
        """Окна, которые пора крутить (включены и next_run_at наступил)."""
        running = [j for j in self.list_jobs() if j.is_running]
        due = [j for j in running if j.next_run_at <= now]
        due.sort(key=lambda j: (j.next_run_at, j.job_id))
        return due

    def start_ready_jobs(self) -> tuple[list[int], list[int]]:
        """Включить автопост у всех окон с каналами. (стартовали, пропущены)."""
        started: list[int] = []
        skipped: list[int] = []
        for job in self.list_jobs():
            if not job.is_ready():
                skipped.append(job.job_id)
                continue
            with self.job_scope(job.job_id):
                self.set_running(True)
                self.clear_last_error()
                self.run_asap()
            started.append(job.job_id)
        return started, skipped

    def pause_all_jobs(self) -> list[int]:
        """Поставить на паузу все окна. Возвращает id, которые были включены."""
        paused: list[int] = []
        for job in self.list_jobs():
            if not job.is_running:
                continue
            with self.job_scope(job.job_id):
                self.set_running(False)
            paused.append(job.job_id)
        return paused

    def clone_job(self, job_id: Optional[int] = None) -> Settings:
        """Новое окно с тем же источником и настройками, назначение пустое."""
        src = self.get_job(int(job_id)) if job_id else self.get_settings()
        if src is None:
            src = self.get_settings()
        created = self.create_job(title="")
        with self.job_scope(created.job_id):
            if src.source_channel:
                self.set_source_channel(src.source_channel)
            self.set_caption(src.caption_template)
            self.set_interval_seconds(src.interval_seconds)
            self.set_posts_per_cycle(src.posts_per_cycle)
            self.set_catchup(src.catchup_enabled)
            self.set_catchup_seconds(src.catchup_seconds)
            self.set_notify_cycles(src.notify_cycles)
            self.set_clean_transfer(src.clean_transfer)
        job = self.get_job(created.job_id)
        if job is None:
            raise RuntimeError("Не удалось клонировать окно")
        logger.info(
            "Клон окна %s → %s (источник %s)",
            src.job_id,
            job.job_id,
            src.source_channel or "—",
        )
        return job

    def create_job(self, *, title: str = "") -> Settings:
        if self._job_count() >= MAX_JOBS:
            raise ValueError(f"Максимум {MAX_JOBS} окон")
        src = self.get_settings()
        n = self._job_count() + 1
        name = (title or "").strip() or f"Окно {n}"
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs(
                    title, caption_template, interval_seconds, posts_per_cycle,
                    is_running, progress_id, source_channel, target_channel,
                    start_link, catchup_enabled, catchup_seconds, notify_cycles,
                    clean_transfer
                ) VALUES (?, ?, ?, ?, 0, 0, '', '', '', ?, ?, ?, ?)
                """,
                (
                    name,
                    src.caption_template,
                    src.interval_seconds,
                    src.posts_per_cycle,
                    1 if src.catchup_enabled else 0,
                    src.catchup_seconds,
                    1 if src.notify_cycles else 0,
                    1 if src.clean_transfer else 0,
                ),
            )
            job_id = int(cur.lastrowid or 0)
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError("Не удалось создать окно")
        logger.info("Создано окно %s (%s)", job_id, name)
        return job

    def delete_job(self, job_id: int) -> None:
        if self._job_count() <= 1:
            raise ValueError("Нельзя удалить последнее окно")
        if not self._job_exists(job_id):
            raise ValueError(f"Нет окна {job_id}")
        with self._connect() as conn:
            conn.execute("DELETE FROM history WHERE job_id = ?", (int(job_id),))
            conn.execute("DELETE FROM jobs WHERE id = ?", (int(job_id),))
        remaining = self.list_job_ids()
        if remaining:
            self.set_active_job(remaining[0])
        logger.info("Удалено окно %s", job_id)

    def set_job_title(self, title: str, job_id: Optional[int] = None) -> None:
        jid = int(job_id or self._current_job_id())
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET title = ? WHERE id = ?", (title.strip(), jid))

    # ------------------------------------------------------------------
    # Типизированные хелперы
    # ------------------------------------------------------------------

    def get_settings(self) -> Settings:
        """Снимок текущего (активного) окна перелива."""
        self.migrate_jobs()
        job = self.get_job(self._current_job_id())
        if job is None:
            self.migrate_jobs()
            job = self.get_job(self._current_job_id())
        if job is None:
            raise RuntimeError("Нет окон перелива")
        return job

    def set_caption(self, text: str) -> None:
        self.set(SETTING_CAPTION, text)

    def set_interval_seconds(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("Интервал должен быть больше 0")
        self.set(SETTING_INTERVAL_SECONDS, max(MIN_INTERVAL_SECONDS, float(seconds)))

    def set_interval_hours(self, hours: float) -> None:
        if hours <= 0:
            raise ValueError("Интервал должен быть больше 0")
        self.set_interval_seconds(float(hours) * 3600.0)

    def set_catchup(self, enabled: bool) -> None:
        self.set(SETTING_CATCHUP, "1" if enabled else "0")

    def set_catchup_seconds(self, seconds: float) -> None:
        self.set(SETTING_CATCHUP_SECONDS, max(MIN_INTERVAL_SECONDS, float(seconds)))

    def set_notify_cycles(self, enabled: bool) -> None:
        self.set(SETTING_NOTIFY_CYCLES, "1" if enabled else "0")

    def set_clean_transfer(self, enabled: bool) -> None:
        self.set(SETTING_CLEAN_TRANSFER, "1" if enabled else "0")

    def set_posts_per_cycle(self, count: int) -> None:
        if count < 1:
            raise ValueError("Количество постов за цикл должно быть ≥ 1")
        self.set(SETTING_POSTS_PER_CYCLE, count)

    def set_running(self, running: bool) -> None:
        self.set(SETTING_IS_RUNNING, "1" if running else "0")

    def get_progress_id(self) -> int:
        return self.get_settings().progress_id

    def set_progress_id(self, message_id: int) -> None:
        """Сохранить ID последнего обработанного поста (для продолжения после рестарта)."""
        self.set(SETTING_PROGRESS_ID, message_id)

    def set_start_link(self, link: str) -> None:
        self.set(SETTING_START_LINK, link)

    def set_source_channel(self, channel: str) -> None:
        from links import normalize_channel

        self.set(SETTING_SOURCE_CHANNEL, normalize_channel(channel))

    def set_target_channel(self, channel: str) -> None:
        from links import normalize_channel

        self.set(SETTING_TARGET_CHANNEL, normalize_channel(channel))

    # ------------------------------------------------------------------
    # Состояние планировщика
    # ------------------------------------------------------------------

    def set_next_run(self, unix_ts: float, reason: str = "") -> None:
        self.set(STATE_NEXT_RUN_AT, f"{float(unix_ts):.0f}")
        if reason:
            self.set(STATE_NEXT_RUN_REASON, reason)

    def get_next_run(self) -> float:
        return self.get_settings().next_run_at

    def get_next_run_reason(self) -> str:
        return self.get_settings().next_run_reason

    def run_asap(self) -> None:
        """Запросить цикл при первой возможности (кнопка ▶️ Старт и т.п.)."""
        self.set_next_run(0.0, "asap")

    def mark_cycle(self, published: int) -> None:
        now = time.time()
        self.set(STATE_LAST_CYCLE_AT, f"{now:.0f}")
        if published > 0:
            self.set(STATE_LAST_PUBLISHED_AT, f"{now:.0f}")

    def get_last_cycle_at(self) -> float:
        return self.get_settings().last_cycle_at

    def get_last_published_at(self) -> float:
        return self.get_settings().last_published_at

    def set_last_error(self, text: str) -> None:
        self.set(STATE_LAST_ERROR, text or "")
        self.set(STATE_LAST_ERROR_AT, f"{time.time():.0f}")

    def clear_last_error(self) -> None:
        self.set(STATE_LAST_ERROR, "")

    def get_last_error(self) -> tuple[str, float]:
        s = self.get_settings()
        return (s.last_error, s.last_error_at)

    def mark_scheduler_tick(self) -> None:
        """Отметка живости планировщика — видно в диагностике панели."""
        self.set(STATE_SCHEDULER_TICK, f"{time.time():.0f}")

    def scheduler_age(self) -> Optional[float]:
        """Сколько секунд назад планировщик подавал признаки жизни."""
        ts = self.get_float(STATE_SCHEDULER_TICK, 0.0)
        if ts <= 0:
            return None
        return max(0.0, time.time() - ts)

    def set_latest_source_id(self, message_id: int) -> None:
        self.set(STATE_LATEST_SOURCE_ID, int(message_id))

    def get_latest_source_id(self) -> int:
        return self.get_settings().latest_source_id

    def backlog(self) -> int:
        """Сколько ID источника ещё впереди (грубая оценка очереди)."""
        s = self.get_settings()
        return max(0, s.latest_source_id - s.progress_id)

    # ------------------------------------------------------------------
    # История
    # ------------------------------------------------------------------

    def add_history(
        self,
        source_message_id: int,
        target_message_id: Optional[int] = None,
        grouped_id: Optional[str] = None,
        status: str = "ok",
        error: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        job_id = self._current_job_id()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO history(
                    source_message_id, target_message_id, grouped_id,
                    status, error, created_at, job_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_message_id,
                    target_message_id,
                    grouped_id,
                    status,
                    error,
                    now,
                    job_id,
                ),
            )

    def was_processed(self, source_message_id: int) -> bool:
        """Проверить, есть ли пост уже в истории со статусом ok."""
        job_id = self._current_job_id()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM history
                WHERE source_message_id = ? AND status = 'ok' AND job_id = ?
                LIMIT 1
                """,
                (source_message_id, job_id),
            ).fetchone()
        return row is not None

    def was_group_processed(self, grouped_id: str) -> bool:
        """Альбом уже публиковался? (страховка от дублей после рестарта)."""
        if not grouped_id:
            return False
        job_id = self._current_job_id()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM history
                WHERE grouped_id = ? AND status = 'ok' AND job_id = ?
                LIMIT 1
                """,
                (str(grouped_id), job_id),
            ).fetchone()
        return row is not None

    def history_count(self) -> int:
        job_id = self._current_job_id()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM history WHERE status = 'ok' AND job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def error_count(self) -> int:
        job_id = self._current_job_id()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM history WHERE status = 'error' AND job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def published_since(self, unix_ts: float) -> int:
        """Сколько успешных публикаций после указанного момента."""
        iso = datetime.fromtimestamp(max(0.0, unix_ts), tz=timezone.utc).isoformat()
        job_id = self._current_job_id()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM history
                WHERE status = 'ok' AND created_at >= ? AND job_id = ?
                """,
                (iso, job_id),
            ).fetchone()
        return int(row["c"]) if row else 0

    def last_errors(self, limit: int = 5) -> list[tuple[int, str]]:
        job_id = self._current_job_id()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_message_id AS mid, error FROM history
                WHERE status = 'error' AND job_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (job_id, int(limit)),
            ).fetchall()
        return [(int(r["mid"]), r["error"] or "") for r in rows]

    def clear_history(self) -> int:
        """Удалить историю текущего окна. Возвращает число удалённых строк."""
        job_id = self._current_job_id()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM history WHERE job_id = ?", (job_id,))
            return int(cur.rowcount or 0)

    def clear_history_after(self, source_message_id: int) -> int:
        """Удалить историю с source_message_id > порога (для старта со ссылки)."""
        job_id = self._current_job_id()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM history WHERE source_message_id > ? AND job_id = ?",
                (int(source_message_id), job_id),
            )
            return int(cur.rowcount or 0)

    def max_ok_source_id(self) -> int:
        """Максимальный source_message_id со статусом ok (0 если пусто)."""
        job_id = self._current_job_id()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(source_message_id) AS m FROM history
                WHERE status = 'ok' AND job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if not row or row["m"] is None:
            return 0
        return int(row["m"])

    def list_target_message_ids(self, limit: Optional[int] = None) -> list[int]:
        """ID сообщений в канале-назначении, которые бот успешно опубликовал."""
        sql = """
            SELECT DISTINCT target_message_id AS mid
            FROM history
            WHERE status = 'ok' AND target_message_id IS NOT NULL AND job_id = ?
            ORDER BY target_message_id DESC
        """
        job_id = self._current_job_id()
        if limit is not None and limit > 0:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            rows = conn.execute(sql, (job_id,)).fetchall()
        return [int(r["mid"]) for r in rows if r["mid"] is not None]

    # ------------------------------------------------------------------
    # Лобби компенсации: рассылка и заявки
    # ------------------------------------------------------------------

    def lobby_was_mailed(self, peer_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT peer_id FROM lobby_mailings WHERE peer_id = ?",
                (int(peer_id),),
            ).fetchone()
        return row is not None

    def lobby_mailed_ids(self) -> set[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT peer_id FROM lobby_mailings").fetchall()
        return {int(r["peer_id"]) for r in rows}

    def lobby_mark_mailed(self, peer_id: int, error: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lobby_mailings(peer_id, sent_at, error)
                VALUES(?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    sent_at = excluded.sent_at,
                    error = excluded.error
                """,
                (int(peer_id), now, error or ""),
            )

    def lobby_mailing_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM lobby_mailings WHERE error = ''"
            ).fetchone()
        return int(row["c"]) if row else 0

    def lobby_save_claim(
        self,
        *,
        user_id: int,
        username: str,
        tariff: str,
        duration_days: int,
        price: float,
        receipt_file_id: str,
        receipt_type: str,
        status: str,
        reject_reason: str = "",
        granted_days: int = 0,
        shop_id: str = "",
        forensic_notes: str = "",
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO lobby_claims(
                    user_id, username, tariff, duration_days, price,
                    receipt_file_id, receipt_type, status, reject_reason,
                    granted_days, shop_id, forensic_notes, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    username or "",
                    tariff,
                    int(duration_days),
                    float(price),
                    receipt_file_id or "",
                    receipt_type or "",
                    status,
                    reject_reason or "",
                    int(granted_days),
                    shop_id or "",
                    forensic_notes or "",
                    now,
                ),
            )
            return int(cur.lastrowid)

    def lobby_latest_claim(self, user_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM lobby_claims
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def lobby_has_granted(self, user_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM lobby_claims
                WHERE user_id = ? AND status = 'granted'
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
        return row is not None

    def shop_save_tariffs(self, rows: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM shop_tariffs")
            for i, row in enumerate(rows):
                conn.execute(
                    """
                    INSERT INTO shop_tariffs(
                        shop_id, title, short_name, sort_order, extra_json, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("shop_id") or ""),
                        str(row.get("title") or ""),
                        str(row.get("short_name") or ""),
                        int(row.get("sort_order", i)),
                        str(row.get("extra_json") or "{}"),
                        now,
                    ),
                )

    def shop_list_tariffs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM shop_tariffs ORDER BY sort_order, shop_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def support_get(self, peer_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM support_sessions WHERE peer_id = ?",
                (int(peer_id),),
            ).fetchone()
        return dict(row) if row else None

    def support_upsert(
        self,
        peer_id: int,
        *,
        state: Optional[str] = None,
        shop_id: Optional[str] = None,
        tariff: Optional[str] = None,
        duration_days: Optional[int] = None,
        price: Optional[float] = None,
        last_msg_id: Optional[int] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.support_get(peer_id) or {}
        data = {
            "state": state if state is not None else cur.get("state") or "idle",
            "shop_id": shop_id if shop_id is not None else cur.get("shop_id") or "",
            "tariff": tariff if tariff is not None else cur.get("tariff") or "",
            "duration_days": (
                int(duration_days)
                if duration_days is not None
                else int(cur.get("duration_days") or 0)
            ),
            "price": float(price) if price is not None else float(cur.get("price") or 0),
            "last_msg_id": (
                int(last_msg_id)
                if last_msg_id is not None
                else int(cur.get("last_msg_id") or 0)
            ),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO support_sessions(
                    peer_id, state, shop_id, tariff, duration_days, price,
                    last_msg_id, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    state = excluded.state,
                    shop_id = excluded.shop_id,
                    tariff = excluded.tariff,
                    duration_days = excluded.duration_days,
                    price = excluded.price,
                    last_msg_id = excluded.last_msg_id,
                    updated_at = excluded.updated_at
                """,
                (
                    int(peer_id),
                    data["state"],
                    data["shop_id"],
                    data["tariff"],
                    data["duration_days"],
                    data["price"],
                    data["last_msg_id"],
                    now,
                ),
            )
