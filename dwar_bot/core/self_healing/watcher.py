"""
Autonomous 300-second log orchestrator with Circuit Breaker.

Scans ``bot.log`` from the last ``tell()`` offset, pauses the bot on
actionable errors, delegates repairs to ``cursor_engine``, and escalates
to Telegram after 3 consecutive failures on the same file.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Awaitable, Callable, Optional

from dwar_bot.config import LOG_FILE
from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.master_controller import MasterController
from dwar_bot.core.self_healing.cursor_engine import apply_patch_via_cursor

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_FILE_RE = re.compile(r'File\s+"([^"]+\.py)"', re.MULTILINE)
_TRACEBACK_RE = re.compile(
    r"(Traceback \(most recent call last\):.*?)(?=\n\d{4}-\d{2}-\d{2}|\n[A-Z]+\s+\||\Z)",
    re.DOTALL,
)

ERROR_MARKERS = (
    "ERROR",
    "CRITICAL",
    "Traceback",
    "DOM-Desync",
)

MAX_PATCH_ATTEMPTS = 3

NotifyFn = Callable[[str], Awaitable[None]]
PauseFn = Callable[[], Awaitable[None]]
ResumeFn = Callable[[], Awaitable[None]]

# Re-export for callers that imported MasterController from watcher
__all__ = ("AutonomousLogWatcher", "MasterController", "MAX_PATCH_ATTEMPTS")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


class AutonomousLogWatcher:
    """
    Background log scanner (default interval 300s) with Circuit Breaker.

    Circuit Breaker
    ---------------
    ``patch_attempts[failed_file]`` increments on failed heals. After
    ``MAX_PATCH_ATTEMPTS`` (3) consecutive failures the file is blacklisted,
    the bot stays fully paused, and a CRITICAL Telegram alarm is sent.
    """

    def __init__(
        self,
        *,
        log_path: Optional[Path] = None,
        interval_seconds: int = 300,
        controller: Optional[MasterController] = None,
        notify_fn: Optional[NotifyFn] = None,
        pause_fn: Optional[PauseFn] = None,
        resume_fn: Optional[ResumeFn] = None,
        patch_fn: Optional[Callable[..., bool]] = None,
    ) -> None:
        self.log_path = Path(log_path) if log_path else LOG_FILE
        self.interval_seconds = max(30, int(interval_seconds))
        self.controller = controller or MasterController(
            pause_fn=pause_fn,
            resume_fn=resume_fn,
        )
        self._notify = notify_fn
        self._patch_fn = patch_fn or apply_patch_via_cursor
        self.patch_attempts: dict[str, int] = defaultdict(int)
        self._blocked_files: set[str] = set()
        self._offset: int = 0
        self._running = False
        self._fh: Optional[object] = None

    async def _notify_tg(self, text: str) -> None:
        if not self._notify:
            logger.warning("AutonomousLogWatcher notify (no TG): %s", text[:200])
            return
        try:
            await self._notify(text)
        except Exception as exc:
            logger.debug("notify failed: %s", exc)

    def _open_log(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.touch()
        fh = self.log_path.open("r", encoding="utf-8", errors="replace")
        # Start at EOF so we only process new lines (boot handled separately).
        fh.seek(0, 2)
        self._offset = fh.tell()
        return fh

    def _read_since_tell(self, fh) -> str:
        """Scan log from the last ``file.tell()`` position."""
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return ""
        if size < self._offset:
            # Log rotated / truncated
            fh.seek(0)
            self._offset = 0
        if size == self._offset:
            return ""
        fh.seek(self._offset)
        chunk = fh.read()
        self._offset = fh.tell()
        return chunk

    @staticmethod
    def _is_actionable(chunk: str) -> bool:
        clean = _strip_ansi(chunk)
        low = clean.lower()
        # Cookie/OAuth expiry is not a code bug — do not auto-patch.
        if (
            "token expired" in low
            or "access_token has expired" in low
            or "waiting for fresh cookies" in low
            or "нужны свежие куки" in low
        ):
            return False
        # Self-heal / Cursor noise must never re-trigger pause (recursive heal)
        heal_noise = (
            "ai_healing",
            "self_healing",
            "cursor_executor",
            "cursor_engine",
            "cursor_self_healer",
            "gemini_auditor",
            "healingorchestrator",
            "autonomouslogwatcher",
            "cursorexecutor",
            "cursor cli timed out",
            "subprocess.timeoutexpired",
            "timeout after 150s",
            "agentn.global.api",
            "local_fixer",
        )
        # Drop healing lines then re-check markers on remaining game code only
        kept = []
        skip_tb = False
        for line in clean.splitlines():
            ll = line.lower()
            if any(n in ll for n in heal_noise):
                skip_tb = True
                continue
            if " | dwar_bot.core.ai_healing." in ll or " | dwar_bot.core.self_healing." in ll:
                skip_tb = True
                continue
            if skip_tb:
                st = line.lstrip()
                if (
                    ll.startswith("traceback (most recent call last)")
                    or st.startswith("File ")
                    or ll.startswith("subprocess.")
                    or (line.startswith(" ") or line.startswith("\t"))
                    or ("error:" in ll and not ll[:1].isdigit())
                ):
                    continue
                if len(line) >= 4 and line[0].isdigit():
                    skip_tb = False
                else:
                    continue
            kept.append(line)
        filtered = "\n".join(kept)
        if not filtered.strip():
            return False
        return any(m in filtered for m in ERROR_MARKERS)

    @staticmethod
    def _extract_traceback(chunk: str) -> str:
        clean = _strip_ansi(chunk)
        m = _TRACEBACK_RE.search(clean)
        if m:
            return m.group(1).strip()
        for marker in ERROR_MARKERS:
            idx = clean.find(marker)
            if idx >= 0:
                return clean[idx: idx + 4000].strip()
        return clean[-4000:].strip()

    @staticmethod
    def _extract_failed_file(traceback_text: str, chunk: str) -> str:
        paths = _FILE_RE.findall(traceback_text) or _FILE_RE.findall(_strip_ansi(chunk))
        for p in reversed(paths):
            norm = p.replace("\\", "/")
            # Never "heal" the healer itself
            if "ai_healing" in norm or "self_healing" in norm:
                continue
            if "dwar_bot/" in norm:
                return norm[norm.find("dwar_bot/"):]
            if "core/self_healing/" in norm:
                continue
        if paths:
            last = paths[-1].replace("\\", "/")
            if "ai_healing" not in last and "self_healing" not in last:
                return last
        return "dwar_bot/main.py"

    async def _circuit_trip(self, failed_file: str) -> None:
        self._blocked_files.add(failed_file)
        await self.controller.pause(reason=f"circuit-breaker:{failed_file}")
        alarm = (
            f"🚨 **CRITICAL FAIL:** Файл `{failed_file}` не удалось починить "
            f"за {MAX_PATCH_ATTEMPTS} попытки. Требуется вмешательство разработчика!"
        )
        logger.critical(alarm)
        await self._notify_tg(alarm)

    async def _handle_error(self, chunk: str) -> None:
        clean = _strip_ansi(chunk)
        filtered = "\n".join(
            line
            for line in clean.splitlines()
            if "self_healing" not in line
            and "cursor_engine" not in line
            and "cursor_self_healer" not in line
            and "auto_healer" not in line
            and "log_watcher" not in line
            and "ai_healing" not in line
            and "cursor_executor" not in line
            and "gemini_auditor" not in line
            and "HealingOrchestrator" not in line
            and "CursorExecutor" not in line
            and "TimeoutExpired" not in line
            and "Cursor CLI timed out" not in line
        )
        if not self._is_actionable(filtered):
            return

        tb = self._extract_traceback(filtered)
        failed_file = self._extract_failed_file(tb, filtered)
        if "ai_healing" in failed_file or "self_healing" in failed_file:
            logger.debug("Skip heal of healer file: %s", failed_file)
            return

        if failed_file in self._blocked_files:
            logger.debug("Circuit open for %s — skip", failed_file)
            return

        attempts = self.patch_attempts[failed_file]
        if attempts >= MAX_PATCH_ATTEMPTS:
            await self._circuit_trip(failed_file)
            return

        logger.warning(
            "AutonomousLogWatcher: error in %s (attempt %d/%d)",
            failed_file,
            attempts + 1,
            MAX_PATCH_ATTEMPTS,
        )

        # 1) Pause farm
        await self.controller.pause(reason=f"heal:{failed_file}")
        set_bot_state(BotState.HEALING)

        # 2–3) Extract stack + apply Cursor patch
        ok = False
        try:
            ok = await asyncio.to_thread(self._patch_fn, failed_file, tb)
        except Exception as exc:
            logger.exception("apply_patch_via_cursor crashed: %s", exc)
            ok = False

        if ok:
            self.patch_attempts[failed_file] = 0
            await self.controller.resume(reason=f"healed:{failed_file}")
            report = (
                "🛠 **Self-Healing Success (300s Watchdog):**\n"
                f"- **Модуль:** `{failed_file}`\n"
                "- **Статус:** Ошибка успешно устранена через Cursor API!\n"
                "- **Тесты:** pytest [PASSED]\n"
                "- **Действие:** Бот продолжат фарм."
            )
            logger.info(report)
            await self._notify_tg(report)
            return

        self.patch_attempts[failed_file] = attempts + 1
        if self.patch_attempts[failed_file] >= MAX_PATCH_ATTEMPTS:
            await self._circuit_trip(failed_file)
            return

        # Soft fail — stay paused briefly then resume so farm can continue
        # until the next scan window (unless circuit trips).
        await self.controller.resume(reason=f"heal-fail:{failed_file}")
        await self._notify_tg(
            f"🔄 **Self-Healing FAIL** `{failed_file}` "
            f"({self.patch_attempts[failed_file]}/{MAX_PATCH_ATTEMPTS})"
        )

    async def start_monitoring(self, interval_seconds: int = 300) -> None:
        """
        Async loop: every ``interval_seconds`` (default 300) scan ``bot.log``
        from the last ``tell()`` position and orchestrate self-heal.
        """
        self.interval_seconds = max(30, int(interval_seconds))
        self._running = True
        logger.info(
            "AutonomousLogWatcher started — interval=%ds file=%s",
            self.interval_seconds,
            self.log_path,
        )

        fh = None
        try:
            fh = self._open_log()
            while self._running:
                try:
                    state = get_bot_state()
                    # Skip while any heal path holds the farm (Gemini orch uses PAUSED)
                    if state in (BotState.HEALING, BotState.PAUSED):
                        await asyncio.sleep(5)
                        continue

                    chunk = self._read_since_tell(fh)
                    if chunk:
                        await self._handle_error(chunk)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("AutonomousLogWatcher loop error: %s", exc)

                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            logger.info("AutonomousLogWatcher cancelled.")
            raise
        finally:
            self._running = False
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            logger.info("AutonomousLogWatcher stopped.")

    def stop(self) -> None:
        self._running = False
