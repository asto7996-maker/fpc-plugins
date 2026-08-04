"""AutoHealer must not Cursor-escalate open-farm hunt loops."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_flash_local_recover_returns_true():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "do NOT escalate AutoHealer→Cursor" in src
    # Old bug: return False after quiet flash hunt → Cursor FAIL spam
    chunk = src.split("Local recover: flash_only")[1].split(
        "World objective (non-flash)"
    )[0]
    assert "return False" not in chunk or "return True" in chunk
    assert "return True" in chunk


def test_auto_healer_skips_cursor_for_hunt_mob():
    src = (ROOT / "core" / "auto_healer.py").read_text(encoding="utf-8")
    assert "hunt_mob soft-stagnation ignored" in src
    assert 'key_l.startswith("hunt_mob:")' in src


def test_village_hunt_mob_is_krets():
    from dwar_bot.modules.suis_knowledge import village_hunt_mob, default_hunt_mob

    assert village_hunt_mob(3) == "Крэтс"
    assert village_hunt_mob(1) == "Крэтс"
    # default for Lv3 is still Zigred (outside village)
    assert "Зигред" in default_hunt_mob(3) or default_hunt_mob(3)


def test_brain_village_area_uses_krets():
    src = (ROOT / "modules" / "progression_brain.py").read_text(encoding="utf-8")
    assert "village_hunt_mob" in src
    assert '"932"' in src
