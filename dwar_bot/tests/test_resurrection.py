"""Tests for automatic character resurrection."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.modules.resurrection import (
    ResurrectionEngine,
    area_looks_like_ghost,
    is_dead_character,
    is_resurrect_item,
)

ROOT = Path(__file__).resolve().parents[1]


def test_is_dead_character():
    assert is_dead_character(0, 100) is True
    assert is_dead_character(0, 100, fight_id=5) is False
    assert is_dead_character(10, 100) is False
    assert is_dead_character(0, 0) is False  # stub


def test_resurrect_item_matching():
    assert is_resurrect_item("Перо Феникса", "Амулеты")
    assert is_resurrect_item("Большой свиток воскрешения", "Свиток")
    assert is_resurrect_item("Ледяное перо воскрешения", "Свиток")
    assert not is_resurrect_item("Отвар восстановления", "Отвар")
    # Mount named «Возрождение» must not be treated as self-rez
    assert not is_resurrect_item("Возрождение", "Ездовое животное")
    assert is_resurrect_item("Свиток возрождения", "Свиток")


def test_area_ghost_detection():
    class It:
        def __init__(self, name, code=""):
            self.name = name
            self.code = code

    assert area_looks_like_ghost("Храм предков")
    assert area_looks_like_ghost("", [It("Возродиться", "RESURRECT")])
    assert not area_looks_like_ghost("Дымные сопки", [It("Огненный паук")])


def test_engine_disabled_by_settings():
    eng = ResurrectionEngine()

    class Farm:
        auto_resurrect = False

    class Settings:
        farm = Farm()

    class Char:
        hp = 0
        hp_max = 100

    class State:
        fight_id = 0

    class Bot:
        settings = Settings()
        _char = Char()
        _state = State()

    assert eng.should_try(Bot()) is False


def test_engine_should_try_when_dead():
    eng = ResurrectionEngine()

    class Farm:
        auto_resurrect = True

    class Settings:
        farm = Farm()

    class Char:
        hp = 0
        hp_max = 100

    class State:
        fight_id = 0

    class Bot:
        settings = Settings()
        _char = Char()
        _state = State()

    assert eng.should_try(Bot()) is True


def test_main_wires_resurrection():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "ResurrectionEngine" in src
    assert "auto_resurrect" in src
    assert "ensure_alive" in src
    assert "_resurrect_bot" in src


def test_combat_calls_resurrect_on_loss():
    src = (ROOT / "modules" / "combat_engine.py").read_text(encoding="utf-8")
    assert "_auto_resurrect_after_loss" in src
    assert "ResurrectionEngine" in src


def test_bot_settings_auto_resurrect_default():
    from dwar_bot.modules.bot_settings import FarmSettings
    assert FarmSettings().auto_resurrect is True


def test_pure_farm_calls_resurrect():
    src = (ROOT / "modules" / "pure_farm.py").read_text(encoding="utf-8")
    assert "ensure_alive" in src
    assert "auto_resurrect" in src
