"""
Auto-healer orchestrator — classifies errors and routes recovery.

Priority
--------
1. IGNORE / AUTH / NETWORK / RATE_LIMIT → local only (never Cursor)
2. PROTOCOL / STAGNATION → local gameplay recover, Cursor only if that fails
3. CODE_BUG → Cursor CLI patch → pytest → restart
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.cursor_self_healer import heal_ready
from dwar_bot.core.self_healing.cursor_engine import apply_patch_via_cursor
from dwar_bot.core.error_recovery import (
    ErrorClass,
    ClassifiedError,
    classify_exception,
    classify_text,
    cursor_prompt_for,
    get_recovery_stats,
)

logger = logging.getLogger(__name__)

NotifyFn = Callable[[str], Awaitable[None]]

GLOBAL_HEAL_COOLDOWN_SEC = 120
STAGNATION_TICKS = 5
STAGNATION_HEAL_COOLDOWN = 600
MAX_FAILS_PER_FP = 3
FP_BACKOFF_SEC = 1800
NETWORK_BACKOFF_SEC = 20


@dataclass
class HealRequest:
    failed_file: str
    traceback_text: str
    reason: str = "exception"
    force: bool = False
    classified: Optional[ClassifiedError] = None


@dataclass
class AutoHealer:
    """Process-wide healer with classification, local recover, Cursor escalate."""

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
    _network_until: float = 0.0

    async def _notify(self, text: str) -> None:
        if not self.notify_fn:
            return
        try:
            await self.notify_fn(text)
        except Exception as exc:
            logger.debug("heal notify: %s", exc)

    async def ensure_ready(self) -> bool:
        ok, detail = await asyncio.to_thread(heal_ready)
        self._ready_checked = True
        if ok:
            logger.info("AutoHealer ready — %s", detail)
        else:
            logger.error("AutoHealer NOT ready — %s", detail)
            await self._notify(
                f"⚠️ <b>AutoHealer не готов</b>:\n<code>{detail[:400]}</code>"
            )
        return ok

    async def handle_exception(self, exc: BaseException, *, where: str = "tick") -> bool:
        classified = classify_exception(exc, where=where)
        get_recovery_stats().note(classified.kind, where)
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.error(
            "AutoHealer: %s in %s → %s (cursor=%s)",
            classified.kind.name, where, classified.failed_file, classified.allow_cursor,
        )

        if classified.kind == ErrorClass.IGNORE:
            return False

        if classified.kind == ErrorClass.AUTH:
            await self._notify(
                "🔑 <b>Сессия/OAuth</b> — Cursor не чинит это.\n"
                "Пришли Cookie Editor JSON (/cookies)."
            )
            return False

        if classified.kind == ErrorClass.NETWORK:
            self._network_until = time.time() + NETWORK_BACKOFF_SEC
            logger.warning("Network error — backoff %ds", NETWORK_BACKOFF_SEC)
            return False

        if classified.kind == ErrorClass.RATE_LIMIT:
            self._network_until = time.time() + 60
            return False

        if classified.kind in (ErrorClass.PROTOCOL, ErrorClass.STAGNATION):
            if self.on_local_recover:
                try:
                    if await self.on_local_recover(f"{where}:{classified.summary}"):
                        await self._notify(
                            f"🛠 <b>Локальный recover</b> ({classified.kind.name})"
                        )
                        return True
                except Exception as rec_exc:
                    logger.debug("local recover: %s", rec_exc)

        if not classified.allow_cursor:
            return False

        prompt = cursor_prompt_for(classified, f"[{where}] {exc}\n{tb}")
        return await self.heal(HealRequest(
            failed_file=classified.failed_file,
            traceback_text=prompt,
            reason=classified.kind.name.lower(),
            classified=classified,
        ))

    async def note_progress(self, focus_key: str, progressed: bool) -> None:
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

        classified = classify_text(
            "STAGNATION / no gameplay progress",
            focus_key=focus_key,
        )
        get_recovery_stats().note(ErrorClass.STAGNATION, focus_key)

        if self.on_local_recover:
            try:
                fixed = await self.on_local_recover(focus_key)
                if fixed:
                    await self._notify(
                        f"🛠 <b>AutoHealer (локально):</b> "
                        f"<code>{focus_key}</code>"
                    )
                    return
            except Exception as exc:
                logger.debug("local recover: %s", exc)

        # Escalate to Cursor only with game-logic prompt — not for auth
        prompt = cursor_prompt_for(
            classified,
            f"STAGNATION focus={focus_key}\n"
            "Бот крутит действия без XP/лута/смены area. "
            "Нужен hunt_farm + fight WS + quest answer.",
        )
        logger.warning("AutoHealer: stagnation → Cursor (%s)", focus_key)
        await self.heal(HealRequest(
            failed_file=classified.failed_file,
            traceback_text=prompt,
            reason="stagnation",
            force=True,
            classified=classified,
        ))

    async def heal(self, req: HealRequest) -> bool:
        # Refuse auth-shaped prompts even if force
        classified = req.classified or classify_text(req.traceback_text)
        if classified.kind == ErrorClass.AUTH:
            logger.info("AutoHealer: refusing Cursor heal for AUTH")
            return False

        fp = f"{req.reason}:{req.failed_file}:{hash(req.traceback_text[-400:])}"
        now = time.time()

        until = self._fail_until.get(fp, 0.0)
        if until and now < until and not req.force:
            return False
        if not req.force and now - self._last_heal_at < GLOBAL_HEAL_COOLDOWN_SEC:
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
                    apply_patch_via_cursor, req.failed_file, req.traceback_text
                )
            except Exception as exc:
                logger.exception("AutoHealer patch crashed: %s", exc)
                ok = False

            if ok:
                self._fail_counts.pop(fp, None)
                self._fail_until.pop(fp, None)
                get_recovery_stats().cursor_heals += 1
                await self._notify(
                    f"✅ <b>AutoHealer SUCCESS</b>\n"
                    f"<code>{req.failed_file}</code> ({req.reason})\n"
                    f"Тесты OK — перезапуск сервиса."
                )
                logger.info("AutoHealer SUCCESS %s", req.failed_file)
                set_bot_state(BotState.HEALING)
                return True

            set_bot_state(BotState.RUNNING)
            if self.resume_fn:
                try:
                    await self.resume_fn()
                except Exception as exc:
                    logger.debug("resume_fn: %s", exc)

            n = self._fail_counts.get(fp, 0) + 1
            self._fail_counts[fp] = n
            if n >= MAX_FAILS_PER_FP:
                self._fail_until[fp] = time.time() + FP_BACKOFF_SEC
            await self._notify(
                f"🔄 <b>AutoHealer FAIL</b>\n"
                f"<code>{req.failed_file}</code> · попытка {n}\n"
                f"<pre>{req.traceback_text[:600]}</pre>"
            )
            logger.error("AutoHealer FAIL %s", req.failed_file)
            return False

    def network_blocked(self) -> bool:
        return time.time() < self._network_until


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
