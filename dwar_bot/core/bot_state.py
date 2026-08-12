"""
Shared runtime bot state for LogWatcher / Telegram / LevelingEngine / main loop.
"""

from __future__ import annotations

from enum import Enum, auto
from threading import Lock
from typing import Optional


class BotState(Enum):
    """Process-wide operational mode (heal pause is global; farm modes are strategic)."""

    RUNNING = auto()
    PAUSED = auto()
    HEALING = auto()
    # Strategic Level-Up modes (MasterController / LevelingEngine)
    EXECUTING_QUEST = auto()
    FARMING = auto()
    BUFFING = auto()
    IDLE_TASKS = auto()
    REPUTATION = auto()


_lock = Lock()
_state: BotState = BotState.RUNNING


def get_bot_state() -> BotState:
    with _lock:
        return _state


def set_bot_state(state: BotState) -> None:
    global _state
    with _lock:
        _state = state


def is_paused() -> bool:
    """True when the farm loop must not act (manual pause or self-heal)."""
    return get_bot_state() in (BotState.PAUSED, BotState.HEALING)


def is_strategic(state: Optional[BotState] = None) -> bool:
    """True when the bot is in an active Level-Up strategic mode."""
    s = state if state is not None else get_bot_state()
    return s in (
        BotState.EXECUTING_QUEST,
        BotState.FARMING,
        BotState.BUFFING,
        BotState.IDLE_TASKS,
        BotState.REPUTATION,
        BotState.RUNNING,
    )
