"""
Background log monitor — detects fatal log markers and triggers AutoHealer.

Fully automatic: ANSI-stripped matching, boot scan of recent log tail,
60s default interval, delegates healing to ``auto_healer``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from dwar_bot.config import LOG_FILE
from dwar_bot.core.auto_healer import HealRequest, get_auto_healer
from dwar_bot.core.bot_state import BotState, get_bot_state
from dwar_bot.core.cursor_self_healer import consume_skip_boot_scan, ensure_cursor_cli

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

ERROR_MARKERS = (
    "Traceback",
    "CRITICAL",
    "TimeoutError",
    "ElementHandleError",
    "DOM-Desync",
    "FileNotFoundError",
    "STAGNATION",
)

# After strip ANSI, these mean a real error line
SOFT_ERROR_RE = re.compile(r"(?:\|\s*)?ERROR(?:\s*\|)?")

_FILE_RE = re.compile(r'File\s+"([^"]+\.py)"', re.MULTILINE)
_TRACEBACK_RE = re.compile(
    r"(Traceback \(most recent call last\):.*?)(?=\n\d{4}-\d{2}-\d{2}|\n[A-Z]+\s+\||\Z)",
    re.DOTALL,
)
_EXCEPTION_RE = re.compile(
    r"^([A-Za-z_][\w\.]*(?:Error|Exception)):\s*.+$",
    re.MULTILINE,
)

NotifyFn = Callable[[str], Awaitable[None]]
PauseFn = Callable[[], Awaitable[None]]
ResumeFn = Callable[[], Awaitable[None]]


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


class LogWatcher:
    def __init__(
        self,
        log_path: Optional[Path] = None,
        *,
        interval_seconds: int = 60,
        notify_fn: Optional[NotifyFn] = None,
        pause_fn: Optional[PauseFn] = None,
        resume_fn: Optional[ResumeFn] = None,
    ) -> None:
        self.log_path = Path(log_path) if log_path else LOG_FILE
        self.interval_seconds = max(15, int(interval_seconds))
        self._notify = notify_fn
        self._pause = pause_fn
        self._resume = resume_fn
        self._offset: int = 0
        self._running = False
        self._cli_ready = False

    def _seek_end(self) -> None:
        try:
            self._offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        except OSError:
            self._offset = 0

    def _boot_scan_tail(self, max_bytes: int = 80_000) -> str:
        """Read the last N bytes so recent crashes aren't missed after restart."""
        try:
            if not self.log_path.exists():
                return ""
            size = self.log_path.stat().st_size
            start = max(0, size - max_bytes)
            with self.log_path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(start)
                data = fh.read()
                self._offset = fh.tell()
            return data
        except OSError:
            return ""

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
        clean = _strip_ansi(chunk)
        # Auth/cookie expiry needs a human cookie paste — Cursor cannot fix it
        low = clean.lower()
        if (
            "token expired" in low
            or "access_token has expired" in low
            or "oauth access_token expired" in low
            or "waiting for fresh cookies" in low
            or "нужны свежие куки" in low
            or "soc_auth.php" in low and "token" in low
        ):
            return False
        if any(m in clean for m in ERROR_MARKERS):
            return True
        if SOFT_ERROR_RE.search(clean) and (
            _EXCEPTION_RE.search(clean) or "Error in tick" in clean or "Fatal" in clean
        ):
            return True
        return False

    @staticmethod
    def _extract_traceback(chunk: str) -> str:
        clean = _strip_ansi(chunk)
        m = _TRACEBACK_RE.search(clean)
        if m:
            return m.group(1).strip()
        for marker in (
            "Traceback (most recent call last):",
            "CRITICAL",
            "Error in tick",
            "Fatal",
            "STAGNATION",
            "ERROR",
        ):
            idx = clean.find(marker)
            if idx >= 0:
                return clean[idx: idx + 4000].strip()
        return clean[-4000:].strip()

    @staticmethod
    def _extract_failed_file(traceback_text: str, chunk: str) -> str:
        paths = _FILE_RE.findall(traceback_text) or _FILE_RE.findall(_strip_ansi(chunk))
        for p in reversed(paths):
            norm = p.replace("\\", "/")
            if "dwar_bot/" in norm:
                return norm[norm.find("dwar_bot/"):]
        if paths:
            return paths[-1]
        m = re.search(r"dwar_bot\.(?:modules|core|auth)\.([a-zA-Z0-9_]+)", chunk)
        if m:
            name = m.group(1)
            root = Path(__file__).resolve().parents[2]
            for folder in ("modules", "core", "auth"):
                candidate = f"dwar_bot/{folder}/{name}.py"
                if (root / candidate).exists():
                    return candidate
        return "dwar_bot/main.py"

    async def _ensure_cli(self) -> None:
        if self._cli_ready:
            return
        try:
            path = await asyncio.to_thread(ensure_cursor_cli)
            self._cli_ready = True
            logger.info("LogWatcher: Cursor CLI ready (%s)", path)
        except Exception as exc:
            logger.error("LogWatcher: CLI bootstrap failed: %s", exc)

    async def _handle_chunk(self, chunk: str) -> None:
        clean = _strip_ansi(chunk)
        filtered = "\n".join(
            line for line in clean.splitlines()
            if "log_watcher" not in line
            and "cursor_self_healer" not in line
            and "auto_healer" not in line
            and "httpx" not in line
        )
        if not self._is_actionable(filtered):
            return

        tb = self._extract_traceback(filtered)
        failed = self._extract_failed_file(tb, filtered)
        logger.warning("LogWatcher: actionable → %s", failed)

        healer = get_auto_healer()
        # Bind pause/resume/notify if provided at watcher start
        if self._notify and not healer.notify_fn:
            healer.notify_fn = self._notify
        if self._pause and not healer.pause_fn:
            healer.pause_fn = self._pause
        if self._resume and not healer.resume_fn:
            healer.resume_fn = self._resume

        await self._ensure_cli()
        await healer.heal(HealRequest(
            failed_file=failed,
            traceback_text=tb,
            reason="log",
        ))

    async def _loop(self) -> None:
        asyncio.create_task(self._ensure_cli(), name="log_watcher_cli_boot")

        # Boot scan once — then always advance offset to EOF so the same
        # historical crash cannot re-trigger heal after service restart.
        # After a successful heal+restart the skip marker is set so we do not
        # immediately re-heal the same (still-fresh) log lines.
        if consume_skip_boot_scan():
            logger.info("LogWatcher: post-heal restart — skip boot-scan, seek EOF.")
            self._seek_end()
        else:
            boot = self._boot_scan_tail()
            if boot and self._is_actionable(boot):
                if _boot_already_healed(boot):
                    logger.info("LogWatcher: boot-scan errors already healed — skipping.")
                elif _boot_error_is_fresh(boot):
                    logger.info("LogWatcher: boot-scan found fresh errors — healing.")
                    await self._handle_chunk(boot)
                else:
                    logger.info("LogWatcher: boot-scan errors are stale — skipping.")
            self._seek_end()

        await asyncio.sleep(min(20, self.interval_seconds))

        while self._running:
            try:
                # Never compete with an in-flight heal
                if get_bot_state() in (BotState.HEALING, BotState.PAUSED):
                    await asyncio.sleep(3)
                    continue
                chunk = self._read_new()
                if chunk:
                    await self._handle_chunk(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("LogWatcher loop error: %s", exc)
            await asyncio.sleep(self.interval_seconds)


def _boot_error_is_fresh(chunk: str, max_age_sec: int = 900) -> bool:
    """Return True if the newest timestamp in chunk is within max_age_sec."""
    import datetime as _dt
    stamps = re.findall(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", chunk)
    if not stamps:
        return True  # unknown age — allow once
    try:
        latest = _dt.datetime.strptime(stamps[-1], "%Y-%m-%d %H:%M:%S")
        # Assume log timestamps are local server time
        age = (_dt.datetime.now() - latest).total_seconds()
        return age <= max_age_sec
    except ValueError:
        return True


def _boot_already_healed(chunk: str) -> bool:
    """True if the newest actionable error was already followed by a SUCCESS heal."""
    clean = _strip_ansi(chunk)
    success_idx = max(
        clean.rfind("Self-heal SUCCESS"),
        clean.rfind("AutoHealer SUCCESS"),
    )
    if success_idx < 0:
        return False
    last_err = -1
    for marker in (
        "Traceback (most recent call last):",
        "Error in tick",
        "STAGNATION",
        "Fatal",
    ):
        last_err = max(last_err, clean.rfind(marker))
    if last_err < 0:
        return True
    return last_err < success_idx


async def start_log_monitoring(
    interval_seconds: int = 60,
    *,
    log_path: Optional[Path] = None,
    notify_fn: Optional[NotifyFn] = None,
    pause_fn: Optional[PauseFn] = None,
    resume_fn: Optional[ResumeFn] = None,
) -> None:
    """Background LogWatcher loop (blocks until cancelled)."""
    watcher = LogWatcher(
        log_path=log_path,
        interval_seconds=interval_seconds,
        notify_fn=notify_fn,
        pause_fn=pause_fn,
        resume_fn=resume_fn,
    )
    watcher._running = True
    logger.info(
        "LogWatcher started — interval=%ds file=%s (auto-heal via AutoHealer)",
        watcher.interval_seconds, watcher.log_path,
    )
    try:
        await watcher._loop()
    except asyncio.CancelledError:
        logger.info("LogWatcher cancelled.")
        raise
    finally:
        watcher._running = False
        logger.info("LogWatcher stopped.")
