"""Tests for DwarBOT-adapted battle strategy."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.modules.battle_strategy import (
    BOTTOM_ATTACK_ID,
    DEFAULT_HIT_SEQ,
    FightBrain,
    MIDDLE_ATTACK_ID,
    TOP_ATTACK_ID,
    extract_combo_sequences,
    parse_hit_list,
    pick_hit_sequence,
)

ROOT = Path(__file__).resolve().parents[1]


def test_parse_hit_list_dwarbot_default():
    seq = parse_hit_list("forward, down, down, up, forward")
    assert seq == [
        MIDDLE_ATTACK_ID,
        BOTTOM_ATTACK_ID,
        BOTTOM_ATTACK_ID,
        TOP_ATTACK_ID,
        MIDDLE_ATTACK_ID,
    ]
    assert tuple(seq) == DEFAULT_HIT_SEQ


def test_parse_hit_list_numeric_and_chars():
    assert parse_hit_list("2,3,1") == [2, 3, 1]
    assert parse_hit_list(list("23121")) == [2, 3, 1, 2, 1]


def test_extract_combo_prefers_longest():
    conf = {
        "combos": (
            '<combo id="1" description="a" level="1" seq="21" title="short"/>'
            '<combo id="2" description="b" level="1" seq="23121" title="long"/>'
        )
    }
    found = extract_combo_sequences(conf)
    assert len(found) == 2
    seq = pick_hit_sequence(conf, configured="forward")
    assert seq == [2, 3, 1, 2, 1]


def test_brain_finisher_unblocks():
    brain = FightBrain(
        hit_seq=[2, 3, 1],
        block_hp_percent=90.0,
        unblock_hp_percent=95.0,
        unblock_before_finisher=True,
        block_cooldown_s=0.0,
    )
    brain.seed_hp(10, 100)  # low HP → want block
    d1 = brain.decide_turn(now=1.0)
    assert d1.hit_zone == 2
    assert d1.set_block is True
    brain.apply_block_result(True, now=1.0)

    d2 = brain.decide_turn(now=2.0)
    assert d2.hit_zone == 3
    assert not d2.is_finisher

    d3 = brain.decide_turn(now=3.0)
    assert d3.hit_zone == 1
    assert d3.is_finisher
    assert d3.set_block is False  # exit block before суперудар


def test_brain_damage_tracking():
    brain = FightBrain(pers_id=42)
    brain.seed_hp(100, 100)
    brain.note_damage(42, 15)
    brain.note_damage(99, 40)
    assert brain.damage_taken == 15
    assert brain.damage_dealt == 40
    assert brain.hp == 85


def test_fight_client_imports_strategy():
    src = (ROOT / "modules" / "fight_client.py").read_text(encoding="utf-8")
    assert "FIGHT_STRATEGY_DWARBOT_V1" in src
    assert "FightBrain" in src
    assert "FS_SCCL_CHANGE_MODE" in src or "CHANGE_MODE" in src
    assert "FS_PE_DAMAGE" in src


def test_combat_post_battle_refresh():
    src = (ROOT / "modules" / "combat_engine.py").read_text(encoding="utf-8")
    assert "_post_battle_refresh" in src
    assert "post_battle_heal" in src
