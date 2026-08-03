"""
MasterController — strategic façade for Level-Up modes + heal pause/resume.

``LevelingEngine`` pushes directives here (quest / farm / reputation / buffs /
idle multitasking). Self-healing uses ``pause`` / ``resume`` / ``enter_healing``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state

logger = logging.getLogger(__name__)

NotifyFn = Callable[[str], Awaitable[None]]
PauseFn = Callable[[], Awaitable[None]]
ResumeFn = Callable[[], Awaitable[None]]


@dataclass
class StrategicDirective:
    """Target instruction from LevelingEngine → MasterController."""

    state: BotState
    priority: int = 0
    title: str = ""
    reason: str = ""
    mob_id: str = ""
    mob_name: str = ""
    area_id: str = ""
    npc_id: str = ""
    quest_title: str = ""
    exp_per_hour: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)
    issued_at: float = field(default_factory=time.time)


class MasterController:
    """
    Coordinates bot operational + strategic state.

    Example (LevelingEngine → MasterController)::

        directive = StrategicDirective(
            state=BotState.FARMING,
            title="Фарм Крэтсов",
            mob_id="12345",
            mob_name="Крэтс",
            area_id="932",
            exp_per_hour=12400,
            reason="max Exp/Min among known area mobs",
        )
        await controller.apply_directive(directive)
    """

    def __init__(
        self,
        *,
        pause_fn: Optional[PauseFn] = None,
        resume_fn: Optional[ResumeFn] = None,
        notify_fn: Optional[NotifyFn] = None,
    ) -> None:
        self._pause_fn = pause_fn
        self._resume_fn = resume_fn
        self._notify_fn = notify_fn
        self.current: Optional[StrategicDirective] = None
        self._history: list[StrategicDirective] = []

    # ------------------------------------------------------------------
    # Heal / pause API (compat with AutonomousLogWatcher)
    # ------------------------------------------------------------------

    @property
    def state(self) -> BotState:
        return get_bot_state()

    def bind(
        self,
        *,
        pause_fn: Optional[PauseFn] = None,
        resume_fn: Optional[ResumeFn] = None,
        notify_fn: Optional[NotifyFn] = None,
    ) -> "MasterController":
        if pause_fn is not None:
            self._pause_fn = pause_fn
        if resume_fn is not None:
            self._resume_fn = resume_fn
        if notify_fn is not None:
            self._notify_fn = notify_fn
        return self

    async def pause(self, reason: str = "self-heal") -> None:
        set_bot_state(BotState.PAUSED)
        if self._pause_fn:
            try:
                await self._pause_fn()
            except Exception as exc:
                logger.warning("MasterController.pause callback failed: %s", exc)
        logger.warning("MasterController → PAUSED (%s)", reason)

    async def resume(self, reason: str = "self-heal-ok") -> None:
        # Restore last strategic mode if any, else RUNNING
        target = BotState.RUNNING
        if self.current and self.current.state not in (BotState.PAUSED, BotState.HEALING):
            target = self.current.state
        set_bot_state(target)
        if self._resume_fn:
            try:
                await self._resume_fn()
            except Exception as exc:
                logger.warning("MasterController.resume callback failed: %s", exc)
        logger.info("MasterController → %s (%s)", target.name, reason)

    async def enter_healing(self) -> None:
        set_bot_state(BotState.HEALING)
        if self._pause_fn:
            try:
                await self._pause_fn()
                set_bot_state(BotState.HEALING)
            except Exception as exc:
                logger.warning("MasterController.enter_healing pause failed: %s", exc)

    # ------------------------------------------------------------------
    # Strategic Level-Up API
    # ------------------------------------------------------------------

    async def apply_directive(self, directive: StrategicDirective) -> None:
        """
        Accept a LevelingEngine target and switch BotState accordingly.

        Does not pause the farm loop — only HEALING / PAUSED do that.
        """
        if get_bot_state() in (BotState.PAUSED, BotState.HEALING):
            # Remember intent; heal watcher owns the pause
            self.current = directive
            logger.info(
                "MasterController: directive queued while %s — %s",
                get_bot_state().name,
                directive.title or directive.state.name,
            )
            return

        prev = self.current
        self.current = directive
        self._history.append(directive)
        if len(self._history) > 50:
            self._history = self._history[-50:]

        set_bot_state(directive.state)
        changed = (
            prev is None
            or prev.state != directive.state
            or prev.mob_id != directive.mob_id
            or prev.quest_title != directive.quest_title
            or prev.title != directive.title
        )
        logger.info(
            "MasterController → %s | %s | mob=%s area=%s exp/h=%.0f | %s",
            directive.state.name,
            directive.title or "—",
            directive.mob_id or directive.mob_name or "—",
            directive.area_id or "—",
            directive.exp_per_hour,
            directive.reason or "",
        )
        if changed:
            logger.debug("directive payload: %s", directive.payload)

    def directive_summary(self) -> dict[str, Any]:
        d = self.current
        if not d:
            return {"state": get_bot_state().name}
        return {
            "state": d.state.name,
            "title": d.title,
            "reason": d.reason,
            "mob_id": d.mob_id,
            "mob_name": d.mob_name,
            "area_id": d.area_id,
            "npc_id": d.npc_id,
            "quest_title": d.quest_title,
            "exp_per_hour": d.exp_per_hour,
            "priority": d.priority,
        }


# Process-wide default (heal watcher + primary account share one controller).
_CONTROLLER: Optional[MasterController] = None


def get_master_controller() -> MasterController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = MasterController()
    return _CONTROLLER


def bind_master_controller(**kwargs: Any) -> MasterController:
    ctrl = get_master_controller()
    ctrl.bind(**kwargs)
    return ctrl
