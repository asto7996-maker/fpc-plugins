"""Planner: junk NPC filter + world-objective farm cap."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_junk_npc_blacklist_in_brain():
    src = (ROOT / "modules" / "progression_brain.py").read_text(encoding="utf-8")
    assert '_JUNK_NPC_IDS = {"816", "817"}' in src
    assert "_is_junk_npc" in src


def test_leveling_world_objective_early_return():
    src = (ROOT / "modules" / "leveling_engine.py").read_text(encoding="utf-8")
    assert "priority=WORLD_OBJECTIVE" in src
    assert "FLASH-idle" in src or "flash_only" in src
    assert "world_objective_flash_only" in src


def test_flash_only_mutes_hunt_and_battle_tg():
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "battle TG muted (flash_only world objective)" in main
    assert "Hunt skipped — flash_only" in main
    assert "Idle 3–5 мин" in main
    assert "no hunt storm" in main


def test_heal_wounded_flash_cooldown():
    src = (ROOT / "modules" / "quest_tracker.py").read_text(encoding="utf-8")
    assert "FLASH-ONLY" in src
    assert "1800.0" in src
