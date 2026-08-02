"""
Shared runtime bot state for LogWatcher / Telegram / main loop.
"""

from __future__ import annotations

from enum import Enum, auto
from threading import Lock


class BotState(Enum):
    RUNNING = auto()
    PAUSED = auto()
    HEALING = auto()


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
    return get_bot_state() in (BotState.PAUSED, BotState.HEALING)
