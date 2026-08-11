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


def test_zero_reward_detection_and_message():
    s = PureFarmStats(
        wins=10, money_at_start=158.13, money_now=158.13,
        level_at_start=3, level_now=3,
    )
    assert s.is_zero_reward() is True
    html = s.telegram_html()
    assert "остановлена" in html or "0 exp" in html.lower()
    # Real gold progress — not zero-reward
    s2 = PureFarmStats(
        wins=20, money_at_start=100, money_now=120,
        level_at_start=3, level_now=3,
    )
    assert s2.is_zero_reward() is False


def test_pure_farm_idles_on_zero_reward():
    src = (ROOT / "modules" / "pure_farm.py").read_text(encoding="utf-8")
    assert "_zero_reward_idle" in src
    assert "ZERO_REWARD_WINS" in src
    assert "остановлена" in src
    assert "flash_locked_village" in src
    assert "Skip Cretas" in src or "Don't grind" in src or "0-exp" in src
    assert "_try_leave_village_now" in src
    assert "POST_VILLAGE_FARM_AREAS" in src
    assert "_max_side_progress" in src
    assert "SIDE_EVERY_WINS" in src
    assert "Переполох" in src or "сарай" in src or "event barn" in src


def test_heal_turnin_not_flash_locked():
    from dwar_bot.modules.quest_tracker import QuestTracker

    qt = QuestTracker.__new__(QuestTracker)
    assert qt._is_heal_wounded_point("Излечение ополченцев", "дайте снадобье раненым")
    assert not qt._is_heal_wounded_point(
        "Лекарство доставлено раненым!", "спасибо за старание"
    )


def test_main_maxfarm_planner_hybrid():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "post_village_open_farm" in src
    assert "planner_tick" in src
    assert "MaxFarm planner tick" in src
    assert "% 12 ==" in src


def test_pure_farm_skips_flavor_npcs():
    from dwar_bot.modules.pure_farm import FLAVOR_NPC_IDS, FLAVOR_NPC_NAME_KW

    assert "121" in FLAVOR_NPC_IDS and "132" in FLAVOR_NPC_IDS
    assert any("сугор" in kw for kw in FLAVOR_NPC_NAME_KW)


def test_bag_actions_skip_food_and_lockpick():
    from dwar_bot.modules.combat_engine import CombatEngine

    src = (ROOT / "modules" / "combat_engine.py").read_text(encoding="utf-8")
    assert "include_food" in src
    assert "ADD_HP" in CombatEngine._BAG_SKIP_CODES
    assert any("взлом" in k for k in CombatEngine._BAG_SKIP_ACTION_KW)
    assert "_bag_action_blacklist" in src
    assert "_last_equip_from_bag_at" in src


def test_pure_farm_does_not_skip_hunt_on_bag_in_post_village():
    src = (ROOT / "modules" / "pure_farm.py").read_text(encoding="utf-8")
    assert "post_village" in src
    assert "include_food=False" in src
    assert "not post_village and not (farm.aggressive" in src


def test_main_skips_fake_exp_proxy_and_levelup_spam():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "wins) * 50" not in src
    assert "Skip Level-Up TG" in src
    # Cretas still zero Exp; open farm may use leveling engine proxy
    assert "zero_reward" in src
    assert "exp_proxy" in src


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
