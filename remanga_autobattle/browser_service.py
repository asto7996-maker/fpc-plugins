"""
Совместимость со старыми импортами.

Реальная логика Remanga живёт в services/remanga_service.py.
"""

from services.remanga_service import *  # noqa: F403
from services.remanga_service import (  # noqa: F401
    BattleOutcome,
    BattleResult,
    BrowserService,
    main_setup,
)
