"""
database.py — слой работы с SQLite.

Хранит:
  • настройки автопостинга (интервал, лимит, шаблон текста, флаг паузы);
  • progress_id — ID последнего обработанного поста в источнике;
  • историю успешно пересланных постов.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


# Ключи настроек в таблице settings
SETTING_CAPTION = "caption_template"
SETTING_INTERVAL_HOURS = "interval_hours"
SETTING_POSTS_PER_CYCLE = "posts_per_cycle"
SETTING_IS_RUNNING = "is_running"
SETTING_PROGRESS_ID = "progress_id"
SETTING_SOURCE_CHANNEL = "source_channel"
SETTING_TARGET_CHANNEL = "target_channel"
SETTING_START_LINK = "start_link"


@dataclass
class Settings:
    """Снимок всех настроек автопостинга."""

    caption_template: str
    interval_hours: float
    posts_per_cycle: int
    is_running: bool
    progress_id: int
    source_channel: str
    target_channel: str
    start_link: str


class Database:
    """Простая обёртка над SQLite без внешних ORM."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Контекстный менеджер соединения с row_factory=Row."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
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

    def ensure_defaults(
        self,
        *,
        caption: str,
        interval_hours: float,
        posts_per_cycle: int,
        source_channel: str,
        target_channel: str,
    ) -> None:
        """Записать значения по умолчанию, если ключ ещё не существует."""
        defaults = {
            SETTING_CAPTION: caption,
            SETTING_INTERVAL_HOURS: str(interval_hours),
            SETTING_POSTS_PER_CYCLE: str(posts_per_cycle),
            SETTING_IS_RUNNING: "0",
            SETTING_PROGRESS_ID: "0",
            SETTING_SOURCE_CHANNEL: source_channel,
            SETTING_TARGET_CHANNEL: target_channel,
            SETTING_START_LINK: "",
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

    # ------------------------------------------------------------------
    # Типизированные хелперы
    # ------------------------------------------------------------------

    def get_settings(self) -> Settings:
        """Прочитать все настройки одним объектом."""
        return Settings(
            caption_template=self.get(SETTING_CAPTION, "") or "",
            interval_hours=float(self.get(SETTING_INTERVAL_HOURS, "6") or "6"),
            posts_per_cycle=int(self.get(SETTING_POSTS_PER_CYCLE, "5") or "5"),
            is_running=(self.get(SETTING_IS_RUNNING, "0") or "0") == "1",
            progress_id=int(self.get(SETTING_PROGRESS_ID, "0") or "0"),
            source_channel=self.get(SETTING_SOURCE_CHANNEL, "") or "",
            target_channel=self.get(SETTING_TARGET_CHANNEL, "") or "",
            start_link=self.get(SETTING_START_LINK, "") or "",
        )

    def set_caption(self, text: str) -> None:
        self.set(SETTING_CAPTION, text)

    def set_interval_hours(self, hours: float) -> None:
        if hours <= 0:
            raise ValueError("Интервал должен быть больше 0")
        self.set(SETTING_INTERVAL_HOURS, hours)

    def set_posts_per_cycle(self, count: int) -> None:
        if count < 1:
            raise ValueError("Количество постов за цикл должно быть ≥ 1")
        self.set(SETTING_POSTS_PER_CYCLE, count)

    def set_running(self, running: bool) -> None:
        self.set(SETTING_IS_RUNNING, "1" if running else "0")

    def get_progress_id(self) -> int:
        return int(self.get(SETTING_PROGRESS_ID, "0") or "0")

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

    def history_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM history WHERE status = 'ok'"
            ).fetchone()
        return int(row["c"]) if row else 0

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
