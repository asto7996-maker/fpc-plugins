"""Unit tests for templates, backoff, work windows, and DB claim logic."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from brand_monitor.database.models import Agent
from brand_monitor.database.repository import Database
from brand_monitor.utils.backoff import ExponentialBackoff
from brand_monitor.utils.templates import render_template


class TemplateTests(unittest.TestCase):
    def test_variant_rendering(self) -> None:
        text = render_template("{Hello|Hello} world")
        self.assertEqual(text, "Hello world")

    def test_context_placeholders(self) -> None:
        text = render_template("Hi {agent}", context={"agent": "Support"})
        self.assertEqual(text, "Hi Support")


class BackoffTests(unittest.TestCase):
    def test_grows_and_caps(self) -> None:
        backoff = ExponentialBackoff(base=1.0, maximum=8.0, max_retries=10, jitter=0.0)
        delays = [backoff.next_delay() for _ in range(5)]
        self.assertEqual(delays[0], 1.0)
        self.assertEqual(delays[1], 2.0)
        self.assertEqual(delays[2], 4.0)
        self.assertEqual(delays[3], 8.0)
        self.assertEqual(delays[4], 8.0)

    def test_exhausted(self) -> None:
        backoff = ExponentialBackoff(max_retries=2, jitter=0.0)
        backoff.next_delay()
        backoff.next_delay()
        self.assertTrue(backoff.exhausted)


class WorkWindowTests(unittest.TestCase):
    def _agent(self, start: str, end: str) -> Agent:
        return Agent(
            id=1,
            phone="+100",
            session_string="x",
            api_id=1,
            api_hash="h",
            proxy_type=None,
            proxy_host=None,
            proxy_port=None,
            proxy_username=None,
            proxy_password=None,
            work_window_start=start,
            work_window_end=end,
            status="active",
        )

    def test_day_window(self) -> None:
        agent = self._agent("09:00", "18:00")
        self.assertTrue(agent.is_within_work_window(datetime(2026, 1, 1, 12, 0)))
        self.assertFalse(agent.is_within_work_window(datetime(2026, 1, 1, 20, 0)))

    def test_overnight_window(self) -> None:
        agent = self._agent("22:00", "06:00")
        self.assertTrue(agent.is_within_work_window(datetime(2026, 1, 1, 23, 0)))
        self.assertTrue(agent.is_within_work_window(datetime(2026, 1, 1, 3, 0)))
        self.assertFalse(agent.is_within_work_window(datetime(2026, 1, 1, 12, 0)))


class DatabaseClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        await self.db.connect()
        await self.db.upsert_agent(
            phone="+79990001122",
            api_id=123,
            api_hash="hash",
            session_string="session",
            status="active",
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_claim_is_exclusive(self) -> None:
        first = await self.db.try_claim_message(chat_id=-100, message_id=1, agent_id=1)
        second = await self.db.try_claim_message(chat_id=-100, message_id=1, agent_id=1)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(await self.db.is_message_processed(-100, 1))

    async def test_seed_and_keywords(self) -> None:
        await self.db.seed_defaults()
        keywords = await self.db.get_active_keywords()
        self.assertGreaterEqual(len(keywords), 1)
        kb = await self.db.list_knowledge()
        self.assertGreaterEqual(len(kb), 1)


if __name__ == "__main__":
    unittest.main()
