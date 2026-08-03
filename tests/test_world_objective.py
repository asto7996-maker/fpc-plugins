"""Tests for world-objective NPC ban (heal wounded loop fix)."""

from __future__ import annotations

from unittest.mock import MagicMock

from dwar_bot.modules.quest_tracker import QuestTracker


def _tracker() -> QuestTracker:
    return QuestTracker(MagicMock())


def test_detect_heal_wounded_kind():
    kind = QuestTracker.detect_world_objective_kind(
        "Поговорить об излечении ополченцев",
        "Вы раздали снадобье раненым? Скорее, воин!",
        "Уже спешу, вождь!",
    )
    assert kind == "heal_wounded"


def test_clear_exhausted_preserves_world_objective_ban():
    qt = _tracker()
    qt.set_world_objective(
        kind="heal_wounded",
        title="Излечение",
        npc_id="409",
        artikul_id="18209",
        ban_key="0:409:0",
        ban_sec=1800,
    )
    qt._exhausted_dialogues.add("0:817:0")
    qt._exhausted_dialogues.add("1:999:0")
    cleared = qt.clear_exhausted(local_only=True)
    assert cleared >= 1
    assert "0:409:0" in qt._exhausted_dialogues
    assert "1:999:0" in qt._exhausted_dialogues
    assert "0:817:0" not in qt._exhausted_dialogues
    assert "409" in qt.exhausted_npc_ids()
    assert qt.has_world_objective("heal_wounded")


def test_clear_exhausted_full_keeps_world_keys():
    qt = _tracker()
    qt.set_world_objective(
        kind="heal_wounded",
        npc_id="409",
        ban_key="0:409:0",
    )
    qt._exhausted_dialogues.add("0:1:0")
    qt.clear_exhausted(local_only=False)
    assert qt._exhausted_dialogues == {"0:409:0"}
    assert qt.has_world_objective()


def test_set_world_objective_clears_hunt_gate():
    qt = _tracker()
    qt.pending_hunt_mob = "Крэтс"
    qt._pending_type2 = {"npc_id": "409"}
    qt.set_world_objective(kind="heal_wounded", npc_id="409", ban_key="0:409:0")
    assert qt.pending_hunt_mob == ""
    assert not qt.has_pending_type2()


def test_purge_extends_world_objective_ban():
    qt = _tracker()
    qt.set_world_objective(kind="heal_wounded", npc_id="409", ban_key="0:409:0", ban_sec=1)
    # Force expiry
    qt._soft_ban_until["0:409:0"] = 0
    ids = qt.exhausted_npc_ids()
    assert "409" in ids
    assert "0:409:0" in qt._exhausted_dialogues
    assert qt._soft_ban_until["0:409:0"] > 0
