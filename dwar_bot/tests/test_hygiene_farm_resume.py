"""Hygiene must not leave the bot spinning empty hunt ticks with zero progress."""

from __future__ import annotations

import time
from pathlib import Path

from dwar_bot.modules.rfcheats_knowledge import HygieneTracker, RfCheatsDefaults

ROOT = Path(__file__).resolve().parents[1]


def test_daily_budget_does_not_rearm_infinite_45min(tmp_path: Path):
    d = RfCheatsDefaults(max_daily_minutes=1, max_continuous_minutes=999, burst_minutes=999)
    hy = HygieneTracker(defaults=d, state_path=tmp_path / "d.json")
    hy.daily_active_sec = 120
    dec1 = hy.check()
    assert dec1.should_pause
    hy.note_break(dec1.sleep_sec)
    until = hy.break_until
    # Second check while break active — remaining only, no extend
    dec2 = hy.check()
    assert dec2.should_pause
    hy.note_break(dec2.sleep_sec)
    assert hy.break_until == until
    assert "midnight" in dec1.reason


def test_combat_source_has_hygiene_wait_and_auto_clear():
    combat = (ROOT / "modules" / "combat_engine.py").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "hygiene_clear_break" in combat
    assert "cleared orphan break" in combat
    assert "Hunt hygiene break" in main
    assert "sleeping" in main
    # Soft-skip must NOT pretend progress (return False)
    assert "Hunt soft-skip (NO_BATTLE)" in main


def test_defaults_allow_longer_daily_farm():
    from dwar_bot.config import COMBAT
    assert COMBAT.rfcheats_max_daily_minutes >= 720
    assert RfCheatsDefaults().max_daily_minutes >= 720
