"""
Healing orchestrator — 120s Gemini audit loop + Cursor patch queue.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from dwar_bot.config import LOG_FILE
from dwar_bot.core.ai_healing.cursor_executor import CursorExecutor
from dwar_bot.core.ai_healing.gemini_auditor import GeminiAuditor
from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
NotifyFn = Callable[[str], Awaitable[None]]
PauseFn = Callable[[], Awaitable[None]]
ResumeFn = Callable[[], Awaitable[None]]


def _default_telemetry_paths() -> list[Path]:
    roots = [
        REPO_ROOT / "data",
        Path("/root/data"),
        Path("/root/dwar_bot/data"),
        REPO_ROOT / "dwar_bot" / "data",
    ]
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        out.append(root / "telemetry.db")
        out.extend(sorted(root.glob("telemetry_*.db")))
    # unique preserve order
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def read_log_slice(log_path: Path, *, max_bytes: int = 48_000) -> str:
    """Read the tail of bot.log for the auditor."""
    path = Path(log_path)
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        # Prefer last ~120s worth of lines by keeping last N lines
        lines = text.splitlines()
        return "\n".join(lines[-250:])
    except OSError as exc:
        logger.warning("read_log_slice: %s", exc)
        return ""


def read_telemetry_summary(
    *,
    window_sec: float = 120.0,
    db_paths: Optional[list[Path]] = None,
) -> dict[str, Any]:
    """
    Aggregate Exp/Gold deltas from telemetry SQLite over ``window_sec``.

    Reads ``economy_snapshots`` when present; otherwise returns empty deltas.
    """
    since = time.time() - max(30.0, float(window_sec))
    summary: dict[str, Any] = {
        "window_sec": window_sec,
        "exp_delta": 0.0,
        "gold_delta": 0.0,
        "battles": 0,
        "wins": 0,
        "potions_used": 0,
        "quests_completed": 0,
        "sources": [],
        "progress": "unknown",
    }
    paths = db_paths or _default_telemetry_paths()
    for db in paths:
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(str(db), timeout=2.0)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT ts, gold, exp_proxy, battles, wins, potions_used,
                           quests_completed
                    FROM economy_snapshots
                    WHERE ts >= ?
                    ORDER BY ts ASC
                    """,
                    (since,),
                ).fetchall()
            except sqlite3.Error:
                rows = []
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.debug("telemetry read %s: %s", db, exc)
            continue
        if not rows:
            continue
        summary["sources"].append(str(db))
        g0 = float(rows[0]["gold"] or 0)
        g1 = float(rows[-1]["gold"] or 0)
        e0 = float(rows[0]["exp_proxy"] or 0)
        e1 = float(rows[-1]["exp_proxy"] or 0)
        summary["gold_delta"] += g1 - g0
        summary["exp_delta"] += e1 - e0
        summary["battles"] += int(rows[-1]["battles"] or 0) - int(rows[0]["battles"] or 0)
        summary["wins"] += int(rows[-1]["wins"] or 0) - int(rows[0]["wins"] or 0)
        summary["potions_used"] += int(rows[-1]["potions_used"] or 0) - int(
            rows[0]["potions_used"] or 0
        )
        summary["quests_completed"] += int(rows[-1]["quests_completed"] or 0) - int(
            rows[0]["quests_completed"] or 0
        )

    if summary["exp_delta"] > 0 or summary["gold_delta"] > 0 or summary["wins"] > 0:
        summary["progress"] = "ok"
    elif summary["sources"]:
        summary["progress"] = "stuck"
    else:
        summary["progress"] = "no_data"
    return summary


class HealingOrchestrator:
    """
    Background 120s loop:
    Gemini audit → pause → Cursor patch → pytest gate → resume / alarm.
    """

    def __init__(
        self,
        *,
        log_path: Optional[Path] = None,
        interval_seconds: int = 120,
        auditor: Optional[GeminiAuditor] = None,
        executor: Optional[CursorExecutor] = None,
        notify_fn: Optional[NotifyFn] = None,
        pause_fn: Optional[PauseFn] = None,
        resume_fn: Optional[ResumeFn] = None,
        telemetry_db_paths: Optional[list[Path]] = None,
        max_queue: int = 8,
        failure_cooldown_sec: float = 600.0,
    ) -> None:
        self.log_path = Path(log_path) if log_path else LOG_FILE
        self.interval_seconds = max(30, int(interval_seconds or 120))
        self.auditor = auditor or GeminiAuditor()
        self.executor = executor or CursorExecutor()
        self.notify_fn = notify_fn
        self.pause_fn = pause_fn
        self.resume_fn = resume_fn
        self.telemetry_db_paths = telemetry_db_paths
        self.queue: deque[dict[str, Any]] = deque(maxlen=max_queue)
        self._busy = False
        self._stop = asyncio.Event()
        self._last_failure_at = 0.0
        self.failure_cooldown_sec = failure_cooldown_sec
        self.stats = {
            "audits": 0,
            "issues": 0,
            "patches_ok": 0,
            "patches_fail": 0,
        }
        self._started_at = time.time()
        self._warmup_sec = 25.0  # short warm-up; still skip boot false-positives
        self._resume_on_failure = True  # never leave farm paused forever


    async def _notify(self, text: str) -> None:
        if not self.notify_fn:
            logger.warning("HealingOrchestrator notify (no TG): %s", text[:240])
            return
        try:
            await self.notify_fn(text)
        except Exception as exc:
            logger.warning("HealingOrchestrator notify failed: %s", exc)

    async def _pause_bot(self) -> None:
        set_bot_state(BotState.PAUSED)
        if self.pause_fn:
            try:
                await self.pause_fn()
            except Exception as exc:
                logger.warning("pause_fn failed: %s", exc)
        # Contract: orchestrator leaves the bot in PAUSED during repair
        if get_bot_state() != BotState.PAUSED:
            set_bot_state(BotState.PAUSED)

    async def _resume_bot(self) -> None:
        if self.resume_fn:
            try:
                await self.resume_fn()
            except Exception as exc:
                logger.warning("resume_fn failed: %s", exc)
        set_bot_state(BotState.RUNNING)

    async def audit_once(self) -> Optional[dict[str, Any]]:
        """Collect signals and run GeminiAuditor once."""
        log_slice = await asyncio.to_thread(read_log_slice, self.log_path)
        telemetry = await asyncio.to_thread(
            read_telemetry_summary,
            window_sec=float(self.interval_seconds),
            db_paths=self.telemetry_db_paths,
        )
        state = get_bot_state().name
        verdict = await asyncio.to_thread(
            self.auditor.audit_bot_health,
            log_slice,
            telemetry,
            state,
        )
        self.stats["audits"] += 1
        if not verdict:
            return None
        verdict = dict(verdict)
        verdict["_log_slice"] = log_slice
        verdict["_telemetry"] = telemetry
        return verdict

    async def handle_issue(self, verdict: dict[str, Any]) -> bool:
        """Pause → local fix / Cursor patch → resume (always try to unpause)."""
        if self._busy:
            self.queue.append(verdict)
            logger.info("HealingOrchestrator: queued issue (%d)", len(self.queue))
            return False
        if time.time() - self._last_failure_at < self.failure_cooldown_sec:
            logger.info(
                "HealingOrchestrator: failure cooldown active — skip patch this tick."
            )
            return False

        self._busy = True
        self.stats["issues"] += 1
        issue_type = str(verdict.get("issue_type") or "UNKNOWN")
        target = str(verdict.get("target_file") or "dwar_bot/main.py")
        prompt = str(verdict.get("cursor_prompt") or "")
        raw_error = str(verdict.get("_log_slice") or "")[-4000:]

        try:
            await self._pause_bot()
            # Guard: recent fight wins in the same window → do not burn Cursor on "CRASH"
            raw_low = raw_error.lower()
            if issue_type in ("CRASH", "STUCK_NO_PROGRESS") and (
                "бой выигран" in raw_low
                or "telemetry battle win" in raw_low
                or "fight: fightover" in raw_low
            ):
                logger.info(
                    "HealingOrchestrator: skip %s — live fight wins in window.",
                    issue_type,
                )
                await self._resume_bot()
                return False

            await self._notify(
                "⚠️ <b>Gemini Audit Alert:</b> Обнаружена проблема "
                f"(<code>{issue_type}</code>) в файле <code>{target}</code>. "
                "Передаю ТЗ в Cursor AI…"
            )

            # Level-1.5: deterministic local fixer (instant, no CLI hang)
            local_path = None
            try:
                from dwar_bot.core.ai_healing.local_fixer import try_local_fix

                local_path = await asyncio.to_thread(
                    try_local_fix, verdict, raw_error,
                )
            except Exception as exc:
                logger.debug("local_fixer: %s", exc)

            if local_path:
                self.stats["patches_ok"] += 1
                await self._resume_bot()
                await self._notify(
                    "✅ <b>Local Patch Applied:</b> "
                    f"<code>{local_path}</code> (без Cursor). Бот возобновил работу."
                )
                return True

            ok = await asyncio.to_thread(
                self.executor.execute_patch,
                target,
                prompt,
                raw_error,
            )
            if ok:
                self.stats["patches_ok"] += 1
                await self._resume_bot()
                await self._notify(
                    "✅ <b>Cursor Patch Applied:</b> Файл "
                    f"<code>{target}</code> успешно исправлен и протестирован! "
                    "Бот возобновил работу."
                )
                return True

            self.stats["patches_fail"] += 1
            self._last_failure_at = time.time()
            err = getattr(self.executor, "last_error", "") or "unknown"
            # CRITICAL: do not leave the bot PAUSED forever — farm must continue
            if self._resume_on_failure:
                await self._resume_bot()
                await self._notify(
                    "🚨 <b>Cursor Patch FAILED</b> — бот <b>снят с паузы</b>, "
                    "фарм продолжается.\n"
                    f"Файл: <code>{target}</code>\n"
                    f"Тип: <code>{issue_type}</code>\n"
                    f"Ошибка: <code>{err[:400]}</code>\n"
                    "Повтор через cooldown."
                )
            else:
                set_bot_state(BotState.PAUSED)
                await self._notify(
                    "🚨 <b>Cursor Patch FAILED</b>\n"
                    f"Файл: <code>{target}</code>\n"
                    f"Тип: <code>{issue_type}</code>\n"
                    f"Бот остаётся на <b>PAUSED</b>.\n"
                    f"Ошибка: <code>{err[:500]}</code>"
                )
            return False
        finally:
            self._busy = False
            # Safety net: if we somehow stayed paused after handle_issue
            if (
                self._resume_on_failure
                and get_bot_state() == BotState.PAUSED
                and not self._busy
            ):
                try:
                    await self._resume_bot()
                except Exception:
                    set_bot_state(BotState.RUNNING)

    async def run_forever(self) -> None:
        logger.info(
            "HealingOrchestrator started — interval=%ds log=%s",
            self.interval_seconds,
            self.log_path,
        )
        try:
            while not self._stop.is_set():
                try:
                    # Drain one queued job first
                    if self.queue and not self._busy:
                        queued = self.queue.popleft()
                        await self.handle_issue(queued)
                    else:
                        if get_bot_state() in (BotState.PAUSED, BotState.HEALING) and self._busy:
                            pass
                        elif get_bot_state() == BotState.PAUSED and not self._busy:
                            # Manual / failed pause — do not auto-heal-spam
                            logger.debug(
                                "HealingOrchestrator: bot PAUSED — audit only, no auto-resume."
                            )
                            verdict = await self.audit_once()
                            if verdict and verdict.get("issue_detected"):
                                logger.info(
                                    "Still unhealthy while paused: %s",
                                    verdict.get("issue_type"),
                                )
                        else:
                            # Warm-up: collect audits but do not pause/patch yet
                            if time.time() - self._started_at < self._warmup_sec:
                                verdict = await self.audit_once()
                                if verdict and verdict.get("issue_detected"):
                                    logger.info(
                                        "HealingOrchestrator warm-up skip: %s (%s)",
                                        verdict.get("issue_type"),
                                        verdict.get("target_file"),
                                    )
                            else:
                                verdict = await self.audit_once()
                                if verdict and verdict.get("issue_detected"):
                                    logger.warning(
                                        "Gemini issue: %s → %s",
                                        verdict.get("issue_type"),
                                        verdict.get("target_file"),
                                    )
                                    await self.handle_issue(verdict)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("HealingOrchestrator tick error: %s", exc)

                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("HealingOrchestrator cancelled.")
            raise
        finally:
            logger.info("HealingOrchestrator stopped. stats=%s", self.stats)

    def stop(self) -> None:
        self._stop.set()


async def start_healing_orchestrator(
    interval_seconds: int = 120,
    *,
    log_path: Optional[Path] = None,
    notify_fn: Optional[NotifyFn] = None,
    pause_fn: Optional[PauseFn] = None,
    resume_fn: Optional[ResumeFn] = None,
    auditor: Optional[GeminiAuditor] = None,
    executor: Optional[CursorExecutor] = None,
) -> HealingOrchestrator:
    """
    Convenience starter used from ``main.py``:

        asyncio.create_task(start_healing_orchestrator(120))

    Note: this coroutine *is* the long-running loop (returns only on cancel).
    Prefer constructing ``HealingOrchestrator`` + ``create_task(orch.run_forever())``
    when you need a handle; this helper still matches the requested signature
    by running until cancelled and returning the orchestrator instance after stop.
    """
    orch = HealingOrchestrator(
        log_path=log_path,
        interval_seconds=interval_seconds,
        auditor=auditor,
        executor=executor,
        notify_fn=notify_fn,
        pause_fn=pause_fn,
        resume_fn=resume_fn,
    )
    await orch.run_forever()
    return orch
