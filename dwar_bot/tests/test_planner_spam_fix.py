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


def test_flash_only_lv3_prefers_travel_or_hunt():
    eng = LevelingEngine()
    profile = FullProfile(char=CharStats(level=3, hp=147, hp_max=147))
    options = [
        GameOption(
            ActionType.TRAVEL,
            title="Дымные сопки",
            score=200,
            payload={"area_id": "100"},
            goal=GoalKind.TRAVEL,
        ),
        GameOption(
            ActionType.HUNT_MOB,
            title="Зигред-воин",
            score=180,
            payload={"name": "Зигред-воин"},
            goal=GoalKind.COMBAT,
        ),
        GameOption(
            ActionType.COMBAT_AREA,
            title="Расселина",
            score=795,
            payload={"action_id": "4696"},
            goal=GoalKind.COMBAT,
        ),
        GameOption(
            ActionType.IDLE,
            title="Ждать",
            score=10,
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
        blocked_npc_ids={"409"},
    )
    assert decision.focus_override is not None
    assert decision.focus_override.action in (
        ActionType.TRAVEL, ActionType.HUNT_MOB, ActionType.COMBAT_AREA,
    )
    assert decision.focus_override.action != ActionType.IDLE
    assert any(
        tag in decision.directive.reason
        for tag in ("FLASH-exit", "FLASH-farm-lv3", "FLASH-loot")
    )
    assert "flash_farm_open" in decision.notes


def test_flash_only_lv3_prefers_gear_over_hunt():
    eng = LevelingEngine()
    profile = FullProfile(char=CharStats(level=3, hp=147, hp_max=147))
    options = [
        GameOption(
            ActionType.EQUIP,
            title="Надеть снаряжение ×2",
            score=820,
            goal=GoalKind.GEAR,
        ),
        GameOption(
            ActionType.HUNT_MOB,
            title="Зигред-воин",
            score=460,
            payload={"name": "Зигред-воин"},
            goal=GoalKind.COMBAT,
        ),
        GameOption(
            ActionType.TRAVEL,
            title="Дымные сопки",
            score=200,
            payload={"area_id": "192"},
            goal=GoalKind.TRAVEL,
        ),
    ]
    decision = eng.decide(
        profile=profile,
        brain_focus=None,
        brain_options=options,
        area_id="932",
        world_objective_kind="heal_wounded",
        world_objective_flash_only=True,
        blocked_npc_ids={"409"},
    )
    assert decision.focus_override is not None
    assert decision.focus_override.action == ActionType.EQUIP
    assert "FLASH-gear" in decision.directive.reason


def test_flash_only_lv3_prefers_story_quest():
    eng = LevelingEngine()
    profile = FullProfile(char=CharStats(level=3, hp=147, hp_max=147))
    options = [
        GameOption(
            ActionType.QUEST_NPC,
            title="NPC: Военачальник",
            score=950,
            payload={"npc_id": "2"},
            goal=GoalKind.QUEST,
        ),
        GameOption(
            ActionType.HUNT_MOB,
            title="Зигред-воин",
            score=460,
            payload={"name": "Зигред-воин"},
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
        blocked_npc_ids={"409"},
    )
    assert decision.focus_override is not None
    assert decision.focus_override.action == ActionType.QUEST_NPC
    assert "FLASH-quest" in decision.directive.reason


def test_infer_hunt_mob_level3_aliases():
    qt = QuestTracker(client=None)  # type: ignore[arg-type]
    assert qt._infer_hunt_mob_name("Повергни Зигред-воина") == "Зигред-воин"
    assert qt._infer_hunt_mob_name("Убей Крэтс-вожака") == "Крэтс-вожак"
    assert qt._infer_hunt_mob_name("Повергни Крэтса") == "Крэтс"
    assert qt._infer_hunt_mob_name("Примите меня в отряд") == ""


def test_main_allows_story_and_loot_under_farm_open():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "Story NPC %s while farm_open" in src
    assert "Лут-точка '%s' при farm_open" in src
    assert "post-hunt equip" in src or "Надето после боя" in src
    assert "Keep brain" in src


def test_flash_only_does_not_prefer_rasselina():
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
    assert "Idle 45–90с" in main
    assert "flash_only medicine wait is intentional" in main or "Flash-only medicine wait" in main
    assert "quiet hunt/idle" in main


def test_autohealer_ignores_flash_idle():
    src = (ROOT / "core" / "auto_healer.py").read_text(encoding="utf-8")
    assert "снадоб" in src
    assert "Intentional Flash medicine wait" in src


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
