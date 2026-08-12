"""False-pause guards: heal Tracebacks / fight wins must not PAUSE the farm."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.core.ai_healing.gemini_auditor import _heuristic_audit, _strip_healing_noise
from dwar_bot.core.self_healing.watcher import AutonomousLogWatcher


ROOT = Path(__file__).resolve().parents[1]


def test_strip_healing_noise_drops_cursor_timeout():
    raw = (
        "2026-08-03 INFO | dwar_bot.modules.combat_engine | Бой выигран (ударов=5).\n"
        "2026-08-03 ERROR | dwar_bot.core.ai_healing.cursor_executor | "
        "CursorExecutor: Cursor CLI timed out (150s)\n"
        "Traceback (most recent call last):\n"
        "subprocess.TimeoutExpired: timed out after 150 seconds\n"
    )
    clean = _strip_healing_noise(raw)
    assert "Бой выигран" in clean
    assert "CursorExecutor" not in clean
    assert "TimeoutExpired" not in clean


def test_heuristic_no_crash_on_cursor_traceback():
    log = (
        "2026-08-03 ERROR | dwar_bot.core.ai_healing.cursor_executor | fail\n"
        "Traceback (most recent call last):\n"
        "subprocess.TimeoutExpired: Command timed out after 150 seconds\n"
        "2026-08-03 INFO | dwar_bot.modules.combat_engine | Бой выигран (ударов=5).\n"
    )
    v = _heuristic_audit(log, {"exp_delta": 0, "gold_delta": 0, "wins": 1}, "FARMING")
    assert v is not None
    assert v["issue_detected"] is False


def test_heuristic_no_stuck_after_fight_win():
    log = (
        "2026-08-03 INFO | dwar_bot.modules.quest_tracker | NPC 816: диалог не сдвинулся\n"
        "2026-08-03 INFO | dwar_bot.modules.combat_engine | Бой выигран (ударов=5).\n"
        "2026-08-03 INFO | dwar_bot.core.telemetry_engine | telemetry battle WIN «hunt»\n"
    )
    v = _heuristic_audit(log, {"exp_delta": 0, "gold_delta": 0, "progress": "stuck"}, "FARMING")
    assert v is not None
    assert v["issue_detected"] is False


def test_watcher_ignores_cursor_executor_errors():
    chunk = (
        "2026-08-03 21:23:54 | ERROR | dwar_bot.core.ai_healing.cursor_executor | "
        "CursorExecutor: Cursor CLI timed out (150s) on agent\n"
        "Traceback (most recent call last):\n"
        "subprocess.TimeoutExpired: timed out\n"
    )
    assert AutonomousLogWatcher._is_actionable(chunk) is False


def test_watcher_ignores_telemetry_battle_error():
    """SUIS kill-limit / hygiene → INFO 'telemetry battle ERROR' must NOT pause farm."""
    chunk = (
        "2026-08-04 01:45:05 | INFO | dwar_bot.modules.combat_engine | "
        "SUIS: лимит убийств 50 достигнут.\n"
        "2026-08-04 01:45:32 | INFO | dwar_bot.core.telemetry_engine | "
        "telemetry battle ERROR «Зигред-воин» hunt dur=27 сек dps=0.0 eff=30 pots=0\n"
        "2026-08-04 01:45:38 | INFO | dwar_bot.main | [60] xylophaze Lv3 | area=932\n"
    )
    assert AutonomousLogWatcher._is_actionable(chunk) is False
    assert AutonomousLogWatcher._extract_failed_file(chunk, chunk) == ""


def test_watcher_actionable_on_real_traceback():
    chunk = (
        "2026-08-04 03:00:00 | ERROR | dwar_bot.main | boom\n"
        "Traceback (most recent call last):\n"
        '  File "/root/dwar_bot/modules/combat_engine.py", line 10, in run\n'
        "    raise RuntimeError('x')\n"
        "RuntimeError: x\n"
    )
    assert AutonomousLogWatcher._is_actionable(chunk) is True
    assert "combat_engine.py" in AutonomousLogWatcher._extract_failed_file(
        AutonomousLogWatcher._extract_traceback(chunk), chunk
    )


def test_markers_present_in_sources():
    g = (ROOT / "core" / "ai_healing" / "gemini_auditor.py").read_text(encoding="utf-8")
    assert "HEURISTIC_NO_FALSE_PAUSE_V1" in g
    w = (ROOT / "core" / "self_healing" / "watcher.py").read_text(encoding="utf-8")
    assert "Cursor CLI timed out" in w
    assert "telemetry battle error" in w
    assert "circuit-breaker-farm" in w
