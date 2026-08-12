"""Unit tests for core/auto_coder.py (no live Cursor / network)."""

from __future__ import annotations

import ast
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dwar_bot.core.auto_coder import (
    AutoCoder,
    HealthIssue,
    ProgressSnapshot,
)


ROOT = Path(__file__).resolve().parents[1]


def test_auto_coder_suppresses_flash_noise(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "heal_wounded: HTTP USE rejected (Не задано действие!) — cooldown 1800s.\n"
        "Post-hunt open-farm: gear done, continue pulls.\n"
        "telemetry battle WIN «Зигред-воин»\n",
        encoding="utf-8",
    )
    state = tmp_path / "state.json"
    state.write_text(
        '{"world_objective": {"kind": "heal_wounded", "flash_only": true}}',
        encoding="utf-8",
    )
    coder = AutoCoder(dry_run=True, log_path=log, repo_root=tmp_path)
    with patch("dwar_bot.config.STATE_FILE", state):
        issue = coder.check_health()
    assert issue is None
    data = __import__("json").loads(state.read_text(encoding="utf-8"))
    assert data["world_objective"].get("http_impossible") is True



def test_build_prompt_contains_required_fields():
    coder = AutoCoder(dry_run=True, log_path=ROOT / "logs" / "missing.log")
    issue = HealthIssue(
        issue_type="NO_PROGRESS",
        reason="stuck 5m",
        target_file="dwar_bot/main.py",
        evidence="HP flat",
    )
    prompt = coder.build_prompt(issue)
    assert "Легенды: Наследие Драконов" in prompt
    assert "NO_PROGRESS" in prompt
    assert "dwar_bot/main.py" in prompt
    assert "Не ломай существующую структуру методов" in prompt


def test_detect_crash_from_traceback(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "INFO ok\n"
        "Traceback (most recent call last):\n"
        '  File "/root/dwar_bot/modules/combat_engine.py", line 42, in tick\n'
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom\n",
        encoding="utf-8",
    )
    coder = AutoCoder(dry_run=True, log_path=log, stuck_window_sec=60)
    issue = coder.check_health()
    assert issue is not None
    assert issue.issue_type == "CRASH"
    assert "combat_engine.py" in issue.target_file


def test_ignore_auth_token_expired(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "Traceback (most recent call last):\n"
        '  File "/root/dwar_bot/core/game_client.py", line 1, in ensure_session\n'
        "TokenExpiredError: OAuth access_token expired — waiting for fresh cookies.\n",
        encoding="utf-8",
    )
    coder = AutoCoder(dry_run=True, log_path=log)
    assert coder.check_health() is None


def test_no_progress_detection(tmp_path):
    log = tmp_path / "bot.log"
    line = (
        "[12] xylophaze Lv3 | HP 147/147 (100%) | MP 0/0 | area=932 | "
        "158.13 зол | предметов=0 | sid=abc…\n"
    )
    log.write_text(line * 3, encoding="utf-8")
    coder = AutoCoder(dry_run=True, log_path=log, stuck_window_sec=5)
    snap = coder._collect_progress_snapshot()
    assert snap is not None
    old = ProgressSnapshot(
        ts=time.time() - 10,
        level=snap.level,
        hp=snap.hp,
        hp_max=snap.hp_max,
        gold=snap.gold,
        area_id=snap.area_id,
        battles=snap.battles,
        exp_proxy=snap.exp_proxy,
        status_fingerprint=snap.status_fingerprint,
    )
    coder._progress_history = [old]
    issue = coder._detect_no_progress()
    assert issue is not None
    assert issue.issue_type == "NO_PROGRESS"


def test_validate_syntax_ok_and_bad(tmp_path):
    coder = AutoCoder(dry_run=True, log_path=tmp_path / "x.log")
    good = tmp_path / "good.py"
    good.write_text("def ok():\n    return 1\n", encoding="utf-8")
    ok, err = coder.validate_syntax(good)
    assert ok and not err

    bad = tmp_path / "bad.py"
    bad.write_text("def broken(\n", encoding="utf-8")
    ok, err = coder.validate_syntax(bad)
    assert not ok
    assert err


def test_rollback_git_checkout(tmp_path, monkeypatch):
    coder = AutoCoder(dry_run=True, log_path=tmp_path / "x.log", repo_root=tmp_path)
    target = tmp_path / "dwar_bot" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text("x=1\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    called = {}

    def fake_run(cmd, **kwargs):
        called["cmd"] = cmd
        return MagicMock(returncode=0)

    monkeypatch.setattr("dwar_bot.core.auto_coder.subprocess.run", fake_run)
    coder.rollback(target)
    assert called["cmd"][:3] == ["git", "checkout", "--"]
    assert coder.stats["rollbacks"] == 1


def test_fix_and_complete_dry_run(tmp_path):
    target = tmp_path / "dwar_bot" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    coder = AutoCoder(
        dry_run=True,
        log_path=tmp_path / "bot.log",
        repo_root=tmp_path,
    )
    issue = HealthIssue(
        issue_type="CRASH",
        reason="test",
        target_file="dwar_bot/main.py",
        evidence="boom",
    )
    assert coder.fix_and_complete_code(issue) is False


def test_main_wires_auto_coder():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "bind_auto_coder" in src
    assert "auto_coder_120_300s" in src
    assert "AutoCoder(120-300s)" in src
