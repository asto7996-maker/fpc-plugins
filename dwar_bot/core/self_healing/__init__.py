"""
Industrial self-healing package for «Легенда: Наследие Драконов».

Modules
-------
- ``ast_checker`` — pre-apply Python AST + safety validation
- ``cursor_engine`` — Cursor CLI patch + backup rotation + pytest gate
- ``watcher`` — 300s log orchestrator with circuit breaker
"""

from __future__ import annotations

from dwar_bot.core.self_healing.ast_checker import validate_python_code
from dwar_bot.core.self_healing.cursor_engine import apply_patch_via_cursor
from dwar_bot.core.self_healing.watcher import AutonomousLogWatcher, MasterController

__all__ = [
    "validate_python_code",
    "apply_patch_via_cursor",
    "AutonomousLogWatcher",
    "MasterController",
]
