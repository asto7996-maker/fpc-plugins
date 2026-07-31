"""Юнит-тесты BackgroundScheduler (очередь задач, combat defer, cleanup)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dwar_bot.modules.analytics_reporter import AnalyticsReporter
from dwar_bot.modules.background_scheduler import (
    PRIORITY_HIGH,
    PRIORITY_LOW,
    TASK_CHECK_MAIL,
    BackgroundScheduler,
    ScheduledTask,
)
from dwar_bot.modules.stats_parser import PlayerStats


@pytest.fixture()
def scheduler(tmp_path: Path) -> BackgroundScheduler:
    analytics = AnalyticsReporter(
        db_path=tmp_path / "analytics.db",
        jsonl_path=tmp_path / "events.jsonl",
        discord_webhook_url="",
        enable_jsonl_mirror=True,
    )
    sched = BackgroundScheduler(
        analytics=analytics,
        poll_interval_sec=0.2,
        captcha_retention_days=0,  # удалять всё старше «сейчас-эпсилон» через mtime
        analytics_retention_days=30,
    )
    return sched


def test_scheduled_task_due_and_mark_ran() -> None:
    task = ScheduledTask(
        task_id="t1",
        interval_seconds=60,
        next_run=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert task.is_due()
    task.mark_ran(success=True)
    assert task.last_run is not None
    assert task.next_run is not None
    assert task.next_run > datetime.now(timezone.utc)


def test_register_and_list(scheduler: BackgroundScheduler) -> None:
    async def _noop(page, stats):
        return True

    scheduler.register_task(
        ScheduledTask(task_id="ping", interval_seconds=30, priority=PRIORITY_HIGH),
        _noop,
    )
    ids = {t["task_id"] for t in scheduler.list_tasks()}
    assert "ping" in ids


@pytest.mark.asyncio
async def test_combat_defers_skip_in_combat(scheduler: BackgroundScheduler) -> None:
    ran = {"count": 0}

    async def _job(page, stats):
        ran["count"] += 1
        return True

    task = ScheduledTask(
        task_id="mail",
        interval_seconds=10,
        skip_in_combat=True,
        next_run=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    scheduler.register_task(task, _job)
    scheduler.set_context(page=None, stats=PlayerStats(), in_combat=True)
    # page is None → _tick_once returns early; set a dummy page object
    class _DummyPage:
        frames = []

    scheduler.set_context(page=_DummyPage(), stats=PlayerStats(), in_combat=True)
    await scheduler._tick_once()
    assert ran["count"] == 0
    assert task.next_run is not None

    scheduler.set_in_combat(False)
    # Still won't run real mail without browser — use simple handler already registered
    # Force due again
    task.next_run = datetime.now(timezone.utc) - timedelta(seconds=1)
    await scheduler._tick_once()
    assert ran["count"] == 1


@pytest.mark.asyncio
async def test_cleanup_deletes_old_captchas(
    scheduler: BackgroundScheduler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captcha_dir = tmp_path / "captchas"
    captcha_dir.mkdir()
    old = captcha_dir / "old.png"
    old.write_bytes(b"x")
    import os
    import time

    old_mtime = time.time() - 10 * 86400
    os.utime(old, (old_mtime, old_mtime))

    monkeypatch.setattr(
        "dwar_bot.modules.background_scheduler.CAPTCHAS_DIR", captcha_dir
    )
    monkeypatch.setattr(
        "dwar_bot.modules.background_scheduler.SCREENSHOTS_DIR", tmp_path / "shots"
    )
    monkeypatch.setattr(
        "dwar_bot.modules.background_scheduler.LOGS_DIR", tmp_path / "logs"
    )
    (tmp_path / "shots").mkdir()
    (tmp_path / "logs").mkdir()

    scheduler.captcha_retention_days = 3
    ok = await scheduler.cleanup_storage()
    assert ok is True
    assert not old.exists()


def test_default_tasks_registered(scheduler: BackgroundScheduler) -> None:
    scheduler.register_default_tasks()
    ids = {t["task_id"] for t in scheduler.list_tasks()}
    assert TASK_CHECK_MAIL in ids
    assert "daily_gift" in ids
    assert "refresh_buffs" in ids
    assert "cleanup_storage" in ids


def test_cron_every_expression() -> None:
    task = ScheduledTask(task_id="x", interval_seconds=0, cron_expression="every:120")
    assert task.interval_seconds == 120
