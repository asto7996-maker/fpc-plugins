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

    @property
    def interval_hours(self) -> float:
        return self.interval_seconds / 3600.0


class Database:
    """Простая обёртка над SQLite без внешних ORM."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
                """
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
            seconds = max(5.0, hours * 3600.0)
            self.set(SETTING_INTERVAL_SECONDS, seconds)
            logger.info(
                "Миграция интервала: %.4f ч → %.0f сек", hours, seconds
            )
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM settings WHERE key = ?", (SETTING_INTERVAL_HOURS,)
            )

    # ------------------------------------------------------------------
    # Типизированные хелперы
    # ------------------------------------------------------------------

    def get_settings(self) -> Settings:
        """Прочитать все настройки одним объектом."""
        return Settings(
            caption_template=self.get(SETTING_CAPTION, "") or "",
            interval_seconds=max(
                5.0, self.get_float(SETTING_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS)
            ),
            posts_per_cycle=max(1, self.get_int(SETTING_POSTS_PER_CYCLE, 5)),
            is_running=self.get_bool(SETTING_IS_RUNNING, False),
            progress_id=self.get_int(SETTING_PROGRESS_ID, 0),
            source_channel=self.get(SETTING_SOURCE_CHANNEL, "") or "",
            target_channel=self.get(SETTING_TARGET_CHANNEL, "") or "",
            start_link=self.get(SETTING_START_LINK, "") or "",
            catchup_enabled=self.get_bool(SETTING_CATCHUP, False),
            catchup_seconds=max(
                5.0, self.get_float(SETTING_CATCHUP_SECONDS, DEFAULT_CATCHUP_SECONDS)
            ),
            notify_cycles=self.get_bool(SETTING_NOTIFY_CYCLES, False),
        )

    def set_caption(self, text: str) -> None:
        self.set(SETTING_CAPTION, text)

    def set_interval_seconds(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("Интервал должен быть больше 0")
        self.set(SETTING_INTERVAL_SECONDS, max(5.0, float(seconds)))

    def set_interval_hours(self, hours: float) -> None:
        if hours <= 0:
            raise ValueError("Интервал должен быть больше 0")
        self.set_interval_seconds(float(hours) * 3600.0)

    def set_catchup(self, enabled: bool) -> None:
        self.set(SETTING_CATCHUP, "1" if enabled else "0")

    def set_catchup_seconds(self, seconds: float) -> None:
        self.set(SETTING_CATCHUP_SECONDS, max(5.0, float(seconds)))

    def set_notify_cycles(self, enabled: bool) -> None:
        self.set(SETTING_NOTIFY_CYCLES, "1" if enabled else "0")

    def set_posts_per_cycle(self, count: int) -> None:
        if count < 1:
            raise ValueError("Количество постов за цикл должно быть ≥ 1")
        self.set(SETTING_POSTS_PER_CYCLE, count)

    def set_running(self, running: bool) -> None:
        self.set(SETTING_IS_RUNNING, "1" if running else "0")

    def get_progress_id(self) -> int:
        return self.get_int(SETTING_PROGRESS_ID, 0)

    def set_progress_id(self, message_id: int) -> None:
        """Сохранить ID последнего обработанного поста (для продолжения после рестарта)."""
        self.set(SETTING_PROGRESS_ID, message_id)

    def max_ok_source_id(self) -> int:
        """Максимальный source_message_id со статусом ok (0 если пусто)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(source_message_id) AS m FROM history
                WHERE status = 'ok'
                """
            ).fetchone()
        if not row or row["m"] is None:
            return 0
        return int(row["m"])

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
        return self.get_float(STATE_NEXT_RUN_AT, 0.0)

    def get_next_run_reason(self) -> str:
        return self.get(STATE_NEXT_RUN_REASON, "") or ""

    def run_asap(self) -> None:
        """Запросить цикл при первой возможности (кнопка ▶️ Старт и т.п.)."""
        self.set_next_run(0.0, "asap")

    def mark_cycle(self, published: int) -> None:
        now = time.time()
        self.set(STATE_LAST_CYCLE_AT, f"{now:.0f}")
        if published > 0:
            self.set(STATE_LAST_PUBLISHED_AT, f"{now:.0f}")

    def get_last_cycle_at(self) -> float:
        return self.get_float(STATE_LAST_CYCLE_AT, 0.0)

    def get_last_published_at(self) -> float:
        return self.get_float(STATE_LAST_PUBLISHED_AT, 0.0)

    def set_last_error(self, text: str) -> None:
        self.set(STATE_LAST_ERROR, text or "")
        self.set(STATE_LAST_ERROR_AT, f"{time.time():.0f}")

    def clear_last_error(self) -> None:
        self.set(STATE_LAST_ERROR, "")

    def get_last_error(self) -> tuple[str, float]:
        return (
            self.get(STATE_LAST_ERROR, "") or "",
            self.get_float(STATE_LAST_ERROR_AT, 0.0),
        )

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
        return self.get_int(STATE_LATEST_SOURCE_ID, 0)

    def backlog(self) -> int:
        """Сколько ID источника ещё впереди (грубая оценка очереди)."""
        latest = self.get_latest_source_id()
        progress = self.get_progress_id()
        return max(0, latest - progress)

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO history(
                    source_message_id, target_message_id, grouped_id,
                    status, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_message_id,
                    target_message_id,
                    grouped_id,
                    status,
                    error,
                    now,
                ),
            )

    def was_processed(self, source_message_id: int) -> bool:
        """Проверить, есть ли пост уже в истории со статусом ok."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM history
                WHERE source_message_id = ? AND status = 'ok'
                LIMIT 1
                """,
                (source_message_id,),
            ).fetchone()
        return row is not None

    def was_group_processed(self, grouped_id: str) -> bool:
        """Альбом уже публиковался? (страховка от дублей после рестарта)."""
        if not grouped_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM history
                WHERE grouped_id = ? AND status = 'ok'
                LIMIT 1
                """,
                (str(grouped_id),),
            ).fetchone()
        return row is not None

    def history_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM history WHERE status = 'ok'"
            ).fetchone()
        return int(row["c"]) if row else 0

    def error_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM history WHERE status = 'error'"
            ).fetchone()
        return int(row["c"]) if row else 0

    def published_since(self, unix_ts: float) -> int:
        """Сколько успешных публикаций после указанного момента."""
        iso = datetime.fromtimestamp(max(0.0, unix_ts), tz=timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM history
                WHERE status = 'ok' AND created_at >= ?
                """,
                (iso,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def last_errors(self, limit: int = 5) -> list[tuple[int, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_message_id AS mid, error FROM history
                WHERE status = 'error'
                ORDER BY id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [(int(r["mid"]), r["error"] or "") for r in rows]

    def clear_history(self) -> int:
        """Удалить всю историю публикаций. Возвращает число удалённых строк."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM history")
            return int(cur.rowcount or 0)

    def clear_history_after(self, source_message_id: int) -> int:
        """Удалить историю с source_message_id > порога (для старта со ссылки)."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM history WHERE source_message_id > ?",
                (int(source_message_id),),
            )
            return int(cur.rowcount or 0)

    def list_target_message_ids(self, limit: Optional[int] = None) -> list[int]:
        """ID сообщений в канале-назначении, которые бот успешно опубликовал."""
        sql = """
            SELECT DISTINCT target_message_id AS mid
            FROM history
            WHERE status = 'ok' AND target_message_id IS NOT NULL
            ORDER BY target_message_id DESC
        """
        if limit is not None and limit > 0:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [int(r["mid"]) for r in rows if r["mid"] is not None]
