#!/usr/bin/env python3
"""Safe Telegram content-machine bot.

This bot is intentionally built for legitimate channel operations:

* collect posts from RSS/Atom feeds and explicitly allowed Telegram channels;
* keep a moderation queue before publishing;
* publish to a target channel with an optional signature/ad link;
* send broadcasts only to people who opted in with /start or /subscribe.

It does not parse competitor chats, invite strangers, scrape user lists, or send
unsolicited DMs. Those actions violate Telegram rules and create high ban risk.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import html
import json
import logging
import os
import queue
import re
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests


APP_NAME = "Telegram Content Machine"
APP_VERSION = "1.0.0"
DEFAULT_DB_PATH = "data/content_machine.sqlite3"
DEFAULT_POLL_TIMEOUT = 25
DEFAULT_FEED_INTERVAL = 180
DEFAULT_SCHEDULER_INTERVAL = 10
DEFAULT_BROADCAST_DELAY = 0.25
MAX_TELEGRAM_TEXT = 4096
MAX_CAPTION = 1024
MAX_QUEUE_ITEMS = 20
MAX_FEED_ITEMS_PER_RUN = 20
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramContentMachine/1.0; "
    "+https://core.telegram.org/bots)"
)


LOGGER = logging.getLogger("content_machine")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_ts() -> int:
    return int(time.time())


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "да", "истина"}:
        return True
    if text in {"0", "false", "no", "n", "off", "нет", "ложь"}:
        return False
    return default


def parse_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader to keep runtime dependencies minimal."""
    file_path = Path(path)
    if not file_path.exists():
        return
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def split_csv(value: str) -> List[str]:
    if not value:
        return []
    result: List[str] = []
    for part in re.split(r"[,\n;]+", value):
        item = part.strip()
        if item:
            result.append(item)
    return result


def parse_id_list(value: str) -> List[int]:
    ids: List[int] = []
    for item in split_csv(value):
        try:
            ids.append(int(item))
        except ValueError:
            LOGGER.warning("Skipping invalid numeric id: %s", item)
    return ids


def compact_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", text or "")
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<.*?>", "", text)
    return html.unescape(text).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def safe_json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


def fingerprint(*parts: Any) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(str(part or "").encode("utf-8", errors="ignore"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def parse_rfc3339_or_epoch(value: str) -> Optional[int]:
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    with contextlib.suppress(Exception):
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    return None


def normalize_chat_id(value: Any) -> str:
    return str(value or "").strip()


def infer_media_type(url: str, mime_type: str = "") -> str:
    lower_url = urllib.parse.urlparse(url or "").path.lower()
    lower_mime = (mime_type or "").lower()
    if lower_mime.startswith("image/") or lower_url.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "photo"
    if lower_mime.startswith("video/") or lower_url.endswith((".mp4", ".mov", ".m4v", ".webm")):
        return "video"
    if lower_mime.startswith("audio/") or lower_url.endswith((".mp3", ".ogg", ".wav", ".m4a")):
        return "audio"
    if url:
        return "document"
    return "text"


def message_has_media(message: Dict[str, Any]) -> bool:
    return any(
        key in message
        for key in (
            "photo",
            "video",
            "document",
            "audio",
            "animation",
            "voice",
            "video_note",
            "sticker",
        )
    )


@dataclasses.dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: Tuple[int, ...]
    target_channel_id: str
    db_path: str = DEFAULT_DB_PATH
    review_required: bool = True
    append_source_link: bool = True
    ad_signature: str = ""
    allowed_channel_ids: Tuple[int, ...] = ()
    initial_feeds: Tuple[str, ...] = ()
    feed_interval_sec: int = DEFAULT_FEED_INTERVAL
    scheduler_interval_sec: int = DEFAULT_SCHEDULER_INTERVAL
    poll_timeout_sec: int = DEFAULT_POLL_TIMEOUT
    broadcast_delay_sec: float = DEFAULT_BROADCAST_DELAY
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        bot_token = os.environ.get("BOT_TOKEN", "").strip()
        admin_ids = tuple(parse_id_list(os.environ.get("ADMIN_IDS", "")))
        target_channel_id = os.environ.get("TARGET_CHANNEL_ID", "").strip()
        db_path = os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH
        review_required = parse_bool(os.environ.get("REVIEW_REQUIRED"), True)
        append_source_link = parse_bool(os.environ.get("APPEND_SOURCE_LINK"), True)
        ad_signature = os.environ.get("AD_SIGNATURE", "").strip()
        allowed_channel_ids = tuple(parse_id_list(os.environ.get("ALLOWED_SOURCE_CHANNEL_IDS", "")))
        initial_feeds = tuple(split_csv(os.environ.get("RSS_FEEDS", "")))
        feed_interval_sec = max(30, parse_int(os.environ.get("FEED_INTERVAL_SEC"), DEFAULT_FEED_INTERVAL))
        scheduler_interval_sec = max(
            2, parse_int(os.environ.get("SCHEDULER_INTERVAL_SEC"), DEFAULT_SCHEDULER_INTERVAL)
        )
        poll_timeout_sec = max(5, parse_int(os.environ.get("POLL_TIMEOUT_SEC"), DEFAULT_POLL_TIMEOUT))
        try:
            broadcast_delay_sec = float(os.environ.get("BROADCAST_DELAY_SEC", DEFAULT_BROADCAST_DELAY))
        except ValueError:
            broadcast_delay_sec = DEFAULT_BROADCAST_DELAY
        log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        cfg = cls(
            bot_token=bot_token,
            admin_ids=admin_ids,
            target_channel_id=target_channel_id,
            db_path=db_path,
            review_required=review_required,
            append_source_link=append_source_link,
            ad_signature=ad_signature,
            allowed_channel_ids=allowed_channel_ids,
            initial_feeds=initial_feeds,
            feed_interval_sec=feed_interval_sec,
            scheduler_interval_sec=scheduler_interval_sec,
            poll_timeout_sec=poll_timeout_sec,
            broadcast_delay_sec=max(0.05, broadcast_delay_sec),
            log_level=log_level,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        errors: List[str] = []
        if not self.bot_token:
            errors.append("BOT_TOKEN is required")
        if not self.admin_ids:
            errors.append("ADMIN_IDS is required")
        if not self.target_channel_id:
            errors.append("TARGET_CHANNEL_ID is required")
        if errors:
            raise ValueError("; ".join(errors))


@dataclasses.dataclass
class Feed:
    id: int
    url: str
    title: str
    enabled: bool
    etag: str = ""
    last_modified: str = ""
    last_checked_at: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclasses.dataclass
class Post:
    id: int
    source_type: str
    source_id: str
    source_name: str
    external_id: str
    title: str
    text: str
    link: str
    media_url: str
    media_type: str
    status: str
    fingerprint: str
    scheduled_at: int = 0
    published_at: int = 0
    attempts: int = 0
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


class Storage:
    """SQLite storage with small, explicit methods and a single write lock."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    last_checked_at INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '',
                    source_name TEXT NOT NULL DEFAULT '',
                    external_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    link TEXT NOT NULL DEFAULT '',
                    media_url TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL DEFAULT 'text',
                    status TEXT NOT NULL DEFAULT 'pending',
                    fingerprint TEXT NOT NULL UNIQUE,
                    scheduled_at INTEGER NOT NULL DEFAULT 0,
                    published_at INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, scheduled_at, id);
                CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source_type, source_id, external_id);

                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    joined_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id INTEGER NOT NULL DEFAULT 0,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def add_audit(self, actor_id: int, action: str, target: str = "", payload: Any = "") -> None:
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log(actor_id, action, target, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (actor_id, action, target, payload, iso_now()),
            )
            self._conn.commit()

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, iso_now()),
            )
            self._conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def add_feed(self, url: str, title: str = "") -> int:
        url = url.strip()
        if not url:
            raise ValueError("empty feed url")
        now = iso_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO feeds(url, title, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(url) DO UPDATE SET enabled=1, updated_at=excluded.updated_at
                """,
                (url, title.strip(), now, now),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT id FROM feeds WHERE url=?", (url,)).fetchone()
            return int(row["id"])

    def remove_feed(self, feed_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def set_feed_enabled(self, feed_id: int, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE feeds SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, iso_now(), feed_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_feeds(self, enabled_only: bool = False) -> List[Feed]:
        sql = "SELECT * FROM feeds"
        args: Tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, args).fetchall()
        return [self._feed_from_row(row) for row in rows]

    def update_feed_meta(self, feed_id: int, title: str, etag: str, last_modified: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE feeds
                SET title=COALESCE(NULLIF(?, ''), title),
                    etag=?,
                    last_modified=?,
                    last_checked_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (title.strip(), etag or "", last_modified or "", now_ts(), iso_now(), feed_id),
            )
            self._conn.commit()

    def touch_feed(self, feed_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE feeds SET last_checked_at=?, updated_at=? WHERE id=?",
                (now_ts(), iso_now(), feed_id),
            )
            self._conn.commit()

    def upsert_subscriber(self, message_from: Dict[str, Any], active: bool = True) -> None:
        chat_id = int(message_from.get("id"))
        username = str(message_from.get("username") or "")
        first_name = str(message_from.get("first_name") or "")
        last_name = str(message_from.get("last_name") or "")
        now = iso_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO subscribers(chat_id, username, first_name, last_name, is_active, joined_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    is_active=excluded.is_active,
                    updated_at=excluded.updated_at
                """,
                (chat_id, username, first_name, last_name, 1 if active else 0, now, now),
            )
            self._conn.commit()

    def set_subscriber_active(self, chat_id: int, active: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE subscribers SET is_active=?, updated_at=? WHERE chat_id=?",
                (1 if active else 0, iso_now(), chat_id),
            )
            self._conn.commit()

    def count_subscribers(self, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS c FROM subscribers"
        if active_only:
            sql += " WHERE is_active=1"
        row = self._conn.execute(sql).fetchone()
        return int(row["c"] if row else 0)

    def iter_active_subscribers(self) -> Iterable[int]:
        rows = self._conn.execute(
            "SELECT chat_id FROM subscribers WHERE is_active=1 ORDER BY chat_id"
        ).fetchall()
        for row in rows:
            yield int(row["chat_id"])

    def create_post(
        self,
        source_type: str,
        source_id: str,
        source_name: str,
        external_id: str,
        title: str,
        text: str,
        link: str,
        media_url: str = "",
        media_type: str = "text",
        status: str = "pending",
        scheduled_at: int = 0,
    ) -> Optional[int]:
        fp = fingerprint(source_type, source_id, external_id or link or title or text)
        now = iso_now()
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO posts(
                        source_type, source_id, source_name, external_id, title, text, link,
                        media_url, media_type, status, fingerprint, scheduled_at,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_type,
                        source_id,
                        source_name,
                        external_id,
                        title,
                        text,
                        link,
                        media_url,
                        media_type or "text",
                        status,
                        fp,
                        scheduled_at,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                return None
            self._conn.commit()
            return int(cur.lastrowid)

    def get_post(self, post_id: int) -> Optional[Post]:
        row = self._conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return self._post_from_row(row) if row else None

    def list_posts(self, statuses: Sequence[str], limit: int = MAX_QUEUE_ITEMS) -> List[Post]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM posts WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (*statuses, int(limit)),
        ).fetchall()
        return [self._post_from_row(row) for row in rows]

    def list_due_posts(self, limit: int = 10) -> List[Post]:
        rows = self._conn.execute(
            """
            SELECT * FROM posts
            WHERE status='approved' AND (scheduled_at=0 OR scheduled_at<=?)
            ORDER BY COALESCE(NULLIF(scheduled_at, 0), id), id
            LIMIT ?
            """,
            (now_ts(), int(limit)),
        ).fetchall()
        return [self._post_from_row(row) for row in rows]

    def set_post_status(
        self,
        post_id: int,
        status: str,
        *,
        scheduled_at: Optional[int] = None,
        error: Optional[str] = None,
        published_at: Optional[int] = None,
        increment_attempts: bool = False,
    ) -> bool:
        assignments = ["status=?", "updated_at=?"]
        args: List[Any] = [status, iso_now()]
        if scheduled_at is not None:
            assignments.append("scheduled_at=?")
            args.append(int(scheduled_at))
        if error is not None:
            assignments.append("error=?")
            args.append(error)
        if published_at is not None:
            assignments.append("published_at=?")
            args.append(int(published_at))
        if increment_attempts:
            assignments.append("attempts=attempts+1")
        args.append(post_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE posts SET {', '.join(assignments)} WHERE id=?", tuple(args)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def stats(self) -> Dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS c FROM posts GROUP BY status"
        ).fetchall()
        result = {str(row["status"]): int(row["c"]) for row in rows}
        result["feeds"] = len(self.list_feeds(False))
        result["subscribers"] = self.count_subscribers(True)
        return result

    @staticmethod
    def _feed_from_row(row: sqlite3.Row) -> Feed:
        return Feed(
            id=int(row["id"]),
            url=str(row["url"]),
            title=str(row["title"]),
            enabled=bool(row["enabled"]),
            etag=str(row["etag"]),
            last_modified=str(row["last_modified"]),
            last_checked_at=int(row["last_checked_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _post_from_row(row: sqlite3.Row) -> Post:
        return Post(
            id=int(row["id"]),
            source_type=str(row["source_type"]),
            source_id=str(row["source_id"]),
            source_name=str(row["source_name"]),
            external_id=str(row["external_id"]),
            title=str(row["title"]),
            text=str(row["text"]),
            link=str(row["link"]),
            media_url=str(row["media_url"]),
            media_type=str(row["media_type"]),
            status=str(row["status"]),
            fingerprint=str(row["fingerprint"]),
            scheduled_at=int(row["scheduled_at"]),
            published_at=int(row["published_at"]),
            attempts=int(row["attempts"]),
            error=str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class TelegramError(RuntimeError):
    def __init__(self, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.payload = payload or {}

    @property
    def retry_after(self) -> int:
        params = self.payload.get("parameters") or {}
        try:
            return int(params.get("retry_after", 0))
        except (TypeError, ValueError):
            return 0


class TelegramAPI:
    """Small Telegram Bot API client with 429 handling."""

    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def request(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = REQUEST_TIMEOUT) -> Any:
        url = f"{self.base_url}/{method}"
        for attempt in range(4):
            try:
                response = self.session.post(url, json=payload or {}, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise TelegramError(f"Telegram request failed: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                data = response.json()
            except ValueError as exc:
                raise TelegramError(f"Telegram returned non-JSON HTTP {response.status_code}") from exc
            if data.get("ok"):
                return data.get("result")
            error = TelegramError(str(data.get("description") or "Telegram API error"), data)
            retry_after = error.retry_after
            if response.status_code == 429 and retry_after and attempt < 3:
                LOGGER.warning("Telegram rate limit, sleeping %s sec", retry_after)
                time.sleep(retry_after + 1)
                continue
            raise error
        raise TelegramError("Telegram request failed after retries")

    def get_updates(self, offset: int, timeout: int) -> List[Dict[str, Any]]:
        return list(
            self.request(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": timeout,
                    "allowed_updates": [
                        "message",
                        "channel_post",
                        "callback_query",
                        "my_chat_member",
                    ],
                },
                timeout=timeout + 10,
            )
            or []
        )

    def send_message(
        self,
        chat_id: Any,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_web_page_preview: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": truncate(text, MAX_TELEGRAM_TEXT),
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return dict(self.request("sendMessage", payload) or {})

    def send_photo(self, chat_id: Any, photo: str, caption: str = "") -> Dict[str, Any]:
        return dict(
            self.request(
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "photo": photo,
                    "caption": truncate(caption, MAX_CAPTION),
                },
            )
            or {}
        )

    def send_video(self, chat_id: Any, video: str, caption: str = "") -> Dict[str, Any]:
        return dict(
            self.request(
                "sendVideo",
                {
                    "chat_id": chat_id,
                    "video": video,
                    "caption": truncate(caption, MAX_CAPTION),
                    "supports_streaming": True,
                },
            )
            or {}
        )

    def send_audio(self, chat_id: Any, audio: str, caption: str = "") -> Dict[str, Any]:
        return dict(
            self.request(
                "sendAudio",
                {
                    "chat_id": chat_id,
                    "audio": audio,
                    "caption": truncate(caption, MAX_CAPTION),
                },
            )
            or {}
        )

    def send_document(self, chat_id: Any, document: str, caption: str = "") -> Dict[str, Any]:
        return dict(
            self.request(
                "sendDocument",
                {
                    "chat_id": chat_id,
                    "document": document,
                    "caption": truncate(caption, MAX_CAPTION),
                },
            )
            or {}
        )

    def copy_message(self, chat_id: Any, from_chat_id: Any, message_id: int, caption: str = "") -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        if caption:
            payload["caption"] = truncate(caption, MAX_CAPTION)
        return dict(self.request("copyMessage", payload) or {})

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self.request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def set_my_commands(self) -> None:
        commands = [
            {"command": "start", "description": "Подписаться на легальные обновления"},
            {"command": "subscribe", "description": "Включить opt-in рассылку"},
            {"command": "unsubscribe", "description": "Отключить рассылку"},
            {"command": "help", "description": "Справка"},
            {"command": "status", "description": "Статус бота (админ)"},
            {"command": "queue", "description": "Очередь модерации (админ)"},
            {"command": "feeds", "description": "RSS-источники (админ)"},
        ]
        with contextlib.suppress(Exception):
            self.request("setMyCommands", {"commands": commands})


class FeedFetcher:
    """RSS/Atom fetcher that stores only new items."""

    def __init__(self, storage: Storage, config: Config) -> None:
        self.storage = storage
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def seed_initial_feeds(self) -> None:
        for url in self.config.initial_feeds:
            try:
                feed_id = self.storage.add_feed(url)
                LOGGER.info("Registered initial feed #%s: %s", feed_id, url)
            except Exception:
                LOGGER.exception("Failed to register initial feed: %s", url)

    def check_all(self) -> int:
        total_new = 0
        for feed in self.storage.list_feeds(enabled_only=True):
            try:
                total_new += self.check_feed(feed)
            except Exception:
                LOGGER.exception("Failed to check feed #%s %s", feed.id, feed.url)
                self.storage.touch_feed(feed.id)
        return total_new

    def check_feed(self, feed: Feed) -> int:
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9"}
        if feed.etag:
            headers["If-None-Match"] = feed.etag
        if feed.last_modified:
            headers["If-Modified-Since"] = feed.last_modified
        response = self.session.get(feed.url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code == 304:
            self.storage.touch_feed(feed.id)
            return 0
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "xml" not in content_type and "rss" not in content_type and "atom" not in content_type:
            LOGGER.warning("Feed #%s returned unexpected content type: %s", feed.id, content_type)
        feed_title, items = self.parse_feed(response.content, feed.url)
        self.storage.update_feed_meta(
            feed.id,
            feed_title,
            response.headers.get("ETag", ""),
            response.headers.get("Last-Modified", ""),
        )
        status = "pending" if self.config.review_required else "approved"
        new_count = 0
        for item in items[:MAX_FEED_ITEMS_PER_RUN]:
            post_id = self.storage.create_post(
                source_type="rss",
                source_id=str(feed.id),
                source_name=feed_title or feed.title or feed.url,
                external_id=item.get("id") or item.get("link") or item.get("title") or "",
                title=item.get("title", ""),
                text=item.get("summary", ""),
                link=item.get("link", ""),
                media_url=item.get("media_url", ""),
                media_type=item.get("media_type", "text"),
                status=status,
            )
            if post_id:
                new_count += 1
                LOGGER.info("Queued post #%s from feed #%s", post_id, feed.id)
        return new_count

    @staticmethod
    def parse_feed(content: bytes, base_url: str) -> Tuple[str, List[Dict[str, str]]]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ValueError(f"Invalid XML feed: {exc}") from exc
        root_tag = FeedFetcher._strip_ns(root.tag).lower()
        if root_tag == "rss":
            return FeedFetcher._parse_rss(root, base_url)
        if root_tag == "feed":
            return FeedFetcher._parse_atom(root, base_url)
        if root.find(".//channel") is not None:
            return FeedFetcher._parse_rss(root, base_url)
        raise ValueError("Unsupported feed format")

    @staticmethod
    def _parse_rss(root: ET.Element, base_url: str) -> Tuple[str, List[Dict[str, str]]]:
        channel = root.find(".//channel")
        if channel is None:
            channel = root
        title = FeedFetcher._child_text(channel, "title")
        items: List[Dict[str, str]] = []
        for item in channel.findall(".//item"):
            link = FeedFetcher._child_text(item, "link")
            guid = FeedFetcher._child_text(item, "guid") or link
            item_title = compact_whitespace(strip_html(FeedFetcher._child_text(item, "title")))
            description = strip_html(
                FeedFetcher._child_text(item, "description")
                or FeedFetcher._child_text(item, "encoded")
                or FeedFetcher._child_text(item, "summary")
            )
            media_url, media_type = FeedFetcher._extract_media(item)
            items.append(
                {
                    "id": guid,
                    "title": item_title,
                    "summary": description,
                    "link": urllib.parse.urljoin(base_url, link) if link else "",
                    "media_url": media_url,
                    "media_type": media_type,
                    "published_at": FeedFetcher._child_text(item, "pubDate"),
                }
            )
        return title, items

    @staticmethod
    def _parse_atom(root: ET.Element, base_url: str) -> Tuple[str, List[Dict[str, str]]]:
        title = FeedFetcher._child_text(root, "title")
        items: List[Dict[str, str]] = []
        for entry in root.findall(".//{*}entry"):
            link = ""
            for link_el in entry.findall("{*}link"):
                rel = link_el.attrib.get("rel", "alternate")
                href = link_el.attrib.get("href", "")
                if href and rel == "alternate":
                    link = href
                    break
                if href and not link:
                    link = href
            item_title = compact_whitespace(strip_html(FeedFetcher._child_text(entry, "title")))
            summary = strip_html(
                FeedFetcher._child_text(entry, "summary")
                or FeedFetcher._child_text(entry, "content")
            )
            media_url, media_type = FeedFetcher._extract_media(entry)
            items.append(
                {
                    "id": FeedFetcher._child_text(entry, "id") or link or item_title,
                    "title": item_title,
                    "summary": summary,
                    "link": urllib.parse.urljoin(base_url, link) if link else "",
                    "media_url": media_url,
                    "media_type": media_type,
                    "published_at": FeedFetcher._child_text(entry, "updated")
                    or FeedFetcher._child_text(entry, "published"),
                }
            )
        return title, items

    @staticmethod
    def _strip_ns(tag: str) -> str:
        if "}" in tag:
            return tag.split("}", 1)[1]
        return tag

    @staticmethod
    def _child_text(parent: ET.Element, local_name: str) -> str:
        for child in list(parent):
            if FeedFetcher._strip_ns(child.tag).lower() == local_name.lower():
                return "".join(child.itertext()).strip()
        found = parent.find(f".//{{*}}{local_name}")
        if found is not None:
            return "".join(found.itertext()).strip()
        return ""

    @staticmethod
    def _extract_media(parent: ET.Element) -> Tuple[str, str]:
        candidates: List[Tuple[str, str]] = []
        for child in parent.iter():
            local = FeedFetcher._strip_ns(child.tag).lower()
            attrs = child.attrib
            if local in {"enclosure", "content"} and attrs.get("url"):
                candidates.append((attrs["url"], attrs.get("type", "")))
            if local in {"thumbnail"} and attrs.get("url"):
                candidates.append((attrs["url"], attrs.get("type", "image/jpeg")))
            if local == "link" and attrs.get("href") and attrs.get("type", "").startswith(("image/", "video/")):
                candidates.append((attrs["href"], attrs.get("type", "")))
        if not candidates:
            return "", "text"
        url, mime_type = candidates[0]
        return url, infer_media_type(url, mime_type)


def inline_keyboard(rows: Sequence[Sequence[Tuple[str, str]]]) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


class ContentBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.storage = Storage(config.db_path)
        self.api = TelegramAPI(config.bot_token)
        self.fetcher = FeedFetcher(self.storage, config)
        self.stop_event = threading.Event()
        self.worker_errors: "queue.Queue[str]" = queue.Queue()
        self.offset = parse_int(self.storage.get_setting("update_offset", "0"), 0)
        self.feed_next_run = 0
        self.schedule_next_run = 0

    def close(self) -> None:
        self.storage.close()

    def run(self) -> None:
        LOGGER.info("%s v%s starting", APP_NAME, APP_VERSION)
        self.fetcher.seed_initial_feeds()
        self.api.set_my_commands()
        self.notify_admins(f"✅ {APP_NAME} запущен. Версия: {APP_VERSION}")
        while not self.stop_event.is_set():
            try:
                self.periodic_jobs()
                updates = self.api.get_updates(self.offset, self.config.poll_timeout_sec)
                for update in updates:
                    self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                    self.storage.set_setting("update_offset", str(self.offset))
                    self.handle_update(update)
            except TelegramError as exc:
                LOGGER.warning("Telegram error: %s", exc)
                time.sleep(max(2, exc.retry_after or 2))
            except KeyboardInterrupt:
                break
            except Exception:
                LOGGER.exception("Main loop failure")
                time.sleep(3)
        LOGGER.info("%s stopped", APP_NAME)

    def stop(self, *_args: Any) -> None:
        self.stop_event.set()

    def periodic_jobs(self) -> None:
        current = now_ts()
        if current >= self.feed_next_run:
            self.feed_next_run = current + self.config.feed_interval_sec
            new_count = self.fetcher.check_all()
            if new_count:
                self.notify_admins(f"🆕 В очереди новых постов: {new_count}")
        if current >= self.schedule_next_run:
            self.schedule_next_run = current + self.config.scheduler_interval_sec
            self.publish_due_posts()

    def handle_update(self, update: Dict[str, Any]) -> None:
        if "message" in update:
            self.handle_message(update["message"])
        elif "channel_post" in update:
            self.handle_channel_post(update["channel_post"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        chat_id = int(chat.get("id") or from_user.get("id") or 0)
        text = str(message.get("text") or message.get("caption") or "").strip()
        if not chat_id:
            return
        if text.startswith("/"):
            self.handle_command(message, chat_id, text)
            return
        if self.is_admin(chat_id):
            if message.get("forward_from_chat") or message.get("photo") or message.get("video") or text:
                self.create_manual_post(message, chat_id)
            return
        self.api.send_message(
            chat_id,
            "Я не принимаю личные сообщения. Используйте /subscribe для добровольной подписки "
            "или /unsubscribe для отключения.",
        )

    def handle_command(self, message: Dict[str, Any], chat_id: int, text: str) -> None:
        command, args = self.split_command(text)
        command = command.lower()
        if command in {"/start", "/subscribe"}:
            from_user = dict(message.get("from") or {"id": chat_id})
            from_user.setdefault("id", chat_id)
            self.storage.upsert_subscriber(from_user, active=True)
            self.storage.add_audit(chat_id, "subscribe", str(chat_id))
            self.api.send_message(
                chat_id,
                "✅ Вы подписались на добровольные обновления. Отключение: /unsubscribe",
            )
            return
        if command == "/unsubscribe":
            self.storage.set_subscriber_active(chat_id, False)
            self.storage.add_audit(chat_id, "unsubscribe", str(chat_id))
            self.api.send_message(chat_id, "Готово, рассылка отключена.")
            return
        if command == "/help":
            self.api.send_message(chat_id, self.help_text(self.is_admin(chat_id)))
            return
        if not self.is_admin(chat_id):
            self.api.send_message(chat_id, "Эта команда доступна только администратору.")
            return
        admin_handlers: Dict[str, Callable[[int, str], None]] = {
            "/status": self.cmd_status,
            "/queue": self.cmd_queue,
            "/approve": self.cmd_approve,
            "/reject": self.cmd_reject,
            "/publish": self.cmd_publish,
            "/schedule": self.cmd_schedule,
            "/feeds": self.cmd_feeds,
            "/feed_add": self.cmd_feed_add,
            "/feed_remove": self.cmd_feed_remove,
            "/feed_on": self.cmd_feed_on,
            "/feed_off": self.cmd_feed_off,
            "/set_signature": self.cmd_set_signature,
            "/broadcast": self.cmd_broadcast,
            "/policy": self.cmd_policy,
        }
        handler = admin_handlers.get(command)
        if not handler:
            self.api.send_message(chat_id, "Неизвестная команда. /help")
            return
        try:
            handler(chat_id, args)
        except Exception as exc:
            LOGGER.exception("Command failed: %s", text)
            self.api.send_message(chat_id, f"Ошибка выполнения команды: {exc}")

    @staticmethod
    def split_command(text: str) -> Tuple[str, str]:
        parts = text.strip().split(maxsplit=1)
        command = parts[0].split("@", 1)[0]
        args = parts[1].strip() if len(parts) > 1 else ""
        return command, args

    def is_admin(self, user_id: int) -> bool:
        return int(user_id) in self.config.admin_ids

    def help_text(self, admin: bool) -> str:
        public = (
            "Команды:\n"
            "/subscribe — включить добровольную рассылку\n"
            "/unsubscribe — отключить рассылку\n"
            "/help — справка\n\n"
            "Бот работает только с разрешенными источниками и opt-in подписчиками."
        )
        if not admin:
            return public
        return (
            public
            + "\n\nАдмин-команды:\n"
            "/status — статистика\n"
            "/queue — очередь модерации\n"
            "/approve <id> — одобрить пост\n"
            "/publish <id> — опубликовать сразу\n"
            "/schedule <id> <unix_ts|YYYY-MM-DDTHH:MM:SS+00:00> — запланировать\n"
            "/reject <id> [причина] — отклонить\n"
            "/feeds — список RSS\n"
            "/feed_add <url> — добавить RSS\n"
            "/feed_remove <id> — удалить RSS\n"
            "/feed_on <id> / /feed_off <id> — включить/выключить RSS\n"
            "/set_signature <текст> — подпись/рекламная ссылка к постам\n"
            "/broadcast CONFIRM <текст> — рассылка только opt-in подписчикам\n"
            "/policy — безопасные правила работы"
        )

    def cmd_policy(self, chat_id: int, _args: str) -> None:
        self.api.send_message(
            chat_id,
            "Политика безопасности:\n"
            "• только разрешенные источники/RSS;\n"
            "• без парсинга участников чужих чатов;\n"
            "• без инвайта незнакомых пользователей;\n"
            "• без спама в ЛС;\n"
            "• рассылки только тем, кто сам подписался через /subscribe.",
        )

    def cmd_status(self, chat_id: int, _args: str) -> None:
        stats = self.storage.stats()
        lines = [
            f"{APP_NAME} v{APP_VERSION}",
            f"База: {self.config.db_path}",
            f"Целевой канал: {self.config.target_channel_id}",
            f"Премодерация: {'да' if self.config.review_required else 'нет'}",
            f"RSS источников: {stats.get('feeds', 0)}",
            f"Opt-in подписчиков: {stats.get('subscribers', 0)}",
            "",
            "Посты:",
        ]
        for status in ["pending", "approved", "published", "rejected", "failed"]:
            lines.append(f"• {status}: {stats.get(status, 0)}")
        self.api.send_message(chat_id, "\n".join(lines))

    def cmd_queue(self, chat_id: int, _args: str) -> None:
        posts = self.storage.list_posts(["pending", "approved", "failed"], MAX_QUEUE_ITEMS)
        if not posts:
            self.api.send_message(chat_id, "Очередь пуста.")
            return
        for post in posts:
            self.send_post_preview(chat_id, post)

    def cmd_approve(self, chat_id: int, args: str) -> None:
        post_id = parse_int(args, 0)
        if not post_id:
            self.api.send_message(chat_id, "Использование: /approve <id>")
            return
        if self.storage.set_post_status(post_id, "approved", error=""):
            self.storage.add_audit(chat_id, "approve", str(post_id))
            self.api.send_message(chat_id, f"Пост #{post_id} одобрен и будет опубликован планировщиком.")
        else:
            self.api.send_message(chat_id, f"Пост #{post_id} не найден.")

    def cmd_publish(self, chat_id: int, args: str) -> None:
        post_id = parse_int(args, 0)
        post = self.storage.get_post(post_id)
        if not post:
            self.api.send_message(chat_id, "Пост не найден.")
            return
        self.publish_post(post)
        self.storage.add_audit(chat_id, "publish", str(post_id))
        self.api.send_message(chat_id, f"Пост #{post_id} опубликован.")

    def cmd_schedule(self, chat_id: int, args: str) -> None:
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            self.api.send_message(chat_id, "Использование: /schedule <id> <unix_ts|YYYY-MM-DDTHH:MM:SS+00:00>")
            return
        post_id = parse_int(parts[0], 0)
        ts = parse_rfc3339_or_epoch(parts[1])
        if not post_id or not ts:
            self.api.send_message(chat_id, "Не удалось распознать id или дату.")
            return
        if self.storage.set_post_status(post_id, "approved", scheduled_at=ts, error=""):
            self.storage.add_audit(chat_id, "schedule", str(post_id), {"scheduled_at": ts})
            self.api.send_message(chat_id, f"Пост #{post_id} запланирован на {ts}.")
        else:
            self.api.send_message(chat_id, f"Пост #{post_id} не найден.")

    def cmd_reject(self, chat_id: int, args: str) -> None:
        parts = args.split(maxsplit=1)
        post_id = parse_int(parts[0] if parts else "", 0)
        reason = parts[1] if len(parts) > 1 else ""
        if not post_id:
            self.api.send_message(chat_id, "Использование: /reject <id> [причина]")
            return
        if self.storage.set_post_status(post_id, "rejected", error=reason):
            self.storage.add_audit(chat_id, "reject", str(post_id), reason)
            self.api.send_message(chat_id, f"Пост #{post_id} отклонен.")
        else:
            self.api.send_message(chat_id, f"Пост #{post_id} не найден.")

    def cmd_feeds(self, chat_id: int, _args: str) -> None:
        feeds = self.storage.list_feeds(False)
        if not feeds:
            self.api.send_message(chat_id, "RSS пока не добавлены. /feed_add <url>")
            return
        lines = ["RSS-источники:"]
        for feed in feeds:
            state = "on" if feed.enabled else "off"
            checked = feed.last_checked_at or "-"
            lines.append(f"#{feed.id} [{state}] {feed.title or feed.url}\n{feed.url}\nlast_check={checked}")
        self.api.send_message(chat_id, "\n\n".join(lines), disable_web_page_preview=True)

    def cmd_feed_add(self, chat_id: int, args: str) -> None:
        url = args.strip()
        if not url.startswith(("http://", "https://")):
            self.api.send_message(chat_id, "Использование: /feed_add https://example.com/feed.xml")
            return
        feed_id = self.storage.add_feed(url)
        self.storage.add_audit(chat_id, "feed_add", str(feed_id), url)
        self.api.send_message(chat_id, f"RSS добавлен: #{feed_id}")

    def cmd_feed_remove(self, chat_id: int, args: str) -> None:
        feed_id = parse_int(args, 0)
        if self.storage.remove_feed(feed_id):
            self.storage.add_audit(chat_id, "feed_remove", str(feed_id))
            self.api.send_message(chat_id, f"RSS #{feed_id} удален.")
        else:
            self.api.send_message(chat_id, "RSS не найден.")

    def cmd_feed_on(self, chat_id: int, args: str) -> None:
        self._cmd_feed_toggle(chat_id, args, True)

    def cmd_feed_off(self, chat_id: int, args: str) -> None:
        self._cmd_feed_toggle(chat_id, args, False)

    def _cmd_feed_toggle(self, chat_id: int, args: str, enabled: bool) -> None:
        feed_id = parse_int(args, 0)
        if self.storage.set_feed_enabled(feed_id, enabled):
            self.storage.add_audit(chat_id, "feed_toggle", str(feed_id), {"enabled": enabled})
            self.api.send_message(chat_id, f"RSS #{feed_id}: {'включен' if enabled else 'выключен'}.")
        else:
            self.api.send_message(chat_id, "RSS не найден.")

    def cmd_set_signature(self, chat_id: int, args: str) -> None:
        self.storage.set_setting("ad_signature", args)
        self.storage.add_audit(chat_id, "set_signature", "", {"length": len(args)})
        self.api.send_message(chat_id, "Подпись обновлена. Она хранится в базе и перекрывает AD_SIGNATURE из .env.")

    def cmd_broadcast(self, chat_id: int, args: str) -> None:
        if not args.startswith("CONFIRM "):
            self.api.send_message(
                chat_id,
                "Для защиты от случайной рассылки используйте:\n/broadcast CONFIRM текст сообщения\n"
                "Рассылка уйдет только пользователям, которые сами подписались.",
            )
            return
        text = args[len("CONFIRM ") :].strip()
        if not text:
            self.api.send_message(chat_id, "Текст рассылки пуст.")
            return
        total = sent = failed = 0
        for subscriber_id in self.storage.iter_active_subscribers():
            total += 1
            try:
                self.api.send_message(subscriber_id, text)
                sent += 1
            except TelegramError as exc:
                failed += 1
                msg = str(exc).lower()
                if "bot was blocked" in msg or "chat not found" in msg or "forbidden" in msg:
                    self.storage.set_subscriber_active(subscriber_id, False)
                LOGGER.warning("Broadcast failed for %s: %s", subscriber_id, exc)
            time.sleep(self.config.broadcast_delay_sec)
        self.storage.add_audit(chat_id, "broadcast", "", {"total": total, "sent": sent, "failed": failed})
        self.api.send_message(chat_id, f"Рассылка завершена. Всего: {total}, отправлено: {sent}, ошибок: {failed}.")

    def send_post_preview(self, chat_id: int, post: Post) -> None:
        text = (
            f"#{post.id} [{post.status}] {post.source_type}:{post.source_name}\n"
            f"Заголовок: {post.title or '-'}\n"
            f"Ссылка: {post.link or '-'}\n"
            f"Медиа: {post.media_type} {post.media_url or ''}\n"
            f"План: {post.scheduled_at or '-'}\n\n"
            f"{truncate(post.text, 1200)}"
        )
        keyboard = inline_keyboard(
            [
                [("✅ Одобрить", f"approve:{post.id}"), ("🚀 Опубликовать", f"publish:{post.id}")],
                [("❌ Отклонить", f"reject:{post.id}")],
            ]
        )
        self.api.send_message(chat_id, text, reply_markup=keyboard, disable_web_page_preview=True)

    def handle_callback(self, callback: Dict[str, Any]) -> None:
        from_user = callback.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        if not self.is_admin(user_id):
            self.api.answer_callback_query(callback_id, "Только администратор.")
            return
        action, _, raw_id = data.partition(":")
        post_id = parse_int(raw_id, 0)
        try:
            if action == "approve" and post_id:
                self.storage.set_post_status(post_id, "approved", error="")
                self.storage.add_audit(user_id, "approve", str(post_id))
                self.api.answer_callback_query(callback_id, "Одобрено")
            elif action == "publish" and post_id:
                post = self.storage.get_post(post_id)
                if post:
                    self.publish_post(post)
                    self.storage.add_audit(user_id, "publish", str(post_id))
                    self.api.answer_callback_query(callback_id, "Опубликовано")
                else:
                    self.api.answer_callback_query(callback_id, "Пост не найден")
            elif action == "reject" and post_id:
                self.storage.set_post_status(post_id, "rejected", error="rejected by callback")
                self.storage.add_audit(user_id, "reject", str(post_id))
                self.api.answer_callback_query(callback_id, "Отклонено")
            else:
                self.api.answer_callback_query(callback_id, "Неизвестное действие")
        except Exception as exc:
            LOGGER.exception("Callback failed")
            self.api.answer_callback_query(callback_id, f"Ошибка: {exc}")

    def handle_channel_post(self, message: Dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id") or 0)
        message_id = int(message.get("message_id") or 0)
        if chat_id not in self.config.allowed_channel_ids:
            LOGGER.info("Ignoring channel post from non-allowed channel %s", chat_id)
            return
        text = str(message.get("text") or message.get("caption") or "")
        title = str(chat.get("title") or chat.get("username") or chat_id)
        media_url = ""
        media_type = "text"
        if message_has_media(message) and message_id:
            media_type = "telegram_copy"
            media_url = f"{chat_id}:{message_id}"
        link = self.message_public_link(chat, message_id)
        status = "pending" if self.config.review_required else "approved"
        post_id = self.storage.create_post(
            source_type="telegram_channel",
            source_id=str(chat_id),
            source_name=title,
            external_id=str(message_id),
            title=truncate(compact_whitespace(text), 120),
            text=text,
            link=link,
            media_url=media_url,
            media_type=media_type,
            status=status,
        )
        if post_id:
            self.notify_admins(f"🆕 Пост #{post_id} из разрешенного канала «{title}» добавлен в очередь.")

    def create_manual_post(self, message: Dict[str, Any], admin_id: int) -> None:
        text = str(message.get("text") or message.get("caption") or "")
        forward_chat = message.get("forward_from_chat") or {}
        source_name = str(forward_chat.get("title") or "manual")
        source_id = str(forward_chat.get("id") or admin_id)
        external_id = str(message.get("forward_from_message_id") or message.get("message_id") or now_ts())
        media_url = ""
        media_type = "text"
        if message.get("photo"):
            media_type = "photo"
            media_url = (message["photo"][-1] or {}).get("file_id", "")
        elif message.get("video"):
            media_type = "video"
            media_url = (message["video"] or {}).get("file_id", "")
        elif message.get("document"):
            media_type = "document"
            media_url = (message["document"] or {}).get("file_id", "")
        elif message.get("audio"):
            media_type = "audio"
            media_url = (message["audio"] or {}).get("file_id", "")
        status = "pending"
        post_id = self.storage.create_post(
            source_type="manual",
            source_id=source_id,
            source_name=source_name,
            external_id=external_id,
            title=truncate(compact_whitespace(text), 120),
            text=text,
            link="",
            media_url=media_url,
            media_type=media_type,
            status=status,
        )
        if post_id:
            self.storage.add_audit(admin_id, "manual_post", str(post_id))
            self.api.send_message(admin_id, f"Пост #{post_id} добавлен в очередь.")
            post = self.storage.get_post(post_id)
            if post:
                self.send_post_preview(admin_id, post)
        else:
            self.api.send_message(admin_id, "Такой пост уже есть в базе.")

    @staticmethod
    def message_public_link(chat: Dict[str, Any], message_id: int) -> str:
        username = chat.get("username")
        if username and message_id:
            return f"https://t.me/{username}/{message_id}"
        return ""

    def publish_due_posts(self) -> None:
        for post in self.storage.list_due_posts(limit=10):
            try:
                self.publish_post(post)
            except Exception:
                LOGGER.exception("Failed to publish due post #%s", post.id)

    def publish_post(self, post: Post) -> None:
        caption = self.compose_post_text(post)
        try:
            if post.media_url and post.media_type == "telegram_copy":
                from_chat_id, message_id = self.parse_copy_source(post.media_url)
                self.api.copy_message(
                    self.config.target_channel_id,
                    from_chat_id,
                    message_id,
                    caption=caption,
                )
            elif post.media_url and post.media_type == "photo":
                self.api.send_photo(self.config.target_channel_id, post.media_url, caption)
            elif post.media_url and post.media_type == "video":
                self.api.send_video(self.config.target_channel_id, post.media_url, caption)
            elif post.media_url and post.media_type == "audio":
                self.api.send_audio(self.config.target_channel_id, post.media_url, caption)
            elif post.media_url and post.media_type == "document":
                self.api.send_document(self.config.target_channel_id, post.media_url, caption)
            else:
                self.api.send_message(self.config.target_channel_id, caption, disable_web_page_preview=False)
            self.storage.set_post_status(
                post.id,
                "published",
                published_at=now_ts(),
                error="",
                increment_attempts=True,
            )
            LOGGER.info("Published post #%s", post.id)
        except TelegramError as exc:
            self.storage.set_post_status(
                post.id,
                "failed",
                error=str(exc),
                increment_attempts=True,
            )
            self.notify_admins(f"⚠️ Не удалось опубликовать пост #{post.id}: {exc}")
            raise

    @staticmethod
    def parse_copy_source(value: str) -> Tuple[str, int]:
        chat_id, _, raw_message_id = value.partition(":")
        message_id = parse_int(raw_message_id, 0)
        if not chat_id or not message_id:
            raise ValueError("invalid telegram copy source")
        return chat_id, message_id

    def compose_post_text(self, post: Post) -> str:
        pieces: List[str] = []
        body = post.text.strip()
        if post.title and post.title not in body:
            pieces.append(post.title.strip())
        if body:
            pieces.append(body)
        if self.config.append_source_link and post.link:
            pieces.append(f"Источник: {post.link}")
        signature = self.storage.get_setting("ad_signature", self.config.ad_signature).strip()
        if signature:
            pieces.append(signature)
        text = "\n\n".join(piece for piece in pieces if piece)
        return truncate(text or post.link or "(без текста)", MAX_TELEGRAM_TEXT)

    def notify_admins(self, text: str) -> None:
        for admin_id in self.config.admin_ids:
            try:
                self.api.send_message(admin_id, text)
            except Exception:
                LOGGER.warning("Failed to notify admin %s", admin_id, exc_info=True)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    parser.add_argument("--check-config", action="store_true", help="validate environment and exit")
    parser.add_argument("--init-db", action="store_true", help="initialize database and exit")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level)
    if args.check_config:
        print("Configuration OK")
        return 0
    if args.init_db:
        storage = Storage(config.db_path)
        fetcher = FeedFetcher(storage, config)
        fetcher.seed_initial_feeds()
        storage.close()
        print(f"Database initialized: {config.db_path}")
        return 0
    bot = ContentBot(config)
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    try:
        bot.run()
        return 0
    finally:
        bot.close()


if __name__ == "__main__":
    raise SystemExit(main())
