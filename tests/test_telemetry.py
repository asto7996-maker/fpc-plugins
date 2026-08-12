"""Tests for TelemetryEngine, RichNotifications, AnalyticsReporter."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from dwar_bot.core.game_client import CharStats, GameState
from dwar_bot.core.game_knowledge_base import GameKnowledgeBase
from dwar_bot.core.rich_notifications import RichNotifications
from dwar_bot.core.telemetry_engine import (
    ConsumableUse,
    LootItem,
    TelemetryEngine,
)
from dwar_bot.modules.stats_parser import Artifact, FullProfile


def _profile(*, gold: float, potions: list[tuple[str, int]] | None = None,
             gear: list[tuple[str, int, int]] | None = None,
             items: list[str] | None = None) -> FullProfile:
    inv: list[Artifact] = []
    for title, n in potions or []:
        for _ in range(n):
            inv.append(Artifact(title=title, kind="Отвар", art_id=title))
    for title, dur, dmax in gear or []:
        inv.append(Artifact(title=title, kind="Оружие", durability=dur, durability_max=dmax))
    for title in items or []:
        inv.append(Artifact(title=title, kind="Ресурс"))
    return FullProfile(
        char=CharStats(nick="t", level=5, hp=80, hp_max=100),
        state=GameState(money=gold, area_id="932"),
        inventory=inv,
    )


def test_quest_telemetry_real_diffs(tmp_path: Path):
    kb = GameKnowledgeBase(tmp_path / "kb.db")
    kb.sample_auction(item_key="Эликсир гиганта", title="Эликсир гиганта", price_gold=0.40)
    kb.sample_auction(item_key="Свиток исцеления", title="Свиток исцеления", price_gold=0.03)
    tel = TelemetryEngine(tmp_path / "tel.db", price_lookup=kb)

    start = _profile(
        gold=10.0,
        potions=[("Эликсир гиганта", 3)],
        gear=[("Меч", 98, 100)],
        items=[],
    )
    # mark scrolls as kind Свиток
    start.inventory.append(Artifact(title="Свиток исцеления", kind="Свиток"))
    start.inventory.append(Artifact(title="Свиток исцеления", kind="Свиток"))
    start.inventory.append(Artifact(title="Свиток исцеления", kind="Свиток"))
    start.inventory.append(Artifact(title="Свиток исцеления", kind="Свиток"))
    start.inventory.append(Artifact(title="Свиток исцеления", kind="Свиток"))
    start.inventory.append(Artifact(title="Свиток исцеления", kind="Свиток"))

    qt = tel.start_quest("Охота на Крэтсов", profile=start, gold=10.0)
    assert qt.status == "active"
    time.sleep(0.05)

    end = _profile(
        gold=11.45,
        potions=[("Эликсир гиганта", 1), ("Малое снадобье", 1)],
        gear=[("Меч", 97, 100)],
        items=["Зуб Крэтса"] * 12,
    )
    # 1 scroll left of 6 → 5 used
    end.inventory.append(Artifact(title="Свиток исцеления", kind="Свиток"))

    done = tel.complete_quest(
        profile=end,
        gold=11.45,
        exp_gained=4500,
        exp_pct_of_level=3.2,
        valor_gained=1200,
    )
    assert done is not None
    assert done.status == "completed"
    assert done.duration_sec >= 0.05
    # 2 potions used
    pot = next(c for c in done.consumables if c.title == "Эликсир гиганта")
    assert pot.qty == 2
    assert pot.unit_cost_silver == 40.0  # 0.40 gold * 100
    scroll = next(c for c in done.consumables if "Свиток" in c.title)
    assert scroll.qty == 5
    assert abs(done.gold_gained - 1.45) < 1e-9
    assert abs(done.durability_delta_pct - (-1.0)) < 0.1
    assert any(x.title == "Зуб Крэтса" and x.qty == 12 for x in done.loot)
    assert done.exp_per_hour > 0

    rich = RichNotifications(tel)
    html = rich.format_quest_completed(done)
    assert "[КВЕСТ ВЫПОЛНЕН]" in html
    assert "Охота на Крэтсов" in html
    assert "Эликсир гиганта" in html
    assert "4 500" in html or "4500" in html.replace(" ", "")
    assert "Exp/час" in html
    assert "Чистая прибыль" in html


def test_battle_telemetry_efficiency(tmp_path: Path):
    tel = TelemetryEngine(tmp_path / "b.db")
    tel.start_battle(source="hunt", mob_name="Крэтс", potions_baseline=0, attacks_baseline=0)
    tel.note_battle_hit(damage=20)
    tel.note_battle_hit(damage=0, missed=True)
    tel.note_battle_hit(damage=25, critical=True)
    tel.note_consumable("Малое зелье", kind="potion")
    time.sleep(0.05)
    bt = tel.end_battle(result="WIN", potions_total=1, attacks_total=3)
    assert bt is not None
    assert bt.hits == 2
    assert bt.misses == 1
    assert bt.crits == 1
    assert bt.potions_used >= 1
    assert bt.dps > 0
    assert 0 < bt.efficiency_score <= 100
    html = RichNotifications(tel).format_battle_finished(bt)
    assert "[БОЙ WIN]" in html
    assert "Efficiency Score" in html


def test_economy_rates(tmp_path: Path):
    tel = TelemetryEngine(tmp_path / "e.db")
    tel.note_economy(gold=10.0, exp_proxy=0, battles=0, wins=0)
    time.sleep(0.05)
    tel.note_economy(gold=12.0, exp_proxy=500, battles=2, wins=2, potions_used=1)
    rates = tel.rates(window_sec=3600)
    assert rates["gold_per_hour"] > 0
    assert rates["exp_per_hour"] > 0
    html = RichNotifications(tel).format_farm_economy()
    assert "Gold/час" in html
    assert "Exp/час" in html


def test_main_wires_telemetry():
    src = (Path(__file__).resolve().parents[1] / "dwar_bot" / "main.py").read_text(encoding="utf-8")
    assert "TelemetryEngine" in src
    assert "RichNotifications" in src
    assert "AnalyticsReporter" in src
    assert "_telemetry_quest_complete" in src
    assert "analytics.build_full_report" in src or "self.analytics.build_full_report" in src
