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


def test_fight_client_reconnect_marker():
    src = (ROOT / "modules" / "fight_client.py").read_text(encoding="utf-8")
    assert "FIGHT_WS_SERIAL_V1" in src
    assert "Fight stall nudge" in src
    assert "reconnect" in src.lower()


def test_local_recover_skips_when_fight_busy():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_fight_busy" in src
    assert "не стартую второй WS" in src
