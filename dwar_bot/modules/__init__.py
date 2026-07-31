"""Прикладные модули бота."""

from __future__ import annotations

from .combat_engine import CombatEngine, CombatReport, CombatResult
from .quest_tracker import DialogState, QuestTracker
from .stats_parser import CharacterStats, InventoryItem, Resource, StatsParser
from .timers_manager import Cooldown, TimersManager

__all__ = [
    "StatsParser",
    "CharacterStats",
    "Resource",
    "InventoryItem",
    "CombatEngine",
    "CombatReport",
    "CombatResult",
    "QuestTracker",
    "DialogState",
    "TimersManager",
    "Cooldown",
]
