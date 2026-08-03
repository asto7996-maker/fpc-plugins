"""Planner: junk NPC filter + world-objective farm cap."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.core.bot_state import BotState
from dwar_bot.modules.leveling_engine import LevelingEngine
from dwar_bot.modules.progression_brain import ActionType, GameOption, GoalKind
from dwar_bot.modules.quest_tracker import QuestTracker
from dwar_bot.modules.stats_parser import CharStats, FullProfile


ROOT = Path(__file__).resolve().parents[1]


def test_junk_npc_blacklist_in_brain():
    src = (ROOT / "modules" / "progression_brain.py").read_text(encoding="utf-8")
    assert '_JUNK_NPC_IDS = {"816", "817"}' in src
    assert "_is_junk_npc" in src


def test_leveling_world_objective_early_return():
    src = (ROOT / "modules" / "leveling_engine.py").read_text(encoding="utf-8")
    assert "priority=WORLD_OBJECTIVE" in src
    assert "FLASH-idle" in src or "FLASH-hunt" in src
    assert "world_objective_flash_only" in src
    assert "FLASH-hunt" in src


def test_flash_only_prefers_hunt_not_rasselina():
    eng = LevelingEngine()
    profile = FullProfile(char=CharStats(level=2, hp=100, hp_max=100))
    options = [
        GameOption(
            ActionType.COMBAT_AREA,
            title="Расселина",
            score=795,
            detail="hotspot",
            payload={"action_id": "4696"},
            goal=GoalKind.COMBAT,
        ),
        GameOption(
            ActionType.HUNT_MOB,
            title="Крэтс",
            score=180,
            detail="hunt",
            payload={"name": "Крэтс"},
            goal=GoalKind.COMBAT,
        ),
        GameOption(
            ActionType.IDLE,
            title="Ждать",
            score=10,
            detail="idle",
            goal=GoalKind.IDLE,
        ),
    ]
    decision = eng.decide(
        profile=profile,
        brain_focus=None,
        brain_options=options,
        area_id="932",
        world_objective_kind="heal_wounded",
        world_objective_flash_only=True,
    )
    assert decision.focus_override is not None
    assert decision.focus_override.action == ActionType.HUNT_MOB
    assert "FLASH-hunt" in decision.directive.reason
    assert decision.directive.state == BotState.FARMING


def test_flash_only_idle_when_no_hunt():
    eng = LevelingEngine()
    profile = FullProfile(char=CharStats(level=2, hp=100, hp_max=100))
    options = [
        GameOption(
            ActionType.COMBAT_AREA,
            title="Расселина",
            score=795,
            payload={"action_id": "4696"},
            goal=GoalKind.COMBAT,
        ),
    ]
    decision = eng.decide(
        profile=profile,
        brain_focus=None,
        brain_options=options,
        area_id="932",
        world_objective_kind="heal_wounded",
        world_objective_flash_only=True,
    )
    assert decision.focus_override is not None
    assert decision.focus_override.action == ActionType.IDLE
    assert "Расселин" not in (decision.focus_override.title or "")


def test_flash_only_allows_hunt_execute():
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "Hunt while flash_only" in main
    assert "Точка '%s' пропущена — flash_only" in main
    assert "Idle 3–5 мин" in main


def test_heal_wounded_flash_cooldown():
    src = (ROOT / "modules" / "quest_tracker.py").read_text(encoding="utf-8")
    assert "FLASH-ONLY" in src
    assert "1800.0" in src
    assert "load_world_objective" in src
    assert "_persist_world_objective" in src


def test_world_objective_persist_roundtrip(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "dwar_bot.config.STATE_FILE",
        state,
        raising=False,
    )
    # QuestTracker reads STATE_FILE inside method via import
    qt = QuestTracker(client=None)  # type: ignore[arg-type]
    qt._world_objective_state_path = lambda: state  # type: ignore[method-assign]
    qt.set_world_objective(
        kind="heal_wounded",
        title="test",
        npc_id="409",
        artikul_id="18209",
        ban_key="global:409",
    )
    assert qt.pending_world_objective.get("flash_only") is True
    qt2 = QuestTracker(client=None)  # type: ignore[arg-type]
    qt2._world_objective_state_path = lambda: state  # type: ignore[method-assign]
    assert qt2.load_world_objective()
    assert qt2.pending_world_objective.get("kind") == "heal_wounded"
    assert qt2.pending_world_objective.get("flash_only") is True
    assert "409" in qt2.world_objective_npc_ids()
