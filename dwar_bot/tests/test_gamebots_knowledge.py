"""Tests for GameBots / Оповещатор knowledge (yougame.biz/threads/184351)."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.modules.gamebots_knowledge import (
    GAMEBOTS_TOGGLES,
    error_looks_occupied,
    filter_hunt_targets,
    is_occupied_target,
    load_catalog,
    save_catalog,
    split_free_busy,
    summarize_targets,
)

ROOT = Path(__file__).resolve().parents[1]


def test_toggles_include_hide_occupied():
    keys = {t.key for t in GAMEBOTS_TOGGLES}
    assert "hide_occupied" in keys
    assert "sound_puzzle" in keys
    assert any(t.label_ru == "Скрывать занятые" for t in GAMEBOTS_TOGGLES)


def test_filter_skips_occupied_and_hidden():
    bots = [
        {"id": "1", "name": "Крэтс", "fight_id": "0"},
        {"id": "2", "name": "Пес", "fight_id": "99"},
        {"id": "3", "name": "Hidden", "fight_id": "0", "hidden": "1"},
        {"id": "4", "name": "Free2", "fight_id": ""},
    ]
    free = filter_hunt_targets(bots, skip_occupied=True, skip_hidden=True)
    assert [b["id"] for b in free] == ["1", "4"]
    assert is_occupied_target(bots[1])
    f, b = split_free_busy(bots)
    assert len(f) == 2 and len(b) == 1
    assert "free=2" in summarize_targets(bots)


def test_error_looks_occupied():
    assert error_looks_occupied("Моб занят другим игроком")
    assert error_looks_occupied("Уже в бою")
    assert not error_looks_occupied("Недостаточно энергии")


def test_catalog_roundtrip(tmp_path: Path):
    path = tmp_path / "gamebots_catalog.json"
    save_catalog(path)
    data = load_catalog(path)
    assert "yougame.biz/threads/184351" in data["source"]
    assert data["integration"]["no_crack"] is True
    assert len(data["toggles"]) >= 5


def test_repo_catalog():
    cat = ROOT / "data" / "gamebots_catalog.json"
    if not cat.is_file():
        save_catalog(cat)
    data = load_catalog(cat)
    assert "Оповещатор" in data.get("product", "") or "GameBots" in data.get("product", "")
