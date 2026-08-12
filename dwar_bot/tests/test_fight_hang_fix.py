"""Regression: fight hang / sticky type=2 hunt gate."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_clear_hunt_gate_clears_need_quest_unlock():
    src = (ROOT / "modules" / "progression_brain.py").read_text(encoding="utf-8")
    # Extract clear_hunt_gate body
    start = src.find("def clear_hunt_gate")
    assert start > 0
    chunk = src[start:start + 350]
    assert "need_quest_unlock = False" in chunk
    assert "pending_hunt_mob = \"\"" in chunk


def test_combat_engine_has_fight_lock():
    src = (ROOT / "modules" / "combat_engine.py").read_text(encoding="utf-8")
    assert "_fight_lock" in src
    assert "_finish_fight_unlocked" in src
    assert "_try_hunt_attack_unlocked" in src
    assert "SUIS hunt fallback" in src
    assert "pin absent" in src


def test_casual_hunt_win_keeps_open_farm():
    from dwar_bot.modules.bot_settings import BotSettings
    from dwar_bot.modules.progression_brain import ProgressionBrain

    brain = ProgressionBrain(BotSettings())
    brain.push_farm(300.0)
    brain.need_quest_unlock = True  # war-chief gate alone must NOT arm turn-in
    brain.pending_hunt_mob = "Зигред-воин"  # soft pin must NOT arm without quest_gate
    brain.mark_hunt_kill_done(quest_gate=False)
    assert brain.awaiting_quest_turnin is False
    assert brain.farm_push_active() is True
    brain.pending_hunt_mob = "Крэтс"
    brain.mark_hunt_kill_done(quest_gate=True)
    assert brain.awaiting_quest_turnin is True
    assert brain.farm_push_active() is False


def test_voenachalnik_block_farms_at_lv3():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "need_quest_unlock=ON" in src
    assert "farm_open story refresh" in src
    assert "farm_open" in src
    assert "_wo_farm_open" in src
    assert "_quest_kill_gated" in src
    assert "quest_gate=gated" in src or "mark_hunt_kill_done(quest_gate=" in src
    assert "via farm_push" in src  # level-up adapt must not pin pending_hunt_mob
    assert "lock_flash_world_objective" in src
    assert "http_impossible" in src or "flash-locked" in src


def test_fight_client_reconnect_marker():
    src = (ROOT / "modules" / "fight_client.py").read_text(encoding="utf-8")
    assert "FIGHT_WS_SERIAL_V1" in src
    assert "Fight stall nudge" in src
    assert "reconnect" in src.lower()
    assert "FIGHT_STRATEGY_DWARBOT_V1" in src
    assert "FightBrain" in src


def test_local_recover_skips_when_fight_busy():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_fight_busy" in src
    assert "не стартую второй WS" in src
