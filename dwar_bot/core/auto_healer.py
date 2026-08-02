"""
Auto-healer orchestrator — fully automatic error fixing.

Triggers
--------
* Uncaught exceptions in the game loop
* LogWatcher ERROR / Traceback markers
* Gameplay stagnation (no progress across focus switches)
* Periodic heal-readiness watchdog

Flow
----
pause quietly → Cursor CLI patch → pytest → systemctl restart → resume
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.cursor_self_healer import heal_ready, patch_code_with_cursor

logger = logging.getLogger(__name__)

NotifyFn = Callable[[str], Awaitable[None]]

GLOBAL_HEAL_COOLDOWN_SEC = 90
STAGNATION_TICKS = 4
STAGNATION_HEAL_COOLDOWN = 480
# Max Cursor heal attempts per fingerprint before backing off longer
MAX_FAILS_PER_FP = 3
FP_BACKOFF_SEC = 1800


@dataclass
class HealRequest:
    failed_file: str
    traceback_text: str
    reason: str = "exception"
    force: bool = False


@dataclass
class AutoHealer:
    """Process-wide healer with cooldowns and local recovery hooks."""

    notify_fn: Optional[NotifyFn] = None
    pause_fn: Optional[Callable[[], Awaitable[None]]] = None
    resume_fn: Optional[Callable[[], Awaitable[None]]] = None
    on_local_recover: Optional[Callable[[str], Awaitable[bool]]] = None

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_heal_at: float = 0.0
    _last_fp: str = ""
    _last_stagnation_heal: float = 0.0
    _stagnation_count: int = 0
    _last_focus_key: str = ""
    _fail_counts: dict[str, int] = field(default_factory=dict)
    _fail_until: dict[str, float] = field(default_factory=dict)
    _ready_checked: bool = False

    async def _notify(self, text: str) -> None:
        if not self.notify_fn:
            return
        try:
            await self.notify_fn(text)
        except Exception as exc:
            logger.debug("heal notify: %s", exc)

    async def ensure_ready(self) -> bool:
        """Boot / watchdog: verify CLI + API key once (and retry later)."""
        ok, detail = await asyncio.to_thread(heal_ready)
        self._ready_checked = True
        if ok:
            logger.info("AutoHealer ready — %s", detail)
        else:
            logger.error("AutoHealer NOT ready — %s", detail)
            await self._notify(
                f"⚠️ <b>AutoHealer не готов</b> (автофикс недоступен):\n"
                f"<code>{detail[:400]}</code>"
            )
        return ok

    async def handle_exception(self, exc: BaseException, *, where: str = "tick") -> bool:
        """Called directly from main loop on uncaught errors."""
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        failed = _guess_file_from_tb(tb) or "dwar_bot/main.py"
        logger.error("AutoHealer: exception in %s → %s", where, failed)
        return await self.heal(HealRequest(
            failed_file=failed,
            traceback_text=f"[{where}] {exc}\n{tb}",
            reason="exception",
        ))

    async def note_progress(self, focus_key: str, progressed: bool) -> None:
        """Track stagnation across focus switches (A↔B empty loops count too)."""
        self._last_focus_key = focus_key
        if progressed:
            self._stagnation_count = 0
            return
        self._stagnation_count += 1
        if self._stagnation_count < STAGNATION_TICKS:
            return
        now = time.time()
        if now - self._last_stagnation_heal < STAGNATION_HEAL_COOLDOWN:
            return
        self._last_stagnation_heal = now
        self._stagnation_count = 0

        # Prefer local recovery: farm push / travel / fronts (must be real progress)
        if self.on_local_recover:
            try:
                fixed = await self.on_local_recover(focus_key)
                if fixed:
                    await self._notify(
                        f"🛠 <b>AutoHealer (локально):</b> ухожу в фарм "
                        f"(было <code>{focus_key}</code>)."
                    )
                    return
            except Exception as exc:
                logger.debug("local recover: %s", exc)

        tb = (
            "STAGNATION / DOM-Desync: бот зациклился без прогресса.\n"
            f"focus={focus_key}\n"
            "Симптомы: пустой лут с точки локации, квест type=2 не принимается "
            "(Лиха беда начало!), NPC помечены exhausted, inventory пуст.\n"
            "Нужно: починить протокол npc|answer для type=2 message-переходов, "
            "уважать cooldown dtime/ltime/hidden у area actions, не спамить "
            "Расселину на кулдауне, сбрасывать exhausted NPC по таймеру, "
            "пробовать action_run.php / альтернативные параметры боя.\n"
            "Проверь config/selectors.py и актуальный HTTP API в "
            "dwar_bot/core/game_client.py, dwar_bot/modules/quest_tracker.py, "
            "dwar_bot/modules/progression_brain.py, dwar_bot/main.py."
        )
        logger.warning("AutoHealer: stagnation → Cursor heal (%s)", focus_key)
        await self.heal(HealRequest(
            failed_file=_file_for_focus(focus_key),
            traceback_text=tb,
            reason="stagnation",
            force=True,
        ))

    async def heal(self, req: HealRequest) -> bool:
        fp = f"{req.reason}:{req.failed_file}:{hash(req.traceback_text[-400:])}"
        now = time.time()

        # Per-fingerprint backoff after repeated failures
        until = self._fail_until.get(fp, 0.0)
        if until and now < until and not req.force:
            logger.debug("AutoHealer: fingerprint backoff until %.0f", until)
            return False

        if not req.force and fp == self._last_fp and now - self._last_heal_at < GLOBAL_HEAL_COOLDOWN_SEC:
            logger.debug("AutoHealer: cooldown skip %s", fp)
            return False
        if not req.force and now - self._last_heal_at < GLOBAL_HEAL_COOLDOWN_SEC:
            logger.debug("AutoHealer: global cooldown")
            return False

        async with self._lock:
            if get_bot_state() == BotState.HEALING:
                return False
            self._last_heal_at = time.time()
            self._last_fp = fp

            set_bot_state(BotState.HEALING)
            if self.pause_fn:
                try:
                    await self.pause_fn()
                    # Re-assert HEALING — pause_fn must not leave us as PAUSED
                    set_bot_state(BotState.HEALING)
                except Exception as exc:
                    logger.warning("pause_fn during heal failed: %s", exc)

            logger.warning(
                "AutoHealer START reason=%s file=%s",
                req.reason, req.failed_file,
            )
            await self._notify(
                f"🔧 <b>AutoHealer START</b>\n"
                f"<code>{req.failed_file}</code> · {req.reason}\n"
                f"Пауза → Cursor → pytest → restart…"
            )

            try:
                ok = await asyncio.to_thread(
                    patch_code_with_cursor, req.failed_file, req.traceback_text
                )
            except Exception as exc:
                logger.exception("AutoHealer patch crashed: %s", exc)
                ok = False

            # Only resume if process still alive (restart may kill us)
            set_bot_state(BotState.RUNNING)
            if self.resume_fn:
                try:
                    await self.resume_fn()
                except Exception as exc:
                    logger.debug("resume_fn: %s", exc)

            if ok:
                self._fail_counts.pop(fp, None)
                self._fail_until.pop(fp, None)
                await self._notify(
                    f"✅ <b>AutoHealer SUCCESS</b>\n"
                    f"<code>{req.failed_file}</code> ({req.reason})\n"
                    f"Тесты OK — перезапуск сервиса для загрузки кода."
                )
                logger.info("AutoHealer SUCCESS %s", req.failed_file)
            else:
                n = self._fail_counts.get(fp, 0) + 1
                self._fail_counts[fp] = n
                if n >= MAX_FAILS_PER_FP:
                    self._fail_until[fp] = time.time() + FP_BACKOFF_SEC
                    logger.error(
                        "AutoHealer: fingerprint %s failed %d× — backoff %ds",
                        fp[:80], n, FP_BACKOFF_SEC,
                    )
                await self._notify(
                    f"🔄 <b>AutoHealer FAIL</b>\n"
                    f"<code>{req.failed_file}</code> ({req.reason}) · попытка {n}\n"
                    f"Продолжаю работу, повторю позже.\n"
                    f"<pre>{req.traceback_text[:800]}</pre>"
                )
                logger.error("AutoHealer FAIL %s", req.failed_file)
            return ok


def _guess_file_from_tb(tb: str) -> str:
    paths = re.findall(r'File "([^"]+\.py)"', tb)
    for p in reversed(paths):
        norm = p.replace("\\", "/")
        if "dwar_bot/" in norm:
            return norm[norm.find("dwar_bot/"):]
    return paths[-1] if paths else "dwar_bot/main.py"


def _file_for_focus(focus_key: str) -> str:
    low = focus_key.lower()
    if "quest" in low or "npc" in low or "вожд" in low:
        return "dwar_bot/modules/quest_tracker.py"
    if "combat" in low or "расселин" in low or "area" in low or "точк" in low:
        return "dwar_bot/modules/progression_brain.py"
    return "dwar_bot/main.py"


_HEALER: Optional[AutoHealer] = None


def get_auto_healer() -> AutoHealer:
    global _HEALER
    if _HEALER is None:
        _HEALER = AutoHealer()
    return _HEALER


def bind_auto_healer(**kwargs: Any) -> AutoHealer:
    global _HEALER
    h = get_auto_healer()
    for k, v in kwargs.items():
        if hasattr(h, k) and v is not None:
            setattr(h, k, v)
    _HEALER = h
    return h
