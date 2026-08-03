"""Tests for two-level Gemini → Cursor AI healing."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dwar_bot.core.ai_healing.gemini_auditor import (
    GeminiAuditor,
    _extract_json,
    _heuristic_audit,
)
from dwar_bot.core.ai_healing.cursor_executor import CursorExecutor
from dwar_bot.core.ai_healing.orchestrator import (
    HealingOrchestrator,
    read_log_slice,
    read_telemetry_summary,
)
from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state


REPO = Path(__file__).resolve().parents[1]


def test_ai_healing_modules_syntax():
    for rel in (
        "dwar_bot/core/ai_healing/__init__.py",
        "dwar_bot/core/ai_healing/gemini_auditor.py",
        "dwar_bot/core/ai_healing/cursor_executor.py",
        "dwar_bot/core/ai_healing/orchestrator.py",
    ):
        src = (REPO / rel).read_text(encoding="utf-8")
        ast.parse(src)


def test_extract_json_from_fence():
    raw = 'Sure.\n```json\n{"issue_detected": true, "issue_type": "CRASH", "target_file": "modules/combat_engine.py", "cursor_prompt": "fix it"}\n```\n'
    data = _extract_json(raw)
    assert data is not None
    assert data["issue_detected"] is True
    assert data["issue_type"] == "CRASH"


def test_heuristic_detects_traceback():
    verdict = _heuristic_audit(
        "Traceback (most recent call last):\n  File combat_engine.py\nAttributeError: boom",
        {"exp_delta": 0, "gold_delta": 0},
        "FARMING",
    )
    assert verdict is not None
    assert verdict["issue_detected"] is True
    assert verdict["issue_type"] == "CRASH"
    assert "combat_engine" in verdict["target_file"]


def test_heuristic_ignores_token_expired():
    verdict = _heuristic_audit(
        "WARNING | TokenExpiredError: OAuth access_token expired — waiting for fresh cookies.\n"
        "Traceback (most recent call last):\n  File stats_parser.py\n"
        "dwar_bot.core.game_client.TokenExpiredError: waiting for fresh cookies",
        {"exp_delta": 0, "gold_delta": 0, "progress": "no_data"},
        "RUNNING",
    )
    assert verdict is not None
    assert verdict["issue_detected"] is False

def test_heuristic_healthy_when_no_signals():
    verdict = _heuristic_audit(
        "INFO | tick ok | hunt win",
        {"exp_delta": 12, "gold_delta": 0.5, "progress": "ok"},
        "FARMING",
    )
    assert verdict is not None
    assert verdict["issue_detected"] is False


def test_gemini_auditor_fallback_without_key():
    auditor = GeminiAuditor(api_key="", allow_heuristic_fallback=True)
    out = auditor.audit_bot_health(
        "Local recover for stagnation: quest_npc:Вождь",
        {"exp_delta": 0, "gold_delta": 0, "progress": "stuck"},
        "EXECUTING_QUEST",
    )
    assert out and out["issue_detected"] is True
    assert out["issue_type"] == "STUCK_NO_PROGRESS"


def test_cursor_executor_rollback_on_pytest_fail(tmp_path, monkeypatch):
    # Work inside repo so paths resolve; mock CLI + pytest
    target = REPO / "dwar_bot" / "core" / "ai_healing" / "__init__.py"
    original = target.read_text(encoding="utf-8")
    exe = CursorExecutor(timeout_sec=5)

    def fake_cli(message, env):
        # Corrupt file to force AST/pytest failure path after "success" CLI
        target.write_text("def broken(\n", encoding="utf-8")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(exe, "_run_cursor_cli", fake_cli)
    monkeypatch.setattr(
        "dwar_bot.core.ai_healing.cursor_executor._ensure_cursor_api_key",
        lambda: "test-key",
    )
    try:
        ok = exe.execute_patch(
            "dwar_bot/core/ai_healing/__init__.py",
            "break me",
            "raw error",
        )
        assert ok is False
        # File restored from backup
        assert "HealingOrchestrator" in target.read_text(encoding="utf-8") or (
            target.read_text(encoding="utf-8") == original
        )
    finally:
        target.write_text(original, encoding="utf-8")


def test_orchestrator_handle_issue_success():
    import asyncio

    set_bot_state(BotState.RUNNING)
    pauses: list[str] = []
    resumes: list[str] = []
    notes: list[str] = []

    async def pause():
        pauses.append("p")
        set_bot_state(BotState.PAUSED)

    async def resume():
        resumes.append("r")
        set_bot_state(BotState.RUNNING)

    async def notify(text: str):
        notes.append(text)

    auditor = MagicMock()
    executor = MagicMock()
    executor.execute_patch.return_value = True
    executor.last_error = ""

    orch = HealingOrchestrator(
        interval_seconds=120,
        auditor=auditor,
        executor=executor,
        notify_fn=notify,
        pause_fn=pause,
        resume_fn=resume,
        failure_cooldown_sec=0,
    )

    async def _run():
        return await orch.handle_issue(
            {
                "issue_detected": True,
                "issue_type": "STUCK_NO_PROGRESS",
                "target_file": "dwar_bot/main.py",
                "cursor_prompt": "unstick",
                "_log_slice": "loop",
            }
        )

    ok = asyncio.run(_run())
    assert ok is True
    assert pauses and resumes
    assert any("Gemini Audit Alert" in n for n in notes)
    assert any("Cursor Patch Applied" in n for n in notes)
    assert get_bot_state() == BotState.RUNNING

def test_resolve_repo_root_has_tests_or_modules():
    from dwar_bot.core.ai_healing.paths import resolve_repo_root, resolve_test_target

    root = resolve_repo_root()
    assert root.exists()
    assert (root / "tests").exists() or (root / "dwar_bot" / "tests").exists() or (
        root / "modules"
    ).exists()
    target = resolve_test_target(root if (root / "tests").exists() else root)
    assert "test" in target


def test_key_kind_aq():
    from dwar_bot.core.ai_healing.gemini_auditor import _key_kind

    assert _key_kind("AQ.Ab8RN6KlijQ4vk7GTy7m113ou6xWaFEf8kT998DVhaI5VeFakA") == "auth_key_AQ"
    assert _key_kind("AIzaSyDummy") == "standard_AIza"


def test_orchestrator_resumes_after_cursor_failure():
    import asyncio

    set_bot_state(BotState.RUNNING)
    notes: list[str] = []

    async def pause():
        set_bot_state(BotState.PAUSED)

    async def resume():
        set_bot_state(BotState.RUNNING)

    async def notify(text: str):
        notes.append(text)

    executor = MagicMock()
    executor.execute_patch.return_value = False
    executor.last_error = "pytest failed"

    orch = HealingOrchestrator(
        interval_seconds=120,
        auditor=MagicMock(),
        executor=executor,
        notify_fn=notify,
        pause_fn=pause,
        resume_fn=resume,
        failure_cooldown_sec=0,
    )
    orch._resume_on_failure = True

    ok = asyncio.run(
        orch.handle_issue(
            {
                "issue_detected": True,
                "issue_type": "CRASH",
                "target_file": "dwar_bot/main.py",
                "cursor_prompt": "fix",
                "_log_slice": "AttributeError",
            }
        )
    )
    assert ok is False
    assert get_bot_state() == BotState.RUNNING
    assert any("FAILED" in n for n in notes)
