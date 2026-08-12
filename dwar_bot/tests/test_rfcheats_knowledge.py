"""Tests for RF-Cheats (t=403608) hygiene knowledge."""

from __future__ import annotations

import time
from pathlib import Path

from dwar_bot.modules.rfcheats_knowledge import (
    LESSONS,
    RFCHEATS_DEFAULTS,
    HygieneTracker,
    RfCheatsDefaults,
    load_catalog,
    save_catalog,
)

ROOT = Path(__file__).resolve().parents[1]


def test_lessons_mention_bitblt_and_sessions():
    blob = " ".join(LESSONS).lower()
    assert "bitblt" in blob
    assert "break" in blob or "5–12" in " ".join(LESSONS) or "marathon" in blob


def test_hygiene_burst_triggers_pause(tmp_path: Path):
    d = RfCheatsDefaults(
        max_continuous_minutes=999,
        max_daily_minutes=999,
        burst_minutes=1,
        break_min_minutes=0.05,
        break_max_minutes=0.1,
    )
    hy = HygieneTracker(defaults=d, state_path=tmp_path / "h.json")
    hy.burst_started = time.time() - 70  # >1 min
    hy.continuous_started = hy.burst_started
    hy.last_activity = time.time()
    dec = hy.check()
    assert dec.should_pause
    assert "burst" in dec.reason
    assert dec.sleep_sec > 0


def test_hygiene_daily_budget(tmp_path: Path):
    d = RfCheatsDefaults(
        max_continuous_minutes=999,
        max_daily_minutes=1,
        burst_minutes=999,
        daily_exhausted_break_minutes=0.1,
    )
    hy = HygieneTracker(defaults=d, state_path=tmp_path / "h2.json")
    hy.daily_active_sec = 70  # >1 min
    dec = hy.check()
    assert dec.should_pause
    assert "daily" in dec.reason
    assert "midnight" in dec.reason
    assert dec.sleep_sec >= 60.0


def test_note_break_idempotent(tmp_path: Path):
    hy = HygieneTracker(defaults=RFCHEATS_DEFAULTS, state_path=tmp_path / "nb.json")
    hy.note_break(120.0)
    first = hy.break_until
    hy.note_break(9999.0)  # must not extend
    assert hy.break_until == first
    assert hy.remaining_break_sec() > 0


def test_clear_break_resumes(tmp_path: Path):
    hy = HygieneTracker(defaults=RFCHEATS_DEFAULTS, state_path=tmp_path / "cb.json")
    hy.note_break(600.0)
    hy.clear_break()
    assert hy.remaining_break_sec() == 0.0
    assert hy.check().should_pause is False


def test_action_delay_in_op_range():
    hy = HygieneTracker(defaults=RFCHEATS_DEFAULTS)
    for _ in range(20):
        s = hy.action_delay_sec()
        assert RFCHEATS_DEFAULTS.action_delay_min <= s <= RFCHEATS_DEFAULTS.action_delay_max


def test_persist_daily(tmp_path: Path):
    path = tmp_path / "state.json"
    hy = HygieneTracker(state_path=path)
    hy.note_activity(80.0)
    hy.note_activity(50.0)  # per-call cap 90s — two calls accumulate
    hy2 = HygieneTracker(state_path=path)
    assert hy2.daily_active_sec >= 120.0


def test_catalog(tmp_path: Path):
    p = tmp_path / "rfcheats_catalog.json"
    save_catalog(p)
    data = load_catalog(p)
    assert "403608" in data["source"]
    assert data["integration"]["no_pixel_bot"] is True
    assert "Random(15)" in data["op_failed_anti_detect_snippet"]


def test_repo_catalog():
    cat = ROOT / "data" / "rfcheats_catalog.json"
    if not cat.is_file():
        save_catalog(cat)
    data = load_catalog(cat)
    assert "АвтоБан" in data.get("title", "")
