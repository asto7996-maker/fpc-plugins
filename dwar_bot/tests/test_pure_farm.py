"""Tests for PureFarm filler + story/quests gate."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.modules.pure_farm import PureFarmEngine, PureFarmStats, VILLAGE_AREAS

ROOT = Path(__file__).resolve().parents[1]


def test_should_run_respects_auto_quests():
    eng = PureFarmEngine()
    # Quests on → PureFarm does not own the tick
    assert eng.should_run(
        max_farm=True, area_id="932", level=3, auto_quests=True,
    ) is False
    assert eng.should_run(
        max_farm=True, area_id="100", level=1, auto_quests=True,
    ) is False
    # Quests off → hunt filler
    assert eng.should_run(
        max_farm=True, area_id="100", level=1, auto_quests=False,
    ) is True
    assert eng.should_run(
        max_farm=False, area_id="932", level=3, auto_quests=False,
    ) is True
    assert eng.should_run(
        max_farm=False, area_id="192", level=5, auto_quests=False,
    ) is False
    # Force hunt-only even with quests
    assert eng.should_run(
        max_farm=False, area_id="192", level=5, auto_quests=True, force=True,
    ) is True
    # Flash heal stalled → hunt for real wins
    assert eng.should_run(
        max_farm=True, area_id="932", level=3, auto_quests=True, story_stalled=True,
    ) is True


def test_main_wires_story_stall_farm():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "story_stalled" in src
    assert "stalled_until" in src
    assert "Flash heal stalled" in src or "heal_wounded stall" in src


def test_telegram_stats_show_wins():
    s = PureFarmStats(
        wins=10, losses=1, money_at_start=100, money_now=105,
        level_at_start=3, level_now=3,
    )
    html = s.telegram_html()
    assert "Pure Farm" in html
    assert "10" in html


def test_main_wires_story_first():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "PureFarmEngine" in src
    assert "pure_farm.run_tick" in src
    assert "PURE_FARM_ONLY" in src
    assert "Story/quests mode" in src
    assert "auto_quests=bool(farm.auto_quests)" in src


def test_pure_farm_module_exists():
    assert (ROOT / "modules" / "pure_farm.py").is_file()
    assert "932" in VILLAGE_AREAS


def test_village_story_npc_outranks_hunt():
    """Local village NPCs must score above open-farm hunt (~480)."""
    from dwar_bot.modules.progression_brain import ProgressionBrain, ActionType
    from dwar_bot.modules.bot_settings import BotSettings
    from dwar_bot.modules.stats_parser import CharStats, FullProfile
    from dwar_bot.core.game_client import GameState, AreaInfo, AreaItem

    brain = ProgressionBrain(BotSettings())
    profile = FullProfile(
        char=CharStats(nick="t", level=3, hp=100, hp_max=100, mp=0, mp_max=0),
        state=GameState(area_id="932", money=100.0),
        inventory=[],
    )
    area = AreaInfo(
        area_id="932",
        title="Деревня",
        items=[
            AreaItem(
                item_id="1",
                name="Вождь Торгор",
                item_type="npc",
                npc_id="409",
                link_id="0",
                f_id="0",
            ),
        ],
    )
    snap = brain.analyze(profile=profile, area=area, npcs=[], local_npcs=[])
    quests = [o for o in snap.options if o.action == ActionType.QUEST_NPC]
    hunts = [o for o in snap.options if o.action == ActionType.HUNT_MOB]
    assert quests, "expected village story NPC option"
    assert hunts, "expected hunt option"
    assert max(o.score for o in quests) > max(o.score for o in hunts)
