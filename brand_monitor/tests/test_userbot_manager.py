"""Focused tests for UserbotManager filtering helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from brand_monitor.config import Settings
from brand_monitor.core.userbot_manager import (
    FatalAgentError,
    UserbotManager,
    _is_fatal_error,
    _is_network_error,
)
from brand_monitor.database.repository import Database
from telethon.errors import AuthKeyDuplicatedError, UserDeactivatedError


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
        self.db = Database(Path(self.tmp.name) / "mgr.db")
        await self.db.connect()
        await self.db.seed_defaults()
        self.manager = UserbotManager(
            db=self.db,
            settings=Settings(database_path=Path(self.tmp.name) / "mgr.db"),
        )
        await self.manager.reload_keywords()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_match_keyword_case_insensitive(self) -> None:
        kw = await self.manager._match_keyword("Срочно нужна помощь с заказом")
        self.assertIsNotNone(kw)
        assert kw is not None
        self.assertIn("помощ", kw.keyword.lower())

    async def test_no_match(self) -> None:
        kw = await self.manager._match_keyword("просто болтовня без триггеров")
        self.assertIsNone(kw)

    async def test_resolve_knowledge(self) -> None:
        keywords = await self.db.get_active_keywords()
        self.assertTrue(keywords)
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
        from brand_monitor.core.userbot_manager import AgentRuntime

        runtime = AgentRuntime(agent=agent)
        notifier = AsyncMock()
        self.manager.admin_notifier = notifier
        await self.manager._handle_fatal_failure(runtime, FatalAgentError("dup key"))
        updated = await self.db.get_agent(agent_id)
        assert updated is not None
        self.assertEqual(updated.status, "inactive")
        self.assertIn("FatalAgentError", updated.last_error or "")
        notifier.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
