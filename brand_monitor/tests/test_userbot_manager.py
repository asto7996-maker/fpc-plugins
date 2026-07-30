"""Focused tests for UserbotManager filtering helpers and kill switch."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from telethon.errors import AuthKeyDuplicatedError, UserDeactivatedError

from brand_monitor.config import Settings
from brand_monitor.core.userbot_manager import (
    AgentRuntime,
    FatalAgentError,
    UserbotManager,
    _is_fatal_error,
    _is_network_error,
)
from brand_monitor.database.repository import Database


class ErrorClassificationTests(unittest.TestCase):
    def test_fatal_errors(self) -> None:
        self.assertTrue(_is_fatal_error(FatalAgentError("x")))
        self.assertTrue(_is_fatal_error(AuthKeyDuplicatedError(request=None)))
        self.assertTrue(_is_fatal_error(UserDeactivatedError(request=None)))

    def test_network_errors(self) -> None:
        self.assertTrue(_is_network_error(ConnectionError("proxy down")))
        self.assertTrue(_is_network_error(TimeoutError()))
        self.assertFalse(_is_network_error(FatalAgentError("no")))


class ManagerLogicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "mgr.db"
        self.db = Database(db_path)
        await self.db.connect()
        await self.db.seed_defaults()
        self.manager = UserbotManager(
            db=self.db,
            settings=Settings(
                database_path=db_path,
                admin_bot_token="",
                admin_ids=(),
                typing_delay_min=0.01,
                typing_delay_max=0.02,
                backoff_base=0.01,
                backoff_max=0.05,
                backoff_max_retries=2,
                reconnect_max_attempts=2,
                log_level="WARNING",
                max_replies_per_hour=4,
                max_replies_per_day=18,
                min_action_pause_sec=0,
                max_action_pause_sec=0,
                pre_reply_delay_min=0.01,
                pre_reply_delay_max=0.02,
                flood_wait_extra_sec=1,
                min_message_length=10,
                max_message_length=500,
                emoji_chance=0.0,
                typo_enabled=False,
                case_randomize=False,
                zwsp_enabled=False,
            ),
        )
        await self.manager.reload_filters()

    async def asyncTearDown(self) -> None:
        await self.manager.stop()
        await self.db.close()
        self.tmp.cleanup()

    async def test_match_keyword_case_insensitive(self) -> None:
        kw = await self.manager._match_keyword("Срочно нужна помощь с заказом")
        self.assertIsNotNone(kw)

    async def test_stop_word_blocks(self) -> None:
        self.assertTrue(self.manager._contains_stop_word("это полный скам ребят"))
        self.assertFalse(self.manager._contains_stop_word("нужна обычная помощь"))

    async def test_no_match(self) -> None:
        kw = await self.manager._match_keyword("просто болтовня без триггеров")
        self.assertIsNone(kw)

    async def test_resolve_knowledge(self) -> None:
        keywords = await self.db.get_active_keywords()
        entry = await self.manager._resolve_knowledge(keywords[0])
        self.assertIsNotNone(entry)

    async def test_fatal_failure_marks_inactive(self) -> None:
        agent_id = await self.db.upsert_agent(
            phone="+70001112233",
            api_id=1,
            api_hash="h",
            session_string="sess",
            status="active",
        )
        agent = await self.db.get_agent(agent_id)
        assert agent is not None
        runtime = AgentRuntime(agent=agent)
        notifier = AsyncMock()
        self.manager.admin_notifier = notifier
        await self.manager._handle_fatal_failure(runtime, FatalAgentError("dup key"))
        updated = await self.db.get_agent(agent_id)
        assert updated is not None
        self.assertEqual(updated.status, "inactive")
        notifier.assert_awaited_once()

    async def test_emergency_stop(self) -> None:
        agent_id = await self.db.upsert_agent(
            phone="+70009998877",
            api_id=1,
            api_hash="h",
            session_string="sess",
            status="active",
        )
        # Don't actually connect Telethon — just mark runtime empty and pause DB
        n = await self.manager.emergency_stop()
        self.assertTrue(self.manager.is_paused)
        agent = await self.db.get_agent(agent_id)
        assert agent is not None
        self.assertEqual(agent.status, "paused")
        self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
