"""
Structured logging for the Dwar bot.

Features
--------
* Rotating file handler  → logs/bot.log  (10 MB × 5 backups)
* Coloured console output via StreamHandler
* Optional async Telegram handler — forwards WARNING+ to a Telegram chat
* Log-retention cleanup  (deletes files older than LOG_RETENTION_DAYS)
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import time
import traceback
from pathlib import Path
from typing import Optional

from dwar_bot.config import (
    LOG_FILE,
    LOGS_DIR,
    LOG_RETENTION_DAYS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_MIN_LEVEL,
    TELEGRAM_RATE_LIMIT,
)

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_FMT = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_LEVEL_COLOURS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[41m",   # red background
}
_RESET = "\033[0m"


class _ColouredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        colour = _LEVEL_COLOURS.get(record.levelname, "")
        record.levelname = f"{colour}{record.levelname}{_RESET}"
        return super().format(record)


# ---------------------------------------------------------------------------
# Telegram rate-limited async handler
# ---------------------------------------------------------------------------

class _TelegramHandler(logging.Handler):
    """
    Non-blocking Telegram log handler.

    Queues log records and flushes them from a background asyncio task,
    respecting TELEGRAM_RATE_LIMIT messages per minute.
    """

    def __init__(self, token: str, chat_id: str, rate_limit: int = TELEGRAM_RATE_LIMIT) -> None:
        super().__init__()
        self._token = token
        self._chat_id = chat_id
        self._rate_limit = rate_limit
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            # Truncate to Telegram message limit
            if len(text) > 4096:
                text = text[:4090] + "…"
            loop = self._get_loop()
            if loop is not None:
                if self._task is None or self._task.done():
                    self._task = loop.create_task(self._flush_loop())
                try:
                    self._queue.put_nowait(text)
                except asyncio.QueueFull:
                    pass  # drop if queue is saturated
        except Exception:
            self.handleError(record)

    async def _flush_loop(self) -> None:
        """Background task: drain the queue respecting the rate limit."""
        import httpx

        interval = 60.0 / max(1, self._rate_limit)
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"

        while True:
            try:
                text = await asyncio.wait_for(self._queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                break

            payload = {
                "chat_id": self._chat_id,
                "text": f"🤖 DwarBot\n{text}",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code not in (200, 429):
                        logging.getLogger(__name__).debug(
                            "Telegram API returned %d", resp.status_code
                        )
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "Telegram send failed: %s", exc
                )
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Public setup function
# ---------------------------------------------------------------------------

def setup_logging(
    level: str = "INFO",
    telegram_token: str = TELEGRAM_BOT_TOKEN,
    telegram_chat_id: str = TELEGRAM_CHAT_ID,
    telegram_min_level: str = TELEGRAM_MIN_LEVEL,
) -> None:
    """
    Configure the root logger.

    Call once at application start-up (before any ``logging.getLogger()`` calls).

    Parameters
    ----------
    level:
        Root log level string (``"DEBUG"``, ``"INFO"``, …).
    telegram_token / telegram_chat_id / telegram_min_level:
        Telegram forwarding settings.  Forwarding is disabled when
        *telegram_token* or *telegram_chat_id* is empty.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove default handlers added by basicConfig or previous setup calls
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()

    formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)
    colour_formatter = _ColouredFormatter(_FMT, datefmt=_DATE_FMT)

    # --- Console ---
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(colour_formatter)
    root.addHandler(console_handler)

    # --- Rotating file ---
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # --- Telegram (optional) ---
    if telegram_token and telegram_chat_id:
        tg_level = getattr(logging, telegram_min_level.upper(), logging.WARNING)
        tg_handler = _TelegramHandler(telegram_token, telegram_chat_id)
        tg_handler.setLevel(tg_level)
        tg_handler.setFormatter(logging.Formatter("%(levelname)s — %(message)s"))
        root.addHandler(tg_handler)
        logging.getLogger(__name__).debug(
            "Telegram log forwarding enabled (min level: %s).", telegram_min_level
        )

    logging.getLogger(__name__).info(
        "Logging initialised — level=%s  file=%s", level, LOG_FILE
    )

    _cleanup_old_logs()


def _cleanup_old_logs() -> None:
    """Delete log files older than LOG_RETENTION_DAYS."""
    cutoff = time.time() - LOG_RETENTION_DAYS * 86_400
    for p in LOGS_DIR.glob("*.log*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                logging.getLogger(__name__).debug("Deleted old log file: %s", p.name)
        except OSError:
            pass


def log_exception(logger: logging.Logger, msg: str, exc: BaseException) -> None:
    """Log *exc* at ERROR level with a full formatted traceback."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("%s\n%s", msg, tb)
