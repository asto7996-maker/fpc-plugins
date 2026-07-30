"""Tests for invite codes and access helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tg_pool.config import CREATOR_TELEGRAM_ID, Settings
from tg_pool.db.models import UserRole
from tg_pool.db.session import create_all, dispose_engine, init_engine, session_scope
from tg_pool.services.access_service import AccessService, generate_invite_code


def _settings(db_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
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


class InviteCodeFormatTests(unittest.TestCase):
    def test_format(self) -> None:
        code = generate_invite_code()
        self.assertRegex(code, r"^ALT-[A-Z0-9]{4}-[A-Z0-9]{4}$")


class AccessServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "access.db"
        self.settings = _settings(db_path)
        init_engine(self.settings)
        await create_all()

    async def asyncTearDown(self) -> None:
        await dispose_engine()
        self.tmp.cleanup()

    async def test_creator_auto_approved(self) -> None:
        async with session_scope() as session:
            svc = AccessService(session, creator_id=CREATOR_TELEGRAM_ID)
            user = await svc.ensure_user(CREATOR_TELEGRAM_ID, username="boss")
            self.assertTrue(user.is_approved)
            self.assertEqual(user.role, UserRole.creator)
            self.assertTrue(svc.has_access(user, CREATOR_TELEGRAM_ID))

    async def test_new_user_locked(self) -> None:
        async with session_scope() as session:
            svc = AccessService(session, creator_id=CREATOR_TELEGRAM_ID)
            user = await svc.ensure_user(111, username="newbie")
            self.assertFalse(user.is_approved)
            self.assertFalse(svc.has_access(user, 111))

    async def test_redeem_invite(self) -> None:
        async with session_scope() as session:
            svc = AccessService(session, creator_id=CREATOR_TELEGRAM_ID)
            invite = await svc.create_invite(CREATOR_TELEGRAM_ID)
            code = invite.code

        async with session_scope() as session:
            svc = AccessService(session, creator_id=CREATOR_TELEGRAM_ID)
            user = await svc.redeem_invite(code, 222, username="guest")
            self.assertTrue(user.is_approved)
            self.assertEqual(user.role, UserRole.admin)

        async with session_scope() as session:
            svc = AccessService(session, creator_id=CREATOR_TELEGRAM_ID)
            with self.assertRaises(ValueError):
                await svc.redeem_invite(code, 333, username="other")


class TextsImportTests(unittest.TestCase):
    def test_main_menu_html(self) -> None:
        from tg_pool.admin.texts import main_menu_text

        text = main_menu_text()
        self.assertIn("<b>", text)
        self.assertIn("Главное меню", text)
