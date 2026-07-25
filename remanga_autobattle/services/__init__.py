"""Сервисы автоматизации: Remanga и MangaBuff."""

from services.remanga_service import BattleOutcome, BattleResult, BrowserService
from services.mangabuff_service import MangaBuffService, MangaBuffStats

__all__ = [
    "BattleOutcome",
    "BattleResult",
    "BrowserService",
    "MangaBuffService",
    "MangaBuffStats",
]
