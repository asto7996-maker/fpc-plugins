"""Tests for GameKnowledgeBase + LevelingEngine + MasterController."""

from __future__ import annotations

import asyncio
from pathlib import Path

from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.game_knowledge_base import GameKnowledgeBase
from dwar_bot.core.master_controller import MasterController, StrategicDirective
from dwar_bot.modules.leveling_engine import LevelingEngine, LevelProgress
from dwar_bot.modules.progression_brain import ActionType, GameOption, GoalKind
from dwar_bot.modules.stats_parser import FullProfile
from dwar_bot.core.game_client import CharStats, GameState


def test_kb_mobs_and_efficiency(tmp_path: Path):
    kb = GameKnowledgeBase(tmp_path / "k.db")
    kb.ingest_hunt_bots(
        [
            {"id": "1", "name": "Крэтс", "level": 2, "hp": 40},
            {"id": "2", "name": "Крэтс-Вожак", "level": 3, "hp": 80},
        ],
        area_id="932",
    )
    kb.record_kill(mob_id="1", name="Крэтс", area_id="932", fight_sec=30, exp_gained=80, level=2)
    kb.record_kill(mob_id="1", name="Крэтс", area_id="932", fight_sec=25, exp_gained=80, level=2)
    matrix = kb.efficiency_matrix(char_level=2, area_id="932")
    assert matrix
    best = kb.best_farm_target(char_level=2, area_id="932")
    assert best is not None
    assert best.name == "Крэтс"
    assert best.exp_per_min > 0


def test_kb_quests_and_auction(tmp_path: Path):
    kb = GameKnowledgeBase(tmp_path / "q.db")
    n = kb.ingest_npc_quests(
        {
            "quests": [
                {
                    "id": "100",
                    "title": "Убийство Крэтса-Вожака",
                    "level": 2,
                    "exp": 500,
                    "valor": 2,
                    "required_items": ["Клык крэтса"],
                    "status": "available",
                }
            ]
        },
        npc_id="55",
        area_id="932",
        char_level=2,
    )
    assert n == 1
    quests = kb.list_quests(area_id="932", max_level=2)
    assert quests[0].title.startswith("Убийство")
    kb.sample_auction(item_key="Клык крэтса", title="Клык крэтса", price_gold=1.5)
    prices = kb.quest_item_prices(quests[0])
    assert prices["Клык крэтса"] == 1.5


def test_level_progress_telegram_format():
    p = LevelProgress(
        level=5,
        exp_pct=78.4,
        exp_per_hour=12400,
        eta_seconds=3 * 3600 + 15 * 60,
        priority_title="Квест 'Убийство Крэтса-Вожака'",
    )
    html = p.telegram_html()
    assert "Level-Up Update" in html
    assert "5 (78.4%)" in html
    assert "12400" in html.replace(" ", "") or "+12 400" in html or "+12400" in html
    assert "3 ч. 15 мин." in html
    assert "Крэтса-Вожака" in html


def test_leveling_priority_quest(tmp_path: Path):
    kb = GameKnowledgeBase(tmp_path / "lv.db")
    kb.ingest_npc_quests(
        [{"id": "1", "title": "Срочный квест", "exp": 900, "status": "available", "level": 1}],
        npc_id="9",
        area_id="932",
        char_level=2,
    )
    engine = LevelingEngine(knowledge=kb, controller=MasterController())
    profile = FullProfile(char=CharStats(nick="t", level=2, hp=80, hp_max=100), state=GameState(area_id="932"))
    quest_opt = GameOption(
        ActionType.QUEST_NPC,
        title="Срочный квест",
        score=800,
        goal=GoalKind.QUEST,
    )
    decision = engine.decide(
        profile=profile,
        brain_focus=quest_opt,
        brain_options=[quest_opt],
        area_id="932",
        need_quest_unlock=True,
    )
    assert decision.directive.state == BotState.EXECUTING_QUEST
    assert decision.focus_override is not None


def test_leveling_farm_picks_best_mob(tmp_path: Path):
    kb = GameKnowledgeBase(tmp_path / "farm.db")
    kb.upsert_mob(mob_id="10", name="Крэтс", level=2, area_id="932", exp_reward=100)
    kb.record_kill(mob_id="10", name="Крэтс", area_id="932", fight_sec=20, exp_gained=100, level=2)
    engine = LevelingEngine(knowledge=kb, controller=MasterController())
    profile = FullProfile(char=CharStats(nick="t", level=2, hp=90, hp_max=100), state=GameState())
    hunt = GameOption(ActionType.HUNT_MOB, title="Охота: Крэтс", score=400, goal=GoalKind.COMBAT)
    decision = engine.decide(
        profile=profile,
        brain_focus=hunt,
        brain_options=[hunt],
        area_id="932",
    )
    assert decision.directive.state == BotState.FARMING
    assert decision.directive.mob_name == "Крэтс" or "Крэтс" in decision.directive.title


def test_master_controller_apply_directive():
    set_bot_state(BotState.RUNNING)
    ctrl = MasterController()

    async def _run() -> None:
        await ctrl.apply_directive(StrategicDirective(
            state=BotState.FARMING,
            title="Farm test",
            mob_id="42",
            exp_per_hour=1000,
        ))
        assert get_bot_state() == BotState.FARMING
        assert ctrl.current is not None
        assert ctrl.current.mob_id == "42"
        summary = ctrl.directive_summary()
        assert summary["state"] == "FARMING"

    asyncio.run(_run())
    set_bot_state(BotState.RUNNING)


def test_main_wires_leveling_engine():
    src = (Path(__file__).resolve().parents[1] / "dwar_bot" / "main.py").read_text(encoding="utf-8")
    assert "format_level_up_rich" in src or "build_level_up_update" in src
    assert "LevelingEngine" in src
    assert "get_knowledge_base" in src
    assert "StrategicDirective" in src
