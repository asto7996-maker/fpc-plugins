"""Tests for SUIS (dwar.browsergamebots.com) knowledge integration."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.modules.battle_strategy import (
    BOTTOM_ATTACK_ID,
    MIDDLE_ATTACK_ID,
    TOP_ATTACK_ID,
    pick_hit_sequence,
)
from dwar_bot.modules.suis_knowledge import (
    SUIS_DEFAULT_PHYSICAL,
    SUIS_EXAMPLE_ADVANCED,
    SUIS_EXAMPLE_SIMPLE,
    SUIS_MOBS,
    apply_suis_defaults_to_combat_dict,
    default_suis_sequence,
    food_choice_for_hp,
    food_ladder_candidates,
    hunt_names_for_level,
    load_catalog,
    parse_suis_sequence,
    resources_for_skill,
    save_catalog,
    suis_sequence_to_hit_list,
)

ROOT = Path(__file__).resolve().parents[1]


def test_parse_simple_formula():
    steps = parse_suis_sequence(SUIS_EXAMPLE_SIMPLE)
    kinds = [s.kind for s in steps]
    assert kinds == ["block_on", "hit", "hit", "block_off", "hit"]
    zones = [s.zone for s in steps if s.kind == "hit"]
    assert zones == [TOP_ATTACK_ID, BOTTOM_ATTACK_ID, TOP_ATTACK_ID]


def test_parse_advanced_with_belt():
    steps = parse_suis_sequence(SUIS_EXAMPLE_ADVANCED)
    assert [s.kind for s in steps] == ["hit", "hit", "slot", "hit"]
    assert steps[2].slot == 2
    hits = suis_sequence_to_hit_list(SUIS_EXAMPLE_ADVANCED)
    assert hits == [TOP_ATTACK_ID, BOTTOM_ATTACK_ID, MIDDLE_ATTACK_ID]


def test_parse_pause_and_spaces():
    steps = parse_suis_sequence("Г П3 Н")
    assert steps[0].kind == "hit"
    assert steps[1].kind == "pause" and steps[1].pause_sec == 3.0
    assert steps[2].zone == BOTTOM_ATTACK_ID


def test_default_sequence_by_level():
    assert default_suis_sequence(2) == SUIS_DEFAULT_PHYSICAL
    assert "Б" not in default_suis_sequence(15)


def test_pick_hit_sequence_suis_beats_botmek():
    seq = pick_hit_sequence(
        None,
        configured=None,
        botmek_fallback=[3, 3, 3],
        suis_fallback=[1, 3, 2],
        source_label="suis:ГНТ",
    )
    assert seq == [1, 3, 2]


def test_pick_hit_sequence_combo_beats_suis():
    conf = {
        "combos": '<combo id="9" description="x" level="1" seq="212" title="srv"/>'
    }
    seq = pick_hit_sequence(
        conf,
        suis_fallback=[1, 1, 1],
        botmek_fallback=[3, 3, 3],
    )
    assert seq == [2, 1, 2]


def test_food_ladder_with_skip():
    assert food_choice_for_hp(80.0, skip_above=75.0) is None
    assert food_choice_for_hp(60.0, skip_above=75.0) == "Груша"
    assert food_choice_for_hp(30.0, skip_above=75.0) == "Фелинойская вобла"
    assert food_choice_for_hp(10.0, skip_above=75.0) == "Огненный лещ"
    # skip disabled → bun for high HP
    assert food_choice_for_hp(80.0, skip_above=0) == "Сдобная булочка"
    cands = food_ladder_candidates(60.0, skip_above=75.0)
    assert cands[0] == "Груша"
    assert "Фелинойская вобла" in cands


def test_hunt_priority_level2():
    names = hunt_names_for_level(2)
    assert "Крэтс" in names or "Бешеный пес" in names or "Зигред" in names
    assert any(m.name == "Крэтс-вожак" for m in SUIS_MOBS)
    assert any(m.hunt_default for m in SUIS_MOBS if m.name == "Крэтс")


def test_hunt_priority_level3_prefers_same_level():
    names = hunt_names_for_level(3)
    assert names[0] in {
        "Зигред-воин", "Крэтс-вожак", "Неистовый пес",
        "Огненная паучиха", "Пепельный паук", "Пес-демон", "Скелет-воин",
    }
    # Exact L3 band (±1) excludes village Крэтс (L1); wider pad still ranks L3 first
    assert "Крэтс" not in names
    wide = hunt_names_for_level(3, pad=2)
    assert wide.index("Зигред-воин") < wide.index("Крэтс")
    from dwar_bot.modules.suis_knowledge import default_hunt_mob
    assert default_hunt_mob(3) == names[0]
    assert default_hunt_mob(1) == "Крэтс"


def test_resources_geologist():
    low = resources_for_skill(0)
    assert "Агат" in low
    assert "Аметист" not in low
    mid = resources_for_skill(60)
    assert "Рубин" in mid
    assert "Алмаз" not in mid


def test_apply_suis_defaults():
    d = apply_suis_defaults_to_combat_dict()
    assert d["hp_elixir_threshold"] == 20.0
    assert d["hp_block_threshold"] == 30.0
    assert d["hp_unblock_threshold"] == 60.0


def test_catalog_roundtrip(tmp_path: Path):
    path = tmp_path / "suis_catalog.json"
    saved = save_catalog(path)
    assert saved.is_file()
    data = load_catalog(path)
    assert data["source"].startswith("https://dwar.browsergamebots.com")
    assert len(data["mobs"]) >= 14
    assert data["combat_defaults"]["session_kill_limit"] == 50
    assert "gather_defaults" in data
    assert "operator_defaults" in data


def test_repo_catalog_exists():
    cat = ROOT / "data" / "suis_catalog.json"
    if not cat.is_file():
        save_catalog(cat)
    data = load_catalog(cat)
    assert len(data.get("mobs") or []) >= 8
