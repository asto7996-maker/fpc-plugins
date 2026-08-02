"""
Background log monitor — every N seconds scans ``bot.log`` for fatal markers
and triggers Cursor self-healing via ``cursor_self_healer.patch_code_with_cursor``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from dwar_bot.config import LOG_FILE
from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.cursor_self_healer import patch_code_with_cursor

logger = logging.getLogger(__name__)

ERROR_MARKERS = (
    "ERROR",
    "CRITICAL",
    "Traceback",
    "TimeoutError",
    "ElementHandleError",
    "DOM-Desync",
)

# File "…/dwar_bot/modules/combat_engine.py", line 42, in foo
_FILE_RE = re.compile(
    r'File\s+"([^"]+\.py)"',
    re.MULTILINE,
)
_TRACEBACK_RE = re.compile(
    r"(Traceback \(most recent call last\):.*?)(?=\n\d{4}-\d{2}-\d{2}|\n[A-Z]+\s+\||\Z)",
    re.DOTALL,
)

NotifyFn = Callable[[str], Awaitable[None]]
PauseFn = Callable[[], Awaitable[None]]
ResumeFn = Callable[[], Awaitable[None]]


class LogWatcher:
    """Tail ``bot.log`` by byte offset and heal on error markers."""

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

    # ------------------------------------------------------------------
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
            if self.log_path.exists():
                self._offset = self.log_path.stat().st_size
            else:
                self._offset = 0
        except OSError:
            self._offset = 0

    def _read_new(self) -> str:
        """Read bytes since last offset using file.tell()-style tracking."""
        try:
            if not self.log_path.exists():
                return ""
            size = self.log_path.stat().st_size
            if size < self._offset:
                # Log rotated / truncated
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
    def _chunk_has_error(chunk: str) -> bool:
        return any(m in chunk for m in ERROR_MARKERS)

    @staticmethod
    def _extract_traceback(chunk: str) -> str:
        m = _TRACEBACK_RE.search(chunk)
        if m:
            return m.group(1).strip()
        # Fallback: from first marker to end (capped)
        for marker in ("Traceback (most recent call last):", "CRITICAL", "ERROR"):
            idx = chunk.find(marker)
            if idx >= 0:
                return chunk[idx: idx + 4000].strip()
        return chunk[-4000:].strip()

    @staticmethod
    def _extract_failed_file(traceback_text: str, chunk: str) -> str:
        paths = _FILE_RE.findall(traceback_text) or _FILE_RE.findall(chunk)
        # Prefer project files under dwar_bot/
        for p in reversed(paths):
            norm = p.replace("\\", "/")
            if "/dwar_bot/" in norm or norm.startswith("dwar_bot/"):
                # return path from dwar_bot/ onward when absolute
                idx = norm.find("dwar_bot/")
                return norm[idx:] if idx >= 0 else norm
        if paths:
            return paths[-1]
        # Heuristic from logger name lines: dwar_bot.modules.combat_engine
        m = re.search(
            r"dwar_bot\.(?:modules|core|auth)\.([a-zA-Z0-9_]+)",
            chunk,
        )
        if m:
            name = m.group(1)
            for folder in ("modules", "core", "auth"):
                candidate = f"dwar_bot/{folder}/{name}.py"
                if Path(candidate).exists() or (Path(__file__).resolve().parents[2] / candidate).exists():
                    return candidate
        return "dwar_bot/main.py"

    async def _send(self, text: str) -> None:
        if not self._notify:
            logger.info("LogWatcher notify (no Telegram): %s", text[:200])
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

    async def _handle_error_chunk(self, chunk: str) -> None:
        traceback_text = self._extract_traceback(chunk)
        failed_file = self._extract_failed_file(traceback_text, chunk)
        fingerprint = f"{failed_file}:{hash(traceback_text[-500:])}"
        if fingerprint == self._last_fingerprint:
            logger.debug("LogWatcher: duplicate error fingerprint — skip.")
            return
        self._last_fingerprint = fingerprint

        logger.warning(
            "LogWatcher: error detected in logs → file=%s",
            failed_file,
        )

        async with self._heal_lock:
            await self._pause_bot()
            set_bot_state(BotState.HEALING)

            # Offload blocking subprocess work to a thread
            ok = await asyncio.to_thread(
                patch_code_with_cursor, failed_file, traceback_text
            )

            if ok:
                # Keep offset at current end (already advanced by _read_new)
                await self._resume_bot()
                await self._send(
                    f"🔍 <b>LogWatcher (300s Check):</b> В логах обнаружена ошибка в "
                    f"<code>{failed_file}</code>. Код успешно исправлен через Cursor API "
                    f"и проверен тестами! Бот продолжит работу."
                )
                logger.info("LogWatcher: heal succeeded for %s — resumed.", failed_file)
            else:
                set_bot_state(BotState.PAUSED)
                await self._send(
                    f"🆘 <b>LogWatcher SOS:</b> Ошибка в <code>{failed_file}</code>.\n"
                    f"Авто-исправление через Cursor не удалось (таймаут / тесты / CLI).\n"
                    f"Бот остаётся на <b>паузе</b>. Проверь логи и правь вручную, "
                    f"затем /resume.\n\n"
                    f"<pre>{traceback_text[:1500]}</pre>"
                )
                logger.error("LogWatcher: heal FAILED for %s — bot stays paused.", failed_file)

    async def _loop(self) -> None:
        # Initial delay so startup noise is not treated as a crash
        await asyncio.sleep(min(60, self.interval_seconds))
        while self._running:
            try:
                # Skip scan while already healing / manually paused by healer
                if get_bot_state() == BotState.HEALING:
                    await asyncio.sleep(self.interval_seconds)
                    continue

                chunk = self._read_new()
                if chunk and self._chunk_has_error(chunk):
                    # Ignore watcher/healer own ERROR lines to avoid recursion
                    filtered = "\n".join(
                        line for line in chunk.splitlines()
                        if "log_watcher" not in line
                        and "cursor_self_healer" not in line
                    )
                    if self._chunk_has_error(filtered):
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

    Start from ``main.py`` as::

        asyncio.create_task(start_log_monitoring(300, notify_fn=..., pause_fn=..., resume_fn=...))
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
        "LogWatcher started — interval=%ds file=%s offset=%d",
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
