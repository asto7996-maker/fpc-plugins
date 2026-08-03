"""
Tests for industrial self-healing package (AST checker, circuit breaker, wiring).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.self_healing.ast_checker import validate_python_code
from dwar_bot.core.self_healing.cursor_engine import _build_prompt, BACKUP_ROOT
from dwar_bot.core.self_healing.watcher import (
    AutonomousLogWatcher,
    MasterController,
    MAX_PATCH_ATTEMPTS,
)


def test_ast_valid_code():
    ok, err = validate_python_code("def foo(x):\n    return x + 1\n")
    assert ok is True
    assert err is None


def test_ast_syntax_error():
    ok, err = validate_python_code("def foo(\n")
    assert ok is False
    assert err is not None
    assert "SyntaxError" in err


def test_ast_blocks_os_system_rm():
    code = "import os\nos.system('rm -rf /')\n"
    ok, err = validate_python_code(code)
    assert ok is False
    assert err is not None
    assert "опасн" in err.lower() or "запрещ" in err.lower()


def test_ast_blocks_subprocess_shell_rm():
    code = "import subprocess\nsubprocess.run('rm -rf /tmp/x', shell=True)\n"
    ok, err = validate_python_code(code)
    assert ok is False
    assert err is not None


def test_cursor_engine_prompt_mentions_selectors_and_iframes():
    prompt = _build_prompt("dwar_bot/modules/hunt_farm.py", "Traceback: boom")
    assert "config/selectors.py" in prompt
    assert "HumanBehavior" in prompt
    assert "main_frame" in prompt
    assert "hunt_farm.py" in prompt
    assert "Не меняй сигнатуры" in prompt


def test_backup_root_under_data():
    assert BACKUP_ROOT.name == "backups"
    assert BACKUP_ROOT.parent.name == "data"


def test_circuit_breaker_trips_after_three_fails(tmp_path: Path):
    set_bot_state(BotState.RUNNING)
    log = tmp_path / "bot.log"
    log.write_text("", encoding="utf-8")

    notify = AsyncMock()
    controller = MasterController()
    calls = {"n": 0}

    def always_fail(failed_file: str, traceback_text: str, dom_snapshot_path=None) -> bool:
        calls["n"] += 1
        return False

    watcher = AutonomousLogWatcher(
        log_path=log,
        interval_seconds=300,
        controller=controller,
        notify_fn=notify,
        patch_fn=always_fail,
    )

    chunk = (
        '2026-08-03 12:00:00 ERROR boom\n'
        'Traceback (most recent call last):\n'
        '  File "dwar_bot/modules/hunt_farm.py", line 10, in tick\n'
        '    raise RuntimeError("x")\n'
        'RuntimeError: x\n'
    )

    async def _run() -> None:
        for _ in range(MAX_PATCH_ATTEMPTS):
            await watcher._handle_error(chunk)

        assert watcher.patch_attempts["dwar_bot/modules/hunt_farm.py"] >= MAX_PATCH_ATTEMPTS
        assert "dwar_bot/modules/hunt_farm.py" in watcher._blocked_files
        assert get_bot_state() == BotState.PAUSED
        critical = [
            c for c in notify.await_args_list
            if c.args and "CRITICAL FAIL" in c.args[0]
        ]
        assert critical, "expected CRITICAL FAIL Telegram alarm"

        before = calls["n"]
        await watcher._handle_error(chunk)
        assert calls["n"] == before

    asyncio.run(_run())


def test_success_resets_attempts_and_resumes(tmp_path: Path):
    set_bot_state(BotState.RUNNING)
    log = tmp_path / "bot.log"
    log.write_text("", encoding="utf-8")
    notify = AsyncMock()

    watcher = AutonomousLogWatcher(
        log_path=log,
        interval_seconds=300,
        controller=MasterController(),
        notify_fn=notify,
        patch_fn=lambda *a, **k: True,
    )
    watcher.patch_attempts["dwar_bot/main.py"] = 2

    chunk = (
        'Traceback (most recent call last):\n'
        '  File "dwar_bot/main.py", line 1, in <module>\n'
        '    raise ValueError("y")\n'
        'ValueError: y\n'
    )

    async def _run() -> None:
        await watcher._handle_error(chunk)
        assert watcher.patch_attempts["dwar_bot/main.py"] == 0
        assert get_bot_state() == BotState.RUNNING
        success = [
            c for c in notify.await_args_list
            if c.args and "Self-Healing Success" in c.args[0]
        ]
        assert success

    asyncio.run(_run())


def test_package_exports():
    from dwar_bot.core import self_healing

    assert callable(self_healing.validate_python_code)
    assert callable(self_healing.apply_patch_via_cursor)
    assert self_healing.AutonomousLogWatcher is AutonomousLogWatcher


def test_main_wires_autonomous_watcher():
    src = (Path(__file__).resolve().parents[1] / "dwar_bot" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "AutonomousLogWatcher" in src
    assert "start_monitoring(interval_seconds=300)" in src
    assert "asyncio.create_task" in src
