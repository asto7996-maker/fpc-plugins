"""Planner: junk NPC filter + world-objective farm cap."""

from __future__ import annotations

import time
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
    """Village exits (сопки) stay gated — hunt must win over Сопки bounce."""
    eng = LevelingEngine()
    profile = FullProfile(char=CharStats(level=3, hp=147, hp_max=147))
    options = [
        GameOption(
            ActionType.TRAVEL,
            title="Переход: В Дымные сопки",
            score=200,
            payload={"area_id": "192"},
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
    assert decision.focus_override.action == ActionType.HUNT_MOB
    assert "FLASH-farm-lv3" in decision.directive.reason
    assert "flash_farm_open" in decision.notes


def test_village_exit_blocked_skips_sopki():
    from dwar_bot.modules.bot_settings import BotSettings
    from dwar_bot.modules.progression_brain import ProgressionBrain

    brain = ProgressionBrain(BotSettings())
    brain.mark_village_exit_blocked(7200)
    assert brain.village_exit_blocked()
    assert brain._cooldowns.get("Переход: В Дымные сопки", 0) > 0


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
    assert "_wo_farm_open" in main
    assert "open-farm" in main or "Continuous open farm" in main or "push_farm(180" in main


def test_autohealer_ignores_flash_idle():
    src = (ROOT / "core" / "auto_healer.py").read_text(encoding="utf-8")
    assert "снадоб" in src
    assert "Intentional Flash medicine wait" in src


def test_heal_wounded_flash_cooldown():
    src = (ROOT / "modules" / "quest_tracker.py").read_text(encoding="utf-8")
    assert "FLASH-ONLY" in src or "flash_only" in src
    assert "http_impossible" in src
    assert "HEAL_WOUNDED_SAFE_GUARD_V3" in src
    assert "lock_flash_world_objective" in src
    assert "load_world_objective" in src
    assert "_persist_world_objective" in src


def test_set_world_objective_idempotent_no_spam(tmp_path):
    qt = QuestTracker(client=None)  # type: ignore[arg-type]
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    qt._world_objective_state_path = lambda: state  # type: ignore[method-assign]
    qt.set_world_objective(
        kind="heal_wounded",
        title="first",
        npc_id="409",
        artikul_id="18209",
        ban_key="global:409",
    )
    assert qt.pending_world_objective.get("flash_only") is True
    qt.pending_world_objective["http_impossible"] = True
    qt.pending_world_objective["flash_notified"] = True
    qt.set_world_objective(
        kind="heal_wounded",
        title="Поговорить об излечении ополченцев",
        npc_id="409",
        artikul_id="18209",
        ban_key="0:409:0",
    )
    # Same kind + flash → KEEP, preserve lock
    assert qt.pending_world_objective.get("http_impossible") is True
    assert qt.pending_world_objective.get("flash_only") is True


def test_try_heal_wounded_skips_when_flash_locked():
    import asyncio
    from unittest.mock import MagicMock

    qt = QuestTracker(client=MagicMock())  # type: ignore[arg-type]
    qt.pending_world_objective = {
        "kind": "heal_wounded",
        "flash_only": True,
        "http_impossible": True,
        "artikul_id": "18209",
    }
    qt._persist_world_objective = lambda: None  # type: ignore[method-assign]

    async def _run():
        ok = await qt._try_heal_wounded(dict(qt.pending_world_objective))
        assert ok is False
        qt._client.common_action.assert_not_called()

    asyncio.run(_run())


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


def test_farm_open_unbans_story_npc():
    qt = QuestTracker(client=None)  # type: ignore[arg-type]
    # Without http lock, farm_open lifts giver from exhausted set
    qt.pending_world_objective = {
        "kind": "gather",
        "npc_id": "409",
        "farm_open": True,
    }
    qt._world_objective_keys.add("global:409")
    qt._exhausted_dialogues.add("0:409:0")
    assert "409" not in qt.exhausted_npc_ids()
    n = qt.clear_world_objective_npc_ban("409")
    assert n >= 1
    assert "global:409" not in qt._world_objective_keys


def test_flash_locked_keeps_giver_exhausted():
    qt = QuestTracker(client=None)  # type: ignore[arg-type]
    qt.pending_world_objective = {
        "kind": "heal_wounded",
        "npc_id": "409",
        "flash_only": True,
        "http_impossible": True,
        "farm_open": True,
    }
    qt._world_objective_keys.add("global:409")
    assert "409" in qt.exhausted_npc_ids()


def test_lv3_empty_rasselina_loses_to_hunt():
    """Demoted empty hotspot (score≤120) must not beat hunt under farm_open."""
    eng = LevelingEngine()
    profile = FullProfile(char=CharStats(level=3, hp=100, hp_max=100))
    options = [
        GameOption(
            ActionType.COMBAT_AREA,
            title="Точка: Расселина",
            score=80,
            payload={"action_id": "4696"},
            goal=GoalKind.COMBAT,
        ),
        GameOption(
            ActionType.HUNT_MOB,
            title="Зигред-воин",
            score=180,
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
    )
    assert decision.focus_override is not None
    assert decision.focus_override.action == ActionType.HUNT_MOB
    assert "Автофарм" in decision.directive.title
    assert "heal_wounded" not in decision.directive.title


def test_proxy_exp_no_fake_5sec_eta():
    eng = LevelingEngine()
    eng.progress.level = 3
    eng.progress.exp_pct = 99.5  # legacy ceiling
    eng._no_level_wins = 12
    eng._proxy_exp = 5000.0
    eng._exp_samples = [(time.time() - 600, 1000.0), (time.time(), 5000.0)]
    eng.observe_world(profile=FullProfile(char=CharStats(level=3, hp=100, hp_max=100)))
    assert eng.progress.exp_pct <= 95.0
    assert eng.progress.eta_seconds <= 0.0
    html = eng.progress.telegram_html()
    assert "5 сек" not in html
    assert "н/д" in html or "оценка" in html


def test_rich_level_up_hides_fake_eta(tmp_path):
    from dwar_bot.core.rich_notifications import RichNotifications
    from dwar_bot.core.telemetry_engine import TelemetryEngine

    rich = RichNotifications(TelemetryEngine(tmp_path / "tel.db"))
    text = rich.format_level_up_rich(
        level=3,
        exp_pct=99.5,
        exp_per_hour=9000.0,
        eta_seconds=5.0,
        priority="Автофарм Lv3+",
        directive_state="FARMING",
    )
    assert "5 сек" not in text
    assert "н/д" in text
    assert "оценка" in text


def test_rfcheats_hunt_no_long_sleep():
    src = (ROOT / "modules" / "combat_engine.py").read_text(encoding="utf-8")
    assert "no in-hunt sleep" in src
    assert "asyncio.sleep(min(float(decision.sleep_sec), 120.0))" not in src
    assert "soft reset, skip this hunt tick" in src
