"""Pytest-asyncio: DatabaseManager CRUD + integrity self-test."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from tg_pool.config import CREATOR_TELEGRAM_ID, Settings
from tg_pool.core.database_manager import DatabaseManager
from tg_pool.db.models import AccountStatus
from tg_pool.db.session import create_all, dispose_engine, init_engine, session_scope
from tg_pool.services.account_service import AccountService


@pytest_asyncio.fixture
async def db_ready(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'selftest.db'}",
        redis_url="redis://localhost:6379/0",
        admin_bot_token="",
        admin_ids=(),
        creator_id=CREATOR_TELEGRAM_ID,
        log_level="WARNING",
        telegram_api_id=1,
        telegram_api_hash="h",
        daily_action_limit=20,
        jitter_min_sec=0.01,
        jitter_max_sec=0.02,
        flood_alert_threshold_sec=300,
        spambot_username="SpamBot",
        spambot_timeout_sec=5,
        tdata_max_zip_bytes=50 * 1024 * 1024,
        gemini_api_key="",
    )
    init_engine(settings)
    await create_all()
    yield
    await dispose_engine()


@pytest.mark.asyncio
async def test_ping_and_crud_smoke(db_ready) -> None:
    db = DatabaseManager()
    ms = await db.ping()
    assert ms >= 0
    await db.crud_smoke()


@pytest.mark.asyncio
async def test_account_status_update(db_ready) -> None:
    async with session_scope() as session:
        svc = AccountService(session)
        acc = await svc.create_account(
            phone_number="+10001112233",
            session_string="s" * 40,
            api_id=1,
            api_hash="h",
            status=AccountStatus.paused,
        )
        aid = acc.id
        await svc.set_status(aid, AccountStatus.active)
    async with session_scope() as session:
        acc = await AccountService(session).get_account(aid)
        assert acc is not None
        assert acc.status == AccountStatus.active


@pytest.mark.asyncio
async def test_integrity_and_duplicate_log_cleanup(db_ready) -> None:
    db = DatabaseManager()
    report = await db.self_test_integrity(purge_stale_drafts_days=14)
    assert report.ok
    assert "accounts" in report.tables
    assert report.missing_tables == ()
