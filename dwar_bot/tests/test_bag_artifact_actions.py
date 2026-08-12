"""Tests for bag artifact_action protocol (meta.id → action_run)."""
from __future__ import annotations

from dwar_bot.core.game_client import DwarGameClient


def test_bag_action_real_id_uses_nested_id_not_key():
    meta = {
        "id": "22711",
        "action_id": "8442",
        "title": "Съесть спелые яблоки",
        "code": "ADD_HP",
    }
    assert DwarGameClient.bag_action_real_id(meta) == "22711"
    assert DwarGameClient.bag_action_real_id({}) == ""
    assert DwarGameClient.bag_action_real_id(None) == ""  # type: ignore[arg-type]


def test_run_artifact_action_method_exists():
    assert hasattr(DwarGameClient, "run_artifact_action")
    assert hasattr(DwarGameClient, "bag_action_real_id")


def test_combat_open_bag_actions_exists():
    from dwar_bot.modules import combat_engine as ce

    src = open(ce.__file__, encoding="utf-8").read()
    assert "async def open_bag_actions" in src
    assert "bag_action_real_id" in src
    assert "action_run.php" in open(
        __import__("dwar_bot.core.game_client", fromlist=["x"]).__file__,
        encoding="utf-8",
    ).read()


def test_pure_farm_opens_bag_before_hunt():
    from dwar_bot.modules import pure_farm as pf

    src = open(pf.__file__, encoding="utf-8").read()
    assert "open_bag_actions" in src
    assert "bag actions" in src.lower() or "Bag actions" in src


def test_main_village_opens_bag_actions():
    from dwar_bot import main as main_mod

    src = open(main_mod.__file__, encoding="utf-8").read()
    assert "open_bag_actions" in src
    assert "Village bag actions" in src or "periodic bag actions" in src
