"""Tests for BotMek.ru fight preset integration."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.modules.battle_strategy import (
    BOTTOM_ATTACK_ID,
    pick_hit_sequence,
)
from dwar_bot.modules.botmek_presets import (
    BOTMEK_PRESETS,
    build_fight_plan,
    load_catalog,
    parse_botmek_share_html,
    select_preset,
)

ROOT = Path(__file__).resolve().parents[1]


def test_select_preset_by_level_newbie():
    p = select_preset(level=2)
    assert p.min_level <= 2 <= p.max_level
    assert p.hit_zone == BOTTOM_ATTACK_ID
    assert "down" in p.burst_steps[-1] or p.hit_seq[-1] == BOTTOM_ATTACK_ID


def test_select_preset_rakhari():
    p = select_preset(level=18, name_hint="ракхари")
    assert "Ракхари" in p.name
    assert p.stance == "magic"
    assert "crit_arrow" in p.burst_steps


def test_build_fight_plan_enables_magic_high_level():
    plan = build_fight_plan(level=16, enabled=True)
    assert plan is not None
    assert plan.preset.downloads >= 0
    seq = plan.fallback_hit_seq()
    assert seq
    assert seq[-1] == BOTTOM_ATTACK_ID or plan.preset.hit_zone == BOTTOM_ATTACK_ID


def test_pick_hit_sequence_botmek_fallback():
    seq = pick_hit_sequence(
        None,
        configured=None,
        botmek_fallback=[3, 2, 3],
        source_label="верка",
    )
    assert seq == [3, 2, 3]


def test_pick_hit_sequence_combo_beats_botmek():
    conf = {
        "combos": '<combo id="9" description="x" level="1" seq="212" title="srv"/>'
    }
    seq = pick_hit_sequence(conf, botmek_fallback=[3, 3, 3], source_label="верка")
    assert seq == [2, 1, 2]


def test_catalog_snapshot_exists():
    rows = load_catalog(ROOT / "data" / "botmek_catalog.json")
    assert len(rows) >= 3
    assert any(r.get("file_id") for r in rows)


def test_parse_share_html_smoke():
    sample = (
        '<a class="box-row" href="/share/?file=Abc123XYZ">'
        '<div class="box-cell title" title="desc long enough here">лабиринт</div>'
        '<div class="box-cell title" title="Количество загрузок">920</div></a>'
    )
    rows = parse_botmek_share_html(sample)
    assert rows
    assert rows[0]["file_id"] == "Abc123XYZ"
    assert rows[0]["downloads"] == 920


def test_presets_cover_botmek_top_names():
    names = " ".join(p.name.lower() for p in BOTMEK_PRESETS)
    for needle in ("лабиринт", "ракхари", "верка", "обитель"):
        assert needle in names


def test_fight_client_mentions_botmek():
    src = (ROOT / "modules" / "fight_client.py").read_text(encoding="utf-8")
    assert "botmek" in src.lower()
    assert "TO_FS_PF_MAGIC" in src
