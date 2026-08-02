"""
Background log monitor — fully automatic heal loop.

Every N seconds tails ``bot.log``, detects fatal errors, pauses briefly,
runs Cursor self-healer, verifies tests, and **always** resumes the bot
(with retry cooldown on failure — no permanent stuck pause).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from dwar_bot.config import LOG_FILE
from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.cursor_self_healer import ensure_cursor_cli, patch_code_with_cursor

logger = logging.getLogger(__name__)

ERROR_MARKERS = (
    "Traceback",
    "CRITICAL",
    "TimeoutError",
    "ElementHandleError",
    "DOM-Desync",
    "FileNotFoundError",
    "TokenExpiredError",
)

# Soft markers only count when accompanied by a real exception/traceback
SOFT_MARKERS = (" ERROR ", "| ERROR |", "ERROR —", " ERROR|")

_FILE_RE = re.compile(r'File\s+"([^"]+\.py)"', re.MULTILINE)
_TRACEBACK_RE = re.compile(
    r"(Traceback \(most recent call last\):.*?)(?=\n\d{4}-\d{2}-\d{2}|\n[A-Z]+\s+\||\Z)",
    re.DOTALL,
)
_EXCEPTION_RE = re.compile(
    r"^([A-Za-z_][\w\.]*Error|[A-Za-z_][\w\.]*Exception):\s*.+$",
    re.MULTILINE,
)

NotifyFn = Callable[[str], Awaitable[None]]
PauseFn = Callable[[], Awaitable[None]]
ResumeFn = Callable[[], Awaitable[None]]

# After a failed heal, wait this long before trying the same fingerprint again
RETRY_COOLDOWN_SEC = 600
MAX_HEAL_ATTEMPTS_PER_FP = 3


class LogWatcher:
    """Tail ``bot.log`` by byte offset and auto-heal on fatal markers."""

    def __init__(
        self,
        log_path: Optional[Path] = None,
        *,
        interval_seconds: int = 300,
        notify_fn: Optional[NotifyFn] = None,
        pause_fn: Optional[PauseFn] = None,
        resume_fn: Optional[ResumeFn] = None,
    ) -> None:
        self.log_path = Path(log_path) if log_path else LOG_FILE
        self.interval_seconds = max(30, int(interval_seconds))
        self._notify = notify_fn
        self._pause = pause_fn
        self._resume = resume_fn
        self._offset: int = 0
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._heal_lock = asyncio.Lock()
        self._last_fingerprint: str = ""
        self._fp_attempts: dict[str, int] = {}
        self._fp_cooldown_until: dict[str, float] = {}
        self._cli_ready = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._seek_end()
        self._task = asyncio.create_task(self._loop(), name="log_watcher")
        logger.info(
            "LogWatcher started — interval=%ds file=%s offset=%d",
            self.interval_seconds, self.log_path, self._offset,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("LogWatcher stopped.")

    def _seek_end(self) -> None:
        try:
            self._offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        except OSError:
            self._offset = 0

    def _read_new(self) -> str:
        try:
            if not self.log_path.exists():
                return ""
            size = self.log_path.stat().st_size
            if size < self._offset:
                self._offset = 0
            if size == self._offset:
                return ""
            with self.log_path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
            return chunk
        except OSError as exc:
            logger.debug("LogWatcher read error: %s", exc)
            return ""

    @staticmethod
    def _is_actionable(chunk: str) -> bool:
        """True only for real crashes — ignore soft ERROR spam without traceback."""
        if any(m in chunk for m in ERROR_MARKERS):
            return True
        if _EXCEPTION_RE.search(chunk) and any(m in chunk for m in SOFT_MARKERS):
            return True
        return False

    @staticmethod
    def _extract_traceback(chunk: str) -> str:
        m = _TRACEBACK_RE.search(chunk)
        if m:
            return m.group(1).strip()
        for marker in (
            "Traceback (most recent call last):",
            "CRITICAL",
            "FileNotFoundError",
            "TokenExpiredError",
            "ERROR",
        ):
            idx = chunk.find(marker)
            if idx >= 0:
                return chunk[idx: idx + 4000].strip()
        return chunk[-4000:].strip()

    @staticmethod
    def _extract_failed_file(traceback_text: str, chunk: str) -> str:
        paths = _FILE_RE.findall(traceback_text) or _FILE_RE.findall(chunk)
        for p in reversed(paths):
            norm = p.replace("\\", "/")
            if "/dwar_bot/" in norm or norm.startswith("dwar_bot/"):
                idx = norm.find("dwar_bot/")
                return norm[idx:] if idx >= 0 else norm
        if paths:
            return paths[-1]
        m = re.search(
            r"dwar_bot\.(?:modules|core|auth)\.([a-zA-Z0-9_]+)",
            chunk,
        )
        if m:
            name = m.group(1)
            root = Path(__file__).resolve().parents[2]
            for folder in ("modules", "core", "auth"):
                candidate = f"dwar_bot/{folder}/{name}.py"
                if (root / candidate).exists():
                    return candidate
        return "dwar_bot/main.py"

    async def _send(self, text: str) -> None:
        if not self._notify:
            logger.info("LogWatcher notify: %s", text[:200])
            return
        try:
            await self._notify(text)
        except Exception as exc:
            logger.debug("LogWatcher notify failed: %s", exc)

    async def _pause_bot(self) -> None:
        set_bot_state(BotState.PAUSED)
        if self._pause:
            try:
                await self._pause()
            except Exception as exc:
                logger.debug("pause_fn: %s", exc)

    async def _resume_bot(self) -> None:
        set_bot_state(BotState.RUNNING)
        if self._resume:
            try:
                await self._resume()
            except Exception as exc:
                logger.debug("resume_fn: %s", exc)

    async def _ensure_cli(self) -> None:
        if self._cli_ready:
            return
        try:
            path = await asyncio.to_thread(ensure_cursor_cli)
            self._cli_ready = True
            logger.info("LogWatcher: Cursor CLI ready (%s)", path)
        except Exception as exc:
            logger.error("LogWatcher: CLI auto-install failed: %s", exc)
            self._cli_ready = False

    async def _handle_error_chunk(self, chunk: str) -> None:
        traceback_text = self._extract_traceback(chunk)
        failed_file = self._extract_failed_file(traceback_text, chunk)
        fingerprint = f"{failed_file}:{hash(traceback_text[-500:])}"

        now = time.time()
        cooldown_until = self._fp_cooldown_until.get(fingerprint, 0)
        if now < cooldown_until:
            logger.debug(
                "LogWatcher: fingerprint cooling down (%.0fs left)",
                cooldown_until - now,
            )
            return

        attempts = self._fp_attempts.get(fingerprint, 0)
        if attempts >= MAX_HEAL_ATTEMPTS_PER_FP:
            # Back off longer, but keep bot running
            self._fp_cooldown_until[fingerprint] = now + RETRY_COOLDOWN_SEC * 3
            logger.warning(
                "LogWatcher: max heal attempts for %s — cooldown, bot keeps running.",
                failed_file,
            )
            await self._send(
                f"⚠️ <b>LogWatcher:</b> ошибка в <code>{failed_file}</code> "
                f"повторяется ({attempts} попыток). Беру паузу {RETRY_COOLDOWN_SEC * 3 // 60} мин "
                f"на авто-фикс, бот <b>продолжает</b> работу.\n"
                f"<pre>{traceback_text[:800]}</pre>"
            )
            return

        if fingerprint == self._last_fingerprint and attempts > 0:
            # Same error again after resume — allowed if cooldown passed
            pass
        self._last_fingerprint = fingerprint

        logger.warning(
            "LogWatcher: actionable error → file=%s attempt=%d",
            failed_file, attempts + 1,
        )

        async with self._heal_lock:
            await self._ensure_cli()
            await self._pause_bot()
            set_bot_state(BotState.HEALING)
            self._fp_attempts[fingerprint] = attempts + 1

            ok = await asyncio.to_thread(
                patch_code_with_cursor, failed_file, traceback_text
            )

            # ALWAYS resume — fully automatic, never stick on PAUSED
            await self._resume_bot()

            if ok:
                self._fp_attempts.pop(fingerprint, None)
                self._fp_cooldown_until.pop(fingerprint, None)
                await self._send(
                    f"🔍 <b>LogWatcher (авто):</b> Ошибка в <code>{failed_file}</code>.\n"
                    f"Код исправлен через Cursor Agent, тесты OK — бот продолжил работу."
                )
                logger.info("LogWatcher: heal OK for %s — auto-resumed.", failed_file)
            else:
                self._fp_cooldown_until[fingerprint] = now + RETRY_COOLDOWN_SEC
                await self._send(
                    f"🔄 <b>LogWatcher (авто-повтор):</b> Не удалось сразу починить "
                    f"<code>{failed_file}</code>.\n"
                    f"Бот <b>снят с паузы</b> и продолжит работу. "
                    f"Повторный авто-фикс через {RETRY_COOLDOWN_SEC // 60} мин.\n"
                    f"<pre>{traceback_text[:1200]}</pre>"
                )
                logger.error(
                    "LogWatcher: heal failed for %s — auto-resumed, retry in %ds",
                    failed_file, RETRY_COOLDOWN_SEC,
                )

    async def _loop(self) -> None:
        # Bootstrap CLI in background ASAP
        asyncio.create_task(self._ensure_cli(), name="log_watcher_cli_boot")
        await asyncio.sleep(min(45, self.interval_seconds))

        while self._running:
            try:
                if get_bot_state() == BotState.HEALING:
                    await asyncio.sleep(5)
                    continue

                chunk = self._read_new()
                if chunk and self._is_actionable(chunk):
                    filtered = "\n".join(
                        line for line in chunk.splitlines()
                        if "log_watcher" not in line
                        and "cursor_self_healer" not in line
                        and "httpx" not in line
                    )
                    if self._is_actionable(filtered):
                        await self._handle_error_chunk(filtered)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("LogWatcher loop error: %s", exc)

            await asyncio.sleep(self.interval_seconds)


async def start_log_monitoring(
    interval_seconds: int = 300,
    *,
    log_path: Optional[Path] = None,
    notify_fn: Optional[NotifyFn] = None,
    pause_fn: Optional[PauseFn] = None,
    resume_fn: Optional[ResumeFn] = None,
) -> None:
    """
    Background LogWatcher loop (blocks until cancelled).

    Fully automatic: CLI install → detect → heal → pytest → always resume.
    """
    watcher = LogWatcher(
        log_path=log_path,
        interval_seconds=interval_seconds,
        notify_fn=notify_fn,
        pause_fn=pause_fn,
        resume_fn=resume_fn,
    )
    watcher._seek_end()
    watcher._running = True
    logger.info(
        "LogWatcher started — interval=%ds file=%s offset=%d (fully automatic)",
        watcher.interval_seconds, watcher.log_path, watcher._offset,
    )
    try:
        await watcher._loop()
    except asyncio.CancelledError:
        logger.info("LogWatcher cancelled.")
        raise
    finally:
        watcher._running = False
        logger.info("LogWatcher stopped.")
