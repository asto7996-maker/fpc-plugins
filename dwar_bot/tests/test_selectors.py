"""Юнит-тесты реестра селекторов config/selectors.py (без браузера)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.selectors import (  # noqa: E402
    MAIN_FRAME_NAME,
    USER_FRAME_NAME,
    CHAT_FRAME_NAME,
    BackpackSelectors,
    CombatSelectors,
    SELECTORS,
    format_validation_report,
    is_xpath,
    missing_selectors,
    split_selector_list,
    to_legacy_selector_kwargs,
)


def test_reserved_frame_names() -> None:
    assert MAIN_FRAME_NAME
    assert USER_FRAME_NAME
    assert CHAT_FRAME_NAME


def test_backpack_pockets() -> None:
    belt = BackpackSelectors()
    assert len(belt.all_pockets()) == 8
    assert "1" in belt.pocket(1)
    with pytest.raises(ValueError):
        belt.pocket(0)


def test_split_and_xpath() -> None:
    assert is_xpath('//*[@id="hp_val"]')
    assert is_xpath('(//div[@class="hp"])[1]')
    assert not is_xpath("#hp_val")
    parts = split_selector_list('#hp, .hp, [data-x="a,b"]')
    assert parts[0] == "#hp"
    assert parts[1] == ".hp"
    assert 'data-x="a,b"' in parts[2]


def test_combat_strikes_present() -> None:
    c = CombatSelectors()
    assert "top" in c.strike_top.lower() or "Верх" in c.strike_top
    assert "center" in c.strike_center.lower() or "Сердце" in c.strike_center
    assert "bottom" in c.strike_bottom.lower() or "Низ" in c.strike_bottom
    assert c.hp_current


def test_flat_dict_and_legacy_bridge() -> None:
    flat = SELECTORS.as_flat_dict()
    assert "combat.strike_top" in flat
    assert "location.resource_fish" in flat
    kw = to_legacy_selector_kwargs()
    assert "main_frame" in kw
    assert "npc_dialog_choices" in kw


def test_report_helpers() -> None:
    report = {"combat.strike_top": True, "combat.hp_current": False}
    text = format_validation_report(report)
    assert "combat.hp_current" in text
    assert missing_selectors(report) == ["combat.hp_current"]
