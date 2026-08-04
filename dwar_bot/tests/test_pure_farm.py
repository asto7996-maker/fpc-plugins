"""Tests for rewritten PureFarm core."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.modules.pure_farm import PureFarmEngine, PureFarmStats, VILLAGE_AREAS

ROOT = Path(__file__).resolve().parents[1]


def test_should_run_max_farm_and_village():
    eng = PureFarmEngine()
    assert eng.should_run(max_farm=True, area_id="100", level=1) is True
    assert eng.should_run(max_farm=False, area_id="932", level=3) is True
    assert eng.should_run(max_farm=False, area_id="192", level=5) is False


def test_telegram_stats_show_wins():
    s = PureFarmStats(wins=10, losses=1, money_at_start=100, money_now=105, level_at_start=3, level_now=3)
    html = s.telegram_html()
    assert "Pure Farm" in html
    assert "10" in html


def test_main_wires_pure_farm():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "PureFarmEngine" in src
    assert "pure_farm.run_tick" in src
    assert 'ENABLE_SELF_HEAL' in src
    assert "PureFarm rewrite: Gemini/AutoCoder OFF" in src


def test_pure_farm_module_exists():
    assert (ROOT / "modules" / "pure_farm.py").is_file()
    assert "932" in VILLAGE_AREAS
