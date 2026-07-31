"""Юнит-тесты AnalyticsReporter (SQLite, KPI, CSV, summary)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dwar_bot.modules.analytics_reporter import (
    EVENT_AUCTION_BUY,
    EVENT_BATTLE_LOST,
    EVENT_BATTLE_WON,
    EVENT_POTION_USED,
    EVENT_RESOURCE_FARMED,
    AnalyticsReporter,
    SessionMetrics,
)


@pytest.fixture()
def reporter(tmp_path: Path) -> AnalyticsReporter:
    return AnalyticsReporter(
        db_path=tmp_path / "analytics.db",
        jsonl_path=tmp_path / "events.jsonl",
        report_interval_hours=12,
        discord_webhook_url="",
        enable_jsonl_mirror=True,
    )


def test_track_battle_and_winrate(reporter: AnalyticsReporter) -> None:
    reporter.track_event(EVENT_BATTLE_WON, {"exp": 10, "valor": 2, "gold": 0.5})
    reporter.track_event(EVENT_BATTLE_WON, {"exp": 5, "gold": 0.25})
    reporter.track_event(EVENT_BATTLE_LOST, {})
    snap = reporter.snapshot_session()
    assert snap.battles_won == 2
    assert snap.battles_lost == 1
    assert abs(snap.winrate_pct - (200.0 / 3.0)) < 0.01
    assert snap.exp_gained == 15
    assert snap.valor_gained == 2
    assert abs(snap.gold_earned - 0.75) < 1e-9


def test_resources_potions_auction(reporter: AnalyticsReporter) -> None:
    reporter.track_event(EVENT_RESOURCE_FARMED, {"name": "Аметист", "count": 3})
    reporter.track_event(EVENT_RESOURCE_FARMED, {"name": "Аметист", "count": 1})
    reporter.track_event(EVENT_POTION_USED, {"name": "Малый эликсир"})
    reporter.track_event(EVENT_AUCTION_BUY, {"gold": 1.5, "name": "Руда"})
    snap = reporter.snapshot_session()
    assert snap.resources_farmed["Аметист"] == 4
    assert snap.potions_used["Малый эликсир"] == 1
    assert snap.auctions_bought == 1
    assert abs(snap.gold_spent - 1.5) < 1e-9
    assert abs(snap.gold_earned - (-1.5)) < 1e-9


def test_generate_summary_and_daily_report(reporter: AnalyticsReporter) -> None:
    reporter.track_event(EVENT_BATTLE_WON, {"gold": 1.0, "exp": 3})
    reporter.track_event(EVENT_RESOURCE_FARMED, {"name": "Трава", "count": 2})
    text = reporter.generate_summary_report(timeframe_hours=24)
    assert "DwarBot отчёт" in text
    assert "Winrate" in text
    assert "Gold/hr" in text
    report = reporter.build_daily_report(24)
    assert report.events_count >= 2
    assert report.metrics.battles_won >= 1


def test_export_to_csv(reporter: AnalyticsReporter, tmp_path: Path) -> None:
    reporter.track_event(EVENT_BATTLE_WON, {"gold": 0.1})
    reporter.track_event(EVENT_POTION_USED, {"name": "эликсир"})
    out = tmp_path / "metrics.csv"
    path = reporter.export_to_csv(str(out))
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "event_type" in content
    assert "battle_won" in content


def test_session_metrics_duration() -> None:
    m = SessionMetrics()
    m.battles_won = 1
    assert m.duration_hours > 0
    assert m.gold_per_hour == 0.0
