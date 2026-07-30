"""Unit tests for Gemini Draft Engine (no live Gemini / Telegram)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tg_pool.clients.gemini_client import build_system_instruction, _strip_urls
from tg_pool.config import CREATOR_TELEGRAM_ID, Settings
from tg_pool.db.models import (
    Account,
    AccountStatus,
    AutoReplySettings,
    DraftStatus,
    PendingDraft,
)
from tg_pool.db.session import create_all, dispose_engine, init_engine, session_scope
from tg_pool.services.draft_engine import draft_card_text
from tg_pool.services.draft_service import DraftService, match_trigger


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


_DEFAULT_TRIGGER = (
    r"(?i)\b(vpn|впн|вэпээн|прокси|proxy|замедлен\w*|обход\w*|"
    r"заблок\w*|не\s*работает\s*ют(уб|ube)|доступ\w*)\b"
)


class TriggerMatchTests(unittest.TestCase):
    def test_vpn_ru(self) -> None:
        self.assertIsNotNone(match_trigger("нужен впн срочно", _DEFAULT_TRIGGER))

    def test_proxy(self) -> None:
        self.assertIsNotNone(match_trigger("ищу proxy для работы", _DEFAULT_TRIGGER))

    def test_no_match(self) -> None:
        self.assertIsNone(match_trigger("привет как дела", _DEFAULT_TRIGGER))


class GeminiHelperTests(unittest.TestCase):
    def test_system_mentions_bot(self) -> None:
        text = build_system_instruction("@PaskodVPN_bot")
        self.assertIn("@PaskodVPN_bot", text)
        self.assertIn("черновик", text.lower())

    def test_strip_urls(self) -> None:
        cleaned = _strip_urls("смотри https://evil.com и example.com ок")
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("example.com", cleaned)


class DraftCardTests(unittest.TestCase):
    def test_card_html(self) -> None:
        draft = PendingDraft(
            id=7,
            account_id=1,
            chat_id=-100,
            chat_title="Test Chat",
            source_message_id=11,
            source_user_id=42,
            source_username="user",
            source_text="нужен vpn",
            matched_trigger="vpn",
            draft_text="Попробуй @PaskodVPN_bot",
            status=DraftStatus.pending,
        )
        text = draft_card_text(draft, auto_on=False)
        self.assertIn("Test Chat", text)
        self.assertIn("@PaskodVPN_bot", text)
        self.assertIn("Авто-режим", text)


class DraftServiceDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "drafts.db"
        self.settings = _settings(db_path)
        init_engine(self.settings)
        await create_all()
        async with session_scope() as session:
            session.add(
                Account(
                    phone_number="+100",
                    session_string="s",
                    api_id=1,
                    api_hash="h",
                    device_model="iPhone 13",
                    system_version="iOS 16",
                    app_version="10.0",
                    status=AccountStatus.active,
                    assistant_enabled=True,
                )
            )

    async def asyncTearDown(self) -> None:
        await dispose_engine()
        self.tmp.cleanup()

    async def test_settings_default_auto_off(self) -> None:
        async with session_scope() as session:
            cfg = await DraftService(session).get_settings()
            self.assertFalse(cfg.auto_approve_enabled)
            self.assertFalse(cfg.enabled)

    async def test_create_and_approve_status(self) -> None:
        async with session_scope() as session:
            svc = DraftService(session)
            draft = await svc.create_draft(
                account_id=1,
                chat_id=-1001,
                chat_title="Chat",
                source_message_id=5,
                source_user_id=9,
                source_username="u",
                source_text="vpn pls",
                matched_trigger="vpn",
                draft_text="смотри @PaskodVPN_bot",
            )
            self.assertEqual(draft.status, DraftStatus.pending)
            updated = await svc.set_draft_status(
                draft.id,
                DraftStatus.rejected,
                reviewed_by=CREATOR_TELEGRAM_ID,
            )
            assert updated is not None
            self.assertEqual(updated.status, DraftStatus.rejected)

    async def test_toggle_auto_approve(self) -> None:
        async with session_scope() as session:
            svc = DraftService(session)
            cfg = await svc.update_settings(auto_approve_enabled=True)
            self.assertTrue(cfg.auto_approve_enabled)


class DraftEngineFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "engine.db"
        self.settings = _settings(db_path)
        init_engine(self.settings)
        await create_all()
        async with session_scope() as session:
            session.add(
                Account(
                    phone_number="+200",
                    session_string="s",
                    api_id=1,
                    api_hash="h",
                    device_model="Pixel 7",
                    system_version="Android 14",
                    app_version="10.0",
                    status=AccountStatus.active,
                    assistant_enabled=True,
                )
            )
            svc = DraftService(session)
            await svc.update_settings(
                enabled=True,
                gemini_api_key="test-key",
                auto_approve_enabled=False,
            )

    async def asyncTearDown(self) -> None:
        await dispose_engine()
        self.tmp.cleanup()

    async def test_handle_creates_pending_and_notifies(self) -> None:
        from tg_pool.services.draft_engine import DraftEngine

        redis = MagicMock()
        redis.exists = AsyncMock(return_value=0)
        pipe = MagicMock()
        pipe.set = MagicMock()
        pipe.execute = AsyncMock(return_value=[True, True])
        redis.pipeline = MagicMock(return_value=pipe)

        bot = MagicMock()
        bot.send_message = AsyncMock(
            return_value=MagicMock(message_id=101)
        )

        engine = DraftEngine(self.settings, redis, bot=bot)

        async def _fake_generate(session, **kwargs):
            return await DraftService(session).create_draft(
                account_id=kwargs["account"].id,
                chat_id=kwargs["chat_id"],
                chat_title=kwargs["chat_title"],
                source_message_id=kwargs["source_message_id"],
                source_user_id=kwargs["source_user_id"],
                source_username=kwargs["source_username"],
                source_text=kwargs["source_text"],
                matched_trigger=kwargs["matched_trigger"],
                draft_text="Рекомендую @PaskodVPN_bot",
            )

        with patch(
            "tg_pool.services.draft_engine.generate_and_store_draft",
            new=AsyncMock(side_effect=_fake_generate),
        ):
            draft = await engine.handle_incoming_message(
                account_id=1,
                chat_id=-500,
                chat_title="VPN Talk",
                message_id=33,
                sender_id=77,
                sender_username="alice",
                text="Подскажите нормальный vpn",
            )

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft.status, DraftStatus.pending)
        bot.send_message.assert_awaited()


if __name__ == "__main__":
    unittest.main()
