"""
Smoke tests used by the Cursor self-healer after an auto-patch.

Keep these fast and free of live network calls.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DWAR = REPO / "dwar_bot"


def test_core_modules_importable():
    from dwar_bot.core import bot_state, cursor_self_healer, log_watcher
    from dwar_bot.core.bot_state import BotState

    assert BotState.PAUSED.name == "PAUSED"
    assert callable(cursor_self_healer.patch_code_with_cursor)
    assert callable(log_watcher.start_log_monitoring)


def test_progression_brain_importable():
    from dwar_bot.modules.progression_brain import ProgressionBrain, ActionType
    from dwar_bot.modules.bot_settings import BotSettings

    brain = ProgressionBrain(BotSettings())
    assert brain.last is not None
    assert ActionType.IDLE.value == "idle"


def test_main_module_syntax():
    src = (DWAR / "main.py").read_text(encoding="utf-8")
    ast.parse(src)


def test_selectors_config_exists():
    sel = REPO / "config" / "selectors.py"
    assert sel.exists(), "config/selectors.py must exist for the self-healer prompt"
    ast.parse(sel.read_text(encoding="utf-8"))


def test_healer_prompt_mentions_selectors():
    from dwar_bot.core.cursor_self_healer import _build_prompt

    prompt = _build_prompt("dwar_bot/main.py", "Traceback: boom")
    assert "config/selectors.py" in prompt
    assert "dwar_bot/main.py" in prompt


@pytest.mark.parametrize(
    "path",
    [
        "dwar_bot/core/cursor_self_healer.py",
        "dwar_bot/core/log_watcher.py",
        "dwar_bot/core/bot_state.py",
        "dwar_bot/modules/combat_engine.py",
        "dwar_bot/modules/quest_tracker.py",
    ],
)
def test_critical_files_parse(path: str):
    target = REPO / path
    assert target.exists()
    ast.parse(target.read_text(encoding="utf-8"))
