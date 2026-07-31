"""Юнит-тесты SelfDiagnostics (DOM hypothesis, telegram format, cleanup)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from dwar_bot.core.self_diagnostics import CrashDump, SelfDiagnostics


@pytest.fixture()
def diagnostics(tmp_path: Path) -> SelfDiagnostics:
    return SelfDiagnostics(dumps_dir=tmp_path / "crash_dumps")


def test_format_telegram_crash_report(diagnostics: SelfDiagnostics) -> None:
    dump = CrashDump(
        dump_id="12345678-aaaa-bbbb-cccc-ddddeeeeffff",
        timestamp=datetime.now(timezone.utc),
        module_name="combat",
        exception_type="TimeoutError",
        exception_traceback="Traceback...\nTimeoutError: x",
        screenshot_path="/tmp/shot.png",
        html_snapshot_path="/tmp/dom.html",
        console_logs=["[error] foo", "[warning] bar"],
        page_url="https://w2.dwar.ru/game.php",
        hypothesis="Селектор изменился с #a на #b",
    )
    text = diagnostics.format_telegram_crash_report(dump)
    assert "combat" in text
    assert "TimeoutError" in text
    assert "screenshot:" in text
    assert "DOM hypothesis" in text


def test_suggest_and_score_candidates(diagnostics: SelfDiagnostics) -> None:
    html = """
    <html><body>
      <button id="strike_top" class="hit-btn">Верх</button>
      <a class="strike_btn_v2" href="#">Удар</a>
      <div class="other">x</div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    cands = diagnostics._extract_candidate_elements(soup)
    assert any(c.get("id") == "strike_top" for c in cands)
    scored = diagnostics._score_candidates("#btn_strike", cands)
    assert scored
    assert "strike" in scored[0]["suggested"].lower() or scored[0]["score"] > 0.2


def test_analyze_dom_desync_found_message(diagnostics: SelfDiagnostics) -> None:
    html = '<button id="btn_strike_v2" class="attack-btn strike_btn_v2">Бить</button>'
    soup = BeautifulSoup(html, "html.parser")
    cands = diagnostics._extract_candidate_elements(soup)
    scored = diagnostics._score_candidates("#btn_strike", cands)
    assert scored, "ожидался хотя бы один похожий кандидат"
    assert "btn_strike" in scored[0]["suggested"] or "strike" in scored[0]["suggested"]


def test_cleanup_old_dumps(diagnostics: SelfDiagnostics, tmp_path: Path) -> None:
    dumps = diagnostics.dumps_dir
    old_dir = dumps / "old_dump"
    old_dir.mkdir()
    (old_dir / "x.png").write_bytes(b"1234")
    import os
    import time

    old_mtime = time.time() - 20 * 86400
    os.utime(old_dir, (old_mtime, old_mtime))
    os.utime(old_dir / "x.png", (old_mtime, old_mtime))

    result = diagnostics.cleanup_old_dumps(max_age_days=7, max_folder_size_mb=500)
    assert result["deleted_dirs"] >= 1
    assert not old_dir.exists()


def test_manifest_append(diagnostics: SelfDiagnostics) -> None:
    dump = CrashDump(
        dump_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        timestamp=datetime.now(timezone.utc),
        module_name="farm",
        exception_type="RuntimeError",
        exception_traceback="RuntimeError: fail",
    )
    diagnostics._append_manifest(dump)
    raw = json.loads(diagnostics._manifest_path.read_text(encoding="utf-8"))
    assert raw["dumps"]
    assert raw["dumps"][-1]["module_name"] == "farm"
