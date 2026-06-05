#!/usr/bin/env python3
"""Safe Telegram content machine bot.

This bot is intentionally built around Telegram Bot API limits:

* it reposts only channel posts that Telegram delivers to the bot, which means
  the bot must be added to the source channels with the required rights;
* it sends broadcasts only to users who opted in with /start or /subscribe;
* it does not scrape members, invite strangers, or send unsolicited direct
  messages.

The implementation focuses on stability for a small/medium channel operation:
SQLite persistence, retry/backoff, deduplication, optional media metadata
cleanup, rate limiting, and clear operational commands.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import mimetypes
import os
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:
    import telebot
    from telebot import apihelper
    from telebot.types import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
    )
except ImportError as exc:  # pragma: no cover - checked by CLI at runtime.
    print(
        "Missing dependency: pyTelegramBotAPI. Install with: "
        "pip install -r requirements-telegram-bot.txt",
        file=sys.stderr,
    )
    raise

try:
    from PIL import Image
except ImportError:  # Pillow is optional, but recommended.
    Image = None  # type: ignore[assignment]


APP_NAME = "telegram-content-machine"
DEFAULT_ENV_FILE = ".env"
DEFAULT_DB_PATH = "data/content_machine.sqlite3"
DEFAULT_TEMP_DIR = "data/tmp"
SUPPORTED_CONTENT_TYPES = [
    "text",
    "photo",
    "video",
    "animation",
    "document",
    "audio",
    "voice",
    "video_note",
]
MAX_TELEGRAM_CAPTION = 1024
MAX_TELEGRAM_MESSAGE = 4096


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "да"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "нет"}:
        return False
    return default


def _parse_int(value: Optional[str], default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_float(value: Optional[str], default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    result: List[str] = []
    for item in value.replace("\n", ",").split(","):
        cleaned = item.strip()
        if cleaned:
            result.append(cleaned)
    return result


def _decode_env_text(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\t", "\t").strip()


def _normalize_channel_ref(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/")
    if value.startswith("http://t.me/"):
        value = value.removeprefix("http://t.me/")
    if value.startswith("t.me/"):
        value = value.removeprefix("t.me/")
    if "/" in value:
        value = value.split("/", 1)[0]
    if value.startswith("@"):
        value = value[1:]
    return value.lower()


def _normalize_chat_id(value: str) -> str:
    value = value.strip()
    if value.startswith("@"):
        return value
    try:
        return str(int(value))
    except ValueError:
        return "@" + _normalize_channel_ref(value)


def load_env_file(path: Path) -> None:
    """Load a simple KEY=VALUE .env file into os.environ.

    Existing environment variables win over file values so systemd overrides or
    shell exports can safely replace the file without editing it.
    """

    if not path.exists():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            logging.getLogger(APP_NAME).warning("Ignoring malformed env line %s in %s", line_number, path)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    target_channel_id: str
    admin_ids: Tuple[int, ...]
    source_channel_refs: Tuple[str, ...]
    database_path: Path = Path(DEFAULT_DB_PATH)
    temp_dir: Path = Path(DEFAULT_TEMP_DIR)
    log_level: str = "INFO"
    polling_timeout: int = 30
    polling_interval: float = 0.0
    worker_count: int = 1
    max_retries: int = 5
    retry_base_delay: float = 2.0
    send_min_interval: float = 1.2
    broadcast_min_interval: float = 0.08
    remove_metadata: bool = True
    prefer_copy_without_cleanup: bool = False
    include_original_source: bool = False
    skip_forwarded_posts: bool = False
    allow_text_posts: bool = True
    allow_media_posts: bool = True
    max_download_mb: int = 45
    caption_template: str = "{caption}\n\n{ad_text}"
    text_template: str = "{text}\n\n{ad_text}"
    ad_text: str = ""
    cta_text: str = ""
    cta_url: str = ""
    dry_run: bool = False

    @staticmethod
    def from_env(env_file: Optional[Path] = None) -> "Settings":
        if env_file:
            load_env_file(env_file)

        token = os.getenv("BOT_TOKEN", "").strip()
        target = os.getenv("TARGET_CHANNEL_ID", "").strip()
        admins = tuple(
            int(item)
            for item in _split_csv(os.getenv("ADMIN_IDS"))
            if item.lstrip("-").isdigit()
        )
        source_refs = tuple(_normalize_channel_ref(item) for item in _split_csv(os.getenv("SOURCE_CHANNELS")))

        settings = Settings(
            bot_token=token,
            target_channel_id=_normalize_chat_id(target) if target else "",
            admin_ids=admins,
            source_channel_refs=source_refs,
            database_path=Path(os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)),
            temp_dir=Path(os.getenv("TEMP_DIR", DEFAULT_TEMP_DIR)),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            polling_timeout=_parse_int(os.getenv("POLLING_TIMEOUT"), 30),
            polling_interval=_parse_float(os.getenv("POLLING_INTERVAL"), 0.0),
            worker_count=max(1, _parse_int(os.getenv("WORKER_COUNT"), 1)),
            max_retries=max(1, _parse_int(os.getenv("MAX_RETRIES"), 5)),
            retry_base_delay=max(0.2, _parse_float(os.getenv("RETRY_BASE_DELAY"), 2.0)),
            send_min_interval=max(0.0, _parse_float(os.getenv("SEND_MIN_INTERVAL"), 1.2)),
            broadcast_min_interval=max(0.0, _parse_float(os.getenv("BROADCAST_MIN_INTERVAL"), 0.08)),
            remove_metadata=_parse_bool(os.getenv("REMOVE_METADATA", "true"), True),
            prefer_copy_without_cleanup=_parse_bool(os.getenv("PREFER_COPY_WITHOUT_CLEANUP", "false"), False),
            include_original_source=_parse_bool(os.getenv("INCLUDE_ORIGINAL_SOURCE", "false"), False),
            skip_forwarded_posts=_parse_bool(os.getenv("SKIP_FORWARDED_POSTS", "false"), False),
            allow_text_posts=_parse_bool(os.getenv("ALLOW_TEXT_POSTS", "true"), True),
            allow_media_posts=_parse_bool(os.getenv("ALLOW_MEDIA_POSTS", "true"), True),
            max_download_mb=max(1, _parse_int(os.getenv("MAX_DOWNLOAD_MB"), 45)),
            caption_template=_decode_env_text(os.getenv("CAPTION_TEMPLATE", "{caption}\n\n{ad_text}")),
            text_template=_decode_env_text(os.getenv("TEXT_TEMPLATE", "{text}\n\n{ad_text}")),
            ad_text=_decode_env_text(os.getenv("AD_TEXT", "")),
            cta_text=os.getenv("CTA_TEXT", "").strip(),
            cta_url=os.getenv("CTA_URL", "").strip(),
            dry_run=_parse_bool(os.getenv("DRY_RUN", "false"), False),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        errors: List[str] = []
        if not self.bot_token:
            errors.append("BOT_TOKEN is required")
        if not self.target_channel_id:
            errors.append("TARGET_CHANNEL_ID is required")
        if not self.admin_ids:
            errors.append("ADMIN_IDS must contain at least one Telegram user id")
        if not self.source_channel_refs:
            errors.append("SOURCE_CHANNELS must contain at least one allowed source channel")
        if self.cta_text and not self.cta_url:
            errors.append("CTA_URL is required when CTA_TEXT is set")
        if self.cta_url and not self.cta_url.startswith(("https://", "http://", "tg://")):
            errors.append("CTA_URL must start with https://, http://, or tg://")
        if errors:
            raise ValueError("; ".join(errors))

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key in ("chat_id", "message_id", "source", "target", "subscriber_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class SQLiteStore:
    def __init__(self, db_path: Path, logger: logging.Logger) -> None:
        self.db_path = db_path
        self.logger = logger
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS post_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id TEXT NOT NULL,
                    source_username TEXT,
                    source_title TEXT,
                    source_message_id INTEGER NOT NULL,
                    target_chat_id TEXT NOT NULL,
                    target_message_id INTEGER,
                    content_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_chat_id, source_message_id)
                );

                CREATE TABLE IF NOT EXISTS subscribers (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS broadcast_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_post_log_created_at ON post_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers(status);
                """
            )

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def reserve_post(self, message: Message, content_type: str, target_chat_id: str) -> bool:
        source_username = getattr(message.chat, "username", None)
        source_title = getattr(message.chat, "title", None)
        now = self.now()
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO post_log (
                        source_chat_id, source_username, source_title, source_message_id,
                        target_chat_id, content_type, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        str(message.chat.id),
                        source_username,
                        source_title,
                        int(message.message_id),
                        target_chat_id,
                        content_type,
                        now,
                        now,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_post_sent(self, source_chat_id: str, source_message_id: int, target_message_id: Optional[int]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE post_log
                SET status = 'sent', target_message_id = ?, error = NULL, updated_at = ?
                WHERE source_chat_id = ? AND source_message_id = ?
                """,
                (target_message_id, self.now(), source_chat_id, source_message_id),
            )

    def mark_post_failed(self, source_chat_id: str, source_message_id: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE post_log
                SET status = 'failed', error = ?, updated_at = ?
                WHERE source_chat_id = ? AND source_message_id = ?
                """,
                (error[:1000], self.now(), source_chat_id, source_message_id),
            )

    def upsert_subscriber(self, user: Any, status: str = "active") -> None:
        now = self.now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO subscribers (
                    user_id, username, first_name, last_name, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user.id),
                    getattr(user, "username", None),
                    getattr(user, "first_name", None),
                    getattr(user, "last_name", None),
                    status,
                    now,
                    now,
                ),
            )

    def unsubscribe(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE subscribers
                SET status = 'unsubscribed', updated_at = ?
                WHERE user_id = ?
                """,
                (self.now(), int(user_id)),
            )

    def active_subscribers(self) -> List[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM subscribers WHERE status = 'active' ORDER BY created_at ASC"
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    def log_broadcast(self, admin_id: int, user_id: int, status: str, error: Optional[str] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO broadcast_log (admin_id, user_id, status, error, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(admin_id), int(user_id), status, (error or "")[:1000], self.now()),
            )

    def stats(self) -> Dict[str, Any]:
        with self.connect() as conn:
            post_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM post_log GROUP BY status"
            ).fetchall()
            subscriber_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM subscribers GROUP BY status"
            ).fetchall()
            last_posts = conn.execute(
                """
                SELECT source_title, source_username, source_message_id, status, error, updated_at
                FROM post_log
                ORDER BY id DESC
                LIMIT 5
                """
            ).fetchall()
        return {
            "posts": {row["status"]: row["count"] for row in post_rows},
            "subscribers": {row["status"]: row["count"] for row in subscriber_rows},
            "last_posts": [dict(row) for row in last_posts],
        }


# ---------------------------------------------------------------------------
# Rate limiting and retries
# ---------------------------------------------------------------------------


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last_call)
            if delay > 0:
                time.sleep(delay)
            self._last_call = time.monotonic()


def retry_call(
    func: Callable[[], Any],
    *,
    logger: logging.Logger,
    max_retries: int,
    base_delay: float,
    operation: str,
) -> Any:
    last_error: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except apihelper.ApiTelegramException as exc:
            last_error = exc
            retry_after = _extract_retry_after(exc)
            if attempt >= max_retries or _is_permanent_telegram_error(exc):
                raise
            delay = retry_after if retry_after is not None else base_delay * (2 ** (attempt - 1))
            logger.warning("%s failed on attempt %s/%s; retry in %.1fs: %s", operation, attempt, max_retries, delay, exc)
            time.sleep(delay)
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("%s failed on attempt %s/%s; retry in %.1fs: %s", operation, attempt, max_retries, delay, exc)
            time.sleep(delay)
    if last_error:
        raise last_error
    raise RuntimeError(f"{operation} failed without an exception")


def _extract_retry_after(exc: apihelper.ApiTelegramException) -> Optional[float]:
    result_json = getattr(exc, "result_json", None) or {}
    parameters = result_json.get("parameters") or {}
    retry_after = parameters.get("retry_after")
    if retry_after is None:
        return None
    try:
        return float(retry_after) + 0.5
    except (TypeError, ValueError):
        return None


def _is_permanent_telegram_error(exc: apihelper.ApiTelegramException) -> bool:
    code = getattr(exc, "error_code", None)
    description = str(getattr(exc, "description", "")).lower()
    if code in {400, 401, 403, 404}:
        if "too many requests" in description:
            return False
        return True
    return False


# ---------------------------------------------------------------------------
# Media processing
# ---------------------------------------------------------------------------


@dataclass
class DownloadedMedia:
    path: Path
    content_type: str
    file_name: str
    mime_type: Optional[str] = None
    cleanup_paths: List[Path] = field(default_factory=list)

    def cleanup(self) -> None:
        for path in [self.path, *self.cleanup_paths]:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


class MediaProcessor:
    def __init__(self, bot: telebot.TeleBot, settings: Settings, logger: logging.Logger) -> None:
        self.bot = bot
        self.settings = settings
        self.logger = logger
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)

    def download_message_media(self, message: Message) -> Optional[DownloadedMedia]:
        file_id, file_name, mime_type, content_type, file_size = self._extract_file_info(message)
        if not file_id:
            return None
        if file_size and file_size > self.settings.max_download_bytes:
            raise ValueError(
                f"File is too large: {file_size / 1024 / 1024:.1f} MB; "
                f"limit is {self.settings.max_download_mb} MB"
            )

        file_info = retry_call(
            lambda: self.bot.get_file(file_id),
            logger=self.logger,
            max_retries=self.settings.max_retries,
            base_delay=self.settings.retry_base_delay,
            operation="get_file",
        )
        extension = self._guess_extension(file_name, mime_type, file_info.file_path)
        fd, raw_path = tempfile.mkstemp(prefix="tg_media_", suffix=extension, dir=self.settings.temp_dir)
        os.close(fd)
        destination = Path(raw_path)

        def do_download() -> None:
            downloaded = self.bot.download_file(file_info.file_path)
            destination.write_bytes(downloaded)

        retry_call(
            do_download,
            logger=self.logger,
            max_retries=self.settings.max_retries,
            base_delay=self.settings.retry_base_delay,
            operation="download_file",
        )

        media = DownloadedMedia(
            path=destination,
            content_type=content_type or "document",
            file_name=file_name or destination.name,
            mime_type=mime_type,
        )
        if self.settings.remove_metadata:
            media.path = self._strip_metadata(media)
        return media

    def _extract_file_info(
        self, message: Message
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[int]]:
        if message.photo:
            photo = message.photo[-1]
            return photo.file_id, f"photo_{message.message_id}.jpg", "image/jpeg", "photo", getattr(photo, "file_size", None)
        if message.video:
            return (
                message.video.file_id,
                message.video.file_name or f"video_{message.message_id}.mp4",
                message.video.mime_type or "video/mp4",
                "video",
                getattr(message.video, "file_size", None),
            )
        if message.animation:
            return (
                message.animation.file_id,
                message.animation.file_name or f"animation_{message.message_id}.mp4",
                message.animation.mime_type,
                "animation",
                getattr(message.animation, "file_size", None),
            )
        if message.document:
            return (
                message.document.file_id,
                message.document.file_name or f"document_{message.message_id}",
                message.document.mime_type,
                "document",
                getattr(message.document, "file_size", None),
            )
        if message.audio:
            return (
                message.audio.file_id,
                message.audio.file_name or f"audio_{message.message_id}.mp3",
                message.audio.mime_type,
                "audio",
                getattr(message.audio, "file_size", None),
            )
        if message.voice:
            return (
                message.voice.file_id,
                f"voice_{message.message_id}.ogg",
                message.voice.mime_type,
                "voice",
                getattr(message.voice, "file_size", None),
            )
        if message.video_note:
            return (
                message.video_note.file_id,
                f"video_note_{message.message_id}.mp4",
                "video/mp4",
                "video_note",
                getattr(message.video_note, "file_size", None),
            )
        return None, None, None, None, None

    def _guess_extension(self, file_name: Optional[str], mime_type: Optional[str], telegram_path: str) -> str:
        for candidate in (file_name, telegram_path):
            if candidate:
                suffix = Path(candidate).suffix
                if suffix:
                    return suffix
        if mime_type:
            guessed = mimetypes.guess_extension(mime_type)
            if guessed:
                return guessed
        return ".bin"

    def _strip_metadata(self, media: DownloadedMedia) -> Path:
        mime = (media.mime_type or "").lower()
        suffix = media.path.suffix.lower()
        if media.content_type == "photo" or mime.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            return self._strip_image_metadata(media)
        if media.content_type in {"video", "animation", "video_note"} or mime.startswith("video/"):
            return self._strip_video_metadata(media)
        if media.content_type == "audio" or mime.startswith("audio/"):
            return self._strip_audio_metadata(media)
        return media.path

    def _strip_image_metadata(self, media: DownloadedMedia) -> Path:
        if Image is None:
            self.logger.warning("Pillow is not installed; image metadata cleanup skipped")
            return media.path
        try:
            with Image.open(media.path) as img:
                output_suffix = ".png" if img.format == "PNG" else ".jpg"
                fd, output_raw = tempfile.mkstemp(
                    prefix="tg_image_clean_",
                    suffix=output_suffix,
                    dir=self.settings.temp_dir,
                )
                os.close(fd)
                output = Path(output_raw)
                clean = Image.new(img.mode, img.size)
                clean.putdata(list(img.getdata()))
                if clean.mode in {"RGBA", "LA", "P"} and output_suffix == ".jpg":
                    clean = clean.convert("RGB")
                save_kwargs: Dict[str, Any] = {}
                if output_suffix == ".jpg":
                    save_kwargs.update({"quality": 92, "optimize": True})
                clean.save(output, **save_kwargs)
                media.cleanup_paths.append(media.path)
                return output
        except Exception as exc:
            self.logger.warning("Image metadata cleanup failed, using original: %s", exc)
            return media.path

    def _strip_video_metadata(self, media: DownloadedMedia) -> Path:
        return self._strip_with_ffmpeg(media, suffix=media.path.suffix or ".mp4")

    def _strip_audio_metadata(self, media: DownloadedMedia) -> Path:
        return self._strip_with_ffmpeg(media, suffix=media.path.suffix or ".mp3")

    def _strip_with_ffmpeg(self, media: DownloadedMedia, suffix: str) -> Path:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.logger.warning("ffmpeg is not installed; media metadata cleanup skipped")
            return media.path
        fd, output_raw = tempfile.mkstemp(prefix="tg_media_clean_", suffix=suffix, dir=self.settings.temp_dir)
        os.close(fd)
        output = Path(output_raw)
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media.path),
            "-map_metadata",
            "-1",
            "-c",
            "copy",
            str(output),
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            media.cleanup_paths.append(media.path)
            return output
        except Exception as exc:
            self.logger.warning("ffmpeg metadata cleanup failed, using original: %s", exc)
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
            return media.path


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


@dataclass
class PostTask:
    message: Message
    content_type: str
    enqueued_at: float = field(default_factory=time.monotonic)


def detect_content_type(message: Message) -> str:
    for content_type in SUPPORTED_CONTENT_TYPES:
        if content_type == "text" and message.text:
            return "text"
        if getattr(message, content_type, None):
            return content_type
    return getattr(message, "content_type", "unknown") or "unknown"


def safe_truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = "..."
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def message_caption(message: Message) -> str:
    return (getattr(message, "caption", None) or "").strip()


def message_text(message: Message) -> str:
    return (getattr(message, "text", None) or "").strip()


def format_template(template: str, **values: str) -> str:
    class SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return ""

    return template.format_map(SafeDict(values)).strip()


def source_link(message: Message) -> str:
    username = getattr(message.chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message.message_id}"
    return ""


def is_forwarded(message: Message) -> bool:
    return bool(
        getattr(message, "forward_from", None)
        or getattr(message, "forward_from_chat", None)
        or getattr(message, "forward_sender_name", None)
    )


# ---------------------------------------------------------------------------
# Bot application
# ---------------------------------------------------------------------------


class ContentMachineBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(APP_NAME)
        self.store = SQLiteStore(settings.database_path, self.logger)
        self.bot = telebot.TeleBot(settings.bot_token, threaded=True, num_threads=4)
        self.media_processor = MediaProcessor(self.bot, settings, self.logger)
        self.post_queue: "queue.Queue[Optional[PostTask]]" = queue.Queue(maxsize=1000)
        self.stop_event = threading.Event()
        self.worker_threads: List[threading.Thread] = []
        self.send_limiter = RateLimiter(settings.send_min_interval)
        self.broadcast_limiter = RateLimiter(settings.broadcast_min_interval)
        self.allowed_sources = set(settings.source_channel_refs)
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.bot.channel_post_handler(content_types=SUPPORTED_CONTENT_TYPES)
        def handle_channel_post(message: Message) -> None:
            self.handle_channel_post(message)

        @self.bot.message_handler(commands=["start", "subscribe"])
        def handle_start(message: Message) -> None:
            self.handle_subscribe(message)

        @self.bot.message_handler(commands=["unsubscribe", "stop"])
        def handle_unsubscribe(message: Message) -> None:
            self.handle_unsubscribe(message)

        @self.bot.message_handler(commands=["help"])
        def handle_help(message: Message) -> None:
            self.reply_help(message)

        @self.bot.message_handler(commands=["ping"])
        def handle_ping(message: Message) -> None:
            if self.is_admin_message(message):
                self.safe_reply(message, "pong")

        @self.bot.message_handler(commands=["status", "stats"])
        def handle_status(message: Message) -> None:
            if self.is_admin_message(message):
                self.reply_status(message)

        @self.bot.message_handler(commands=["sources"])
        def handle_sources(message: Message) -> None:
            if self.is_admin_message(message):
                self.reply_sources(message)

        @self.bot.message_handler(commands=["broadcast"])
        def handle_broadcast(message: Message) -> None:
            if self.is_admin_message(message):
                self.handle_broadcast(message)

        @self.bot.message_handler(func=lambda message: True, content_types=["text"])
        def handle_private_text(message: Message) -> None:
            if getattr(message.chat, "type", "") == "private":
                self.safe_reply(
                    message,
                    "Я принимаю только команды.\n"
                    "Нажмите /subscribe, чтобы получать легальные рассылки, или /unsubscribe для отписки.",
                )

    def run(self) -> None:
        self.logger.info("Starting %s", APP_NAME)
        self.start_workers()
        self._install_signal_handlers()
        while not self.stop_event.is_set():
            try:
                self.bot.infinity_polling(
                    timeout=self.settings.polling_timeout,
                    long_polling_timeout=self.settings.polling_timeout,
                    skip_pending=True,
                    none_stop=True,
                    interval=self.settings.polling_interval,
                    allowed_updates=["message", "channel_post"],
                )
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self.logger.error("Polling crashed, restarting in 5 seconds: %s", exc, exc_info=True)
                time.sleep(5)
        self.shutdown()

    def start_workers(self) -> None:
        for index in range(self.settings.worker_count):
            thread = threading.Thread(target=self._worker_loop, name=f"post-worker-{index + 1}", daemon=True)
            thread.start()
            self.worker_threads.append(thread)

    def shutdown(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        try:
            self.bot.stop_polling()
        except Exception:
            pass
        for _ in self.worker_threads:
            self.post_queue.put(None)
        for thread in self.worker_threads:
            thread.join(timeout=10)
        self.logger.info("Stopped %s", APP_NAME)

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            self.logger.info("Received signal %s, stopping", signum)
            self.shutdown()

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, _handler)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Incoming posts
    # ------------------------------------------------------------------

    def handle_channel_post(self, message: Message) -> None:
        if not self.is_allowed_source(message):
            self.logger.info(
                "Ignoring post from non-allowed source",
                extra={"chat_id": str(message.chat.id), "message_id": message.message_id},
            )
            return
        content_type = detect_content_type(message)
        if content_type == "text" and not self.settings.allow_text_posts:
            return
        if content_type != "text" and not self.settings.allow_media_posts:
            return
        if self.settings.skip_forwarded_posts and is_forwarded(message):
            self.logger.info("Skipping forwarded post", extra={"chat_id": str(message.chat.id), "message_id": message.message_id})
            return

        reserved = self.store.reserve_post(message, content_type, self.settings.target_channel_id)
        if not reserved:
            self.logger.info(
                "Duplicate post skipped",
                extra={"chat_id": str(message.chat.id), "message_id": message.message_id},
            )
            return

        try:
            self.post_queue.put_nowait(PostTask(message=message, content_type=content_type))
            self.logger.info(
                "Queued source post",
                extra={"chat_id": str(message.chat.id), "message_id": message.message_id},
            )
        except queue.Full:
            error = "Internal queue is full"
            self.store.mark_post_failed(str(message.chat.id), int(message.message_id), error)
            self.logger.error(error)

    def is_allowed_source(self, message: Message) -> bool:
        candidates = {str(message.chat.id)}
        username = getattr(message.chat, "username", None)
        title = getattr(message.chat, "title", None)
        if username:
            candidates.add(_normalize_channel_ref(username))
            candidates.add("@" + _normalize_channel_ref(username))
        if title:
            candidates.add(title.lower())
        return any(candidate in self.allowed_sources for candidate in candidates)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            task = self.post_queue.get()
            if task is None:
                self.post_queue.task_done()
                break
            try:
                self.process_post(task)
            except Exception as exc:
                message = task.message
                self.store.mark_post_failed(str(message.chat.id), int(message.message_id), str(exc))
                self.logger.error(
                    "Post processing failed: %s\n%s",
                    exc,
                    traceback.format_exc(),
                    extra={"chat_id": str(message.chat.id), "message_id": message.message_id},
                )
            finally:
                self.post_queue.task_done()

    def process_post(self, task: PostTask) -> None:
        message = task.message
        self.send_limiter.wait()

        if self.settings.dry_run:
            self.logger.info(
                "DRY_RUN: would repost message",
                extra={"chat_id": str(message.chat.id), "message_id": message.message_id},
            )
            self.store.mark_post_sent(str(message.chat.id), int(message.message_id), None)
            return

        if task.content_type == "text":
            sent = self.send_text_post(message)
        elif self.settings.prefer_copy_without_cleanup and not self.settings.remove_metadata:
            sent = self.copy_post(message)
        else:
            sent = self.reupload_media_post(message, task.content_type)

        target_message_id = getattr(sent, "message_id", None) if sent is not None else None
        self.store.mark_post_sent(str(message.chat.id), int(message.message_id), target_message_id)
        self.logger.info(
            "Post sent",
            extra={
                "chat_id": str(message.chat.id),
                "message_id": message.message_id,
                "target": self.settings.target_channel_id,
            },
        )

    def send_text_post(self, message: Message) -> Any:
        text = self.render_text(message)
        return retry_call(
            lambda: self.bot.send_message(
                self.settings.target_channel_id,
                safe_truncate(text, MAX_TELEGRAM_MESSAGE),
                reply_markup=self.build_markup(),
                disable_web_page_preview=False,
            ),
            logger=self.logger,
            max_retries=self.settings.max_retries,
            base_delay=self.settings.retry_base_delay,
            operation="send_message",
        )

    def copy_post(self, message: Message) -> Any:
        caption = self.render_caption(message)
        return retry_call(
            lambda: self.bot.copy_message(
                chat_id=self.settings.target_channel_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=safe_truncate(caption, MAX_TELEGRAM_CAPTION) if caption else None,
                reply_markup=self.build_markup(),
            ),
            logger=self.logger,
            max_retries=self.settings.max_retries,
            base_delay=self.settings.retry_base_delay,
            operation="copy_message",
        )

    def reupload_media_post(self, message: Message, content_type: str) -> Any:
        media = self.media_processor.download_message_media(message)
        if media is None:
            if message_text(message):
                return self.send_text_post(message)
            raise ValueError(f"Unsupported content type: {content_type}")
        try:
            caption = safe_truncate(self.render_caption(message), MAX_TELEGRAM_CAPTION)
            markup = self.build_markup()
            operation = f"send_{media.content_type}"

            def send() -> Any:
                with media.path.open("rb") as fh:
                    if media.content_type == "photo":
                        return self.bot.send_photo(self.settings.target_channel_id, fh, caption=caption or None, reply_markup=markup)
                    if media.content_type == "video":
                        return self.bot.send_video(self.settings.target_channel_id, fh, caption=caption or None, reply_markup=markup)
                    if media.content_type == "animation":
                        return self.bot.send_animation(self.settings.target_channel_id, fh, caption=caption or None, reply_markup=markup)
                    if media.content_type == "audio":
                        return self.bot.send_audio(self.settings.target_channel_id, fh, caption=caption or None, reply_markup=markup)
                    if media.content_type == "voice":
                        return self.bot.send_voice(self.settings.target_channel_id, fh, caption=caption or None, reply_markup=markup)
                    if media.content_type == "video_note":
                        return self.bot.send_video_note(self.settings.target_channel_id, fh, reply_markup=markup)
                    return self.bot.send_document(
                        self.settings.target_channel_id,
                        fh,
                        visible_file_name=media.file_name,
                        caption=caption or None,
                        reply_markup=markup,
                    )

            return retry_call(
                send,
                logger=self.logger,
                max_retries=self.settings.max_retries,
                base_delay=self.settings.retry_base_delay,
                operation=operation,
            )
        finally:
            media.cleanup()

    def render_caption(self, message: Message) -> str:
        source = source_link(message) if self.settings.include_original_source else ""
        return format_template(
            self.settings.caption_template,
            caption=message_caption(message),
            text=message_text(message),
            ad_text=self.settings.ad_text,
            source=source,
            channel_title=getattr(message.chat, "title", "") or "",
            channel_username=getattr(message.chat, "username", "") or "",
        )

    def render_text(self, message: Message) -> str:
        source = source_link(message) if self.settings.include_original_source else ""
        return format_template(
            self.settings.text_template,
            text=message_text(message),
            caption=message_caption(message),
            ad_text=self.settings.ad_text,
            source=source,
            channel_title=getattr(message.chat, "title", "") or "",
            channel_username=getattr(message.chat, "username", "") or "",
        )

    def build_markup(self) -> Optional[InlineKeyboardMarkup]:
        if not self.settings.cta_text or not self.settings.cta_url:
            return None
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(self.settings.cta_text, url=self.settings.cta_url))
        return markup

    # ------------------------------------------------------------------
    # Private users and opt-in broadcasts
    # ------------------------------------------------------------------

    def handle_subscribe(self, message: Message) -> None:
        if not getattr(message, "from_user", None):
            return
        self.store.upsert_subscriber(message.from_user, status="active")
        self.safe_reply(
            message,
            "Вы подписаны на уведомления от этого бота.\n"
            "Отписаться можно командой /unsubscribe.",
        )

    def handle_unsubscribe(self, message: Message) -> None:
        if not getattr(message, "from_user", None):
            return
        self.store.unsubscribe(int(message.from_user.id))
        self.safe_reply(message, "Готово, вы отписаны от рассылки.")

    def handle_broadcast(self, message: Message) -> None:
        admin_id = int(message.from_user.id)
        text = self._broadcast_text_from_message(message)
        if not text:
            self.safe_reply(
                message,
                "Использование:\n"
                "1) /broadcast текст рассылки\n"
                "2) ответьте /broadcast на сообщение, которое нужно переслать opt-in подписчикам.",
            )
            return
        subscribers = self.store.active_subscribers()
        if not subscribers:
            self.safe_reply(message, "Нет активных opt-in подписчиков.")
            return
        self.safe_reply(message, f"Начинаю рассылку по {len(subscribers)} opt-in подписчикам.")
        sent = 0
        failed = 0
        for user_id in subscribers:
            if self.stop_event.is_set():
                break
            self.broadcast_limiter.wait()
            try:
                if message.reply_to_message:
                    self.bot.copy_message(user_id, message.chat.id, message.reply_to_message.message_id)
                else:
                    self.bot.send_message(user_id, safe_truncate(text, MAX_TELEGRAM_MESSAGE), disable_web_page_preview=False)
                self.store.log_broadcast(admin_id, user_id, "sent")
                sent += 1
            except Exception as exc:
                failed += 1
                self.store.log_broadcast(admin_id, user_id, "failed", str(exc))
                if "bot was blocked" in str(exc).lower() or "chat not found" in str(exc).lower():
                    self.store.unsubscribe(user_id)
                self.logger.warning("Broadcast to %s failed: %s", user_id, exc)
        self.safe_reply(message, f"Рассылка завершена. Успешно: {sent}, ошибок: {failed}.")

    def _broadcast_text_from_message(self, message: Message) -> str:
        if message.reply_to_message:
            return "__copy_reply__"
        text = message.text or ""
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return ""
        return parts[1].strip()

    # ------------------------------------------------------------------
    # Admin and utility commands
    # ------------------------------------------------------------------

    def is_admin_message(self, message: Message) -> bool:
        user_id = getattr(getattr(message, "from_user", None), "id", None)
        if user_id is not None and int(user_id) in self.settings.admin_ids:
            return True
        self.safe_reply(message, "Команда доступна только администраторам.")
        return False

    def reply_help(self, message: Message) -> None:
        text = (
            "Команды:\n"
            "/subscribe — подписаться на opt-in рассылку\n"
            "/unsubscribe — отписаться\n"
            "/help — помощь\n\n"
            "Админ-команды:\n"
            "/status — статистика и очередь\n"
            "/sources — разрешенные каналы-источники\n"
            "/broadcast текст — рассылка только подписавшимся пользователям\n"
            "/ping — проверка доступности"
        )
        self.safe_reply(message, text)

    def reply_sources(self, message: Message) -> None:
        sources = "\n".join(f"- {source}" for source in self.settings.source_channel_refs)
        self.safe_reply(message, f"Разрешенные источники:\n{sources}")

    def reply_status(self, message: Message) -> None:
        stats = self.store.stats()
        posts = stats["posts"]
        subscribers = stats["subscribers"]
        lines = [
            "Статус content-machine:",
            f"Очередь: {self.post_queue.qsize()}",
            f"Посты: sent={posts.get('sent', 0)}, queued={posts.get('queued', 0)}, failed={posts.get('failed', 0)}",
            f"Подписчики: active={subscribers.get('active', 0)}, unsubscribed={subscribers.get('unsubscribed', 0)}",
            f"Dry-run: {self.settings.dry_run}",
            "",
            "Последние посты:",
        ]
        for item in stats["last_posts"]:
            source_name = item.get("source_username") or item.get("source_title") or "unknown"
            error = f" error={item['error']}" if item.get("error") else ""
            lines.append(f"- {source_name}/{item['source_message_id']}: {item['status']}{error}")
        self.safe_reply(message, "\n".join(lines))

    def safe_reply(self, message: Message, text: str) -> None:
        try:
            self.bot.reply_to(message, safe_truncate(text, MAX_TELEGRAM_MESSAGE))
        except Exception as exc:
            self.logger.warning("Failed to reply: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe Telegram content machine bot")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="Path to .env file")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration and exit")
    parser.add_argument("--print-example-env", action="store_true", help="Print example .env and exit")
    return parser.parse_args(argv)


def example_env() -> str:
    return """# Telegram bot token from @BotFather
BOT_TOKEN=123456:CHANGE_ME

# Target channel id or @username. Add the bot as admin to this channel.
TARGET_CHANNEL_ID=@your_target_channel

# Telegram numeric user ids allowed to run admin commands.
ADMIN_IDS=123456789

# Comma-separated source channel ids or usernames.
# The bot must be added to every source channel so Bot API can deliver channel_post updates.
SOURCE_CHANNELS=@source_one,@source_two,-1001234567890

# Text appended to reposted content.
AD_TEXT=Подписывайся на наш канал: https://t.me/your_target_channel

# Optional inline button under reposts.
CTA_TEXT=Открыть канал
CTA_URL=https://t.me/your_target_channel

# Templates. Available placeholders: {text}, {caption}, {ad_text}, {source}, {channel_title}, {channel_username}
TEXT_TEMPLATE={text}\\n\\n{ad_text}
CAPTION_TEMPLATE={caption}\\n\\n{ad_text}

# Safety and stability settings.
REMOVE_METADATA=true
INCLUDE_ORIGINAL_SOURCE=false
SKIP_FORWARDED_POSTS=false
ALLOW_TEXT_POSTS=true
ALLOW_MEDIA_POSTS=true
MAX_DOWNLOAD_MB=45
SEND_MIN_INTERVAL=1.2
BROADCAST_MIN_INTERVAL=0.08
MAX_RETRIES=5
RETRY_BASE_DELAY=2
WORKER_COUNT=1
DATABASE_PATH=data/content_machine.sqlite3
TEMP_DIR=data/tmp
LOG_LEVEL=INFO
DRY_RUN=false
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.print_example_env:
        print(example_env())
        return 0
    env_file = Path(args.env_file) if args.env_file else None
    try:
        settings = Settings.from_env(env_file)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    logger = logging.getLogger(APP_NAME)
    logger.info("Configuration loaded")
    if args.check_config:
        print("Configuration OK")
        return 0
    app = ContentMachineBot(settings)
    try:
        app.run()
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
