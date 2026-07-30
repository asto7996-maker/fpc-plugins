"""Unit tests for templates, backoff, work windows, DB claim, rate limits."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brand_monitor.core.rate_limiter import SlidingWindowRateLimiter
from brand_monitor.core.reply_coordinator import ReplyCoordinator
from brand_monitor.database.models import Agent
from brand_monitor.database.repository import Database
from brand_monitor.utils.backoff import ExponentialBackoff
from brand_monitor.utils.fingerprint import generate_fingerprint
from brand_monitor.utils.templates import expand_spintax, render_template


class TemplateTests(unittest.TestCase):
    def test_nested_spintax(self) -> None:
        text = expand_spintax("{Hello|{Hi|Hey}} world")
        self.assertIn(text, {"Hello world", "Hi world", "Hey world"})

    def test_deep_nested(self) -> None:
        text = expand_spintax("{A|{B|{C|C}}}")
        self.assertIn(text, {"A", "B", "C"})

    def test_context_placeholders(self) -> None:
        text = render_template(
            "Hi {agent}",
            context={"agent": "Support"},
            randomize=False,
        )
        self.assertEqual(text, "Hi Support")

    def test_zwsp_uniquify(self) -> None:
        text = render_template(
            "Hello there friend",
            randomize=True,
            emoji_chance=0.0,
            zwsp_enabled=True,
            typo_enabled=False,
            case_randomize=False,
        )
        self.assertIn("\u200b", text)


class BackoffTests(unittest.TestCase):
    def test_grows_and_caps(self) -> None:
        backoff = ExponentialBackoff(base=1.0, maximum=8.0, max_retries=10, jitter=0.0)
        delays = [backoff.next_delay() for _ in range(5)]
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 8.0])

    def test_exhausted(self) -> None:
        backoff = ExponentialBackoff(max_retries=2, jitter=0.0)
        backoff.next_delay()
        backoff.next_delay()
        self.assertTrue(backoff.exhausted)


class FingerprintTests(unittest.TestCase):
    def test_generate(self) -> None:
        fp = generate_fingerprint()
        self.assertTrue(fp.device_model)
        self.assertTrue(fp.system_version)
        self.assertTrue(fp.app_version)
        self.assertIn(fp.lang_code, {"ru", "en"})


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


class RateLimiterTests(unittest.TestCase):
    def test_hourly_limit(self) -> None:
        lim = SlidingWindowRateLimiter(
            max_per_hour=2,
            max_per_day=10,
            min_pause_sec=0,
            max_pause_sec=0,
        )
        now = datetime.now(timezone.utc)
        self.assertTrue(lim.check(1, now).allowed)
        lim.register_action(1, now)
        lim.register_action(1, now + timedelta(seconds=1))
        decision = lim.check(1, now + timedelta(seconds=2))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "hourly_limit")

    def test_min_pause(self) -> None:
        lim = SlidingWindowRateLimiter(
            max_per_hour=10,
            max_per_day=20,
            min_pause_sec=600,
            max_pause_sec=600,
        )
        now = datetime.now(timezone.utc)
        lim.register_action(7, now)
        decision = lim.check(7, now + timedelta(seconds=10))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "min_pause")


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_claimant_wins(self) -> None:
        coord = ReplyCoordinator()
        first = await coord.try_register(1, -100, 55)
        second = await coord.try_register(2, -100, 55)
        self.assertIsNotNone(first)
        self.assertIsNone(second)


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

    async def test_fingerprint_persisted(self) -> None:
        agent = await self.db.get_agent(1)
        assert agent is not None
        self.assertTrue(agent.has_fingerprint)
        again = await self.db.ensure_fingerprint(1)
        self.assertEqual(agent.device_model, again.device_model)

    async def test_seed_stopwords_and_keywords(self) -> None:
        await self.db.seed_defaults()
        keywords = await self.db.get_active_keywords()
        stops = await self.db.get_active_stop_words()
        self.assertGreaterEqual(len(keywords), 1)
        self.assertGreaterEqual(len(stops), 1)
        self.assertIn("скам", stops)

    async def test_csv_export(self) -> None:
        await self.db.try_claim_message(
            chat_id=-1,
            message_id=9,
            agent_id=1,
            trigger_keyword="help",
            source_text="need help please",
        )
        await self.db.mark_interaction_sent(-1, 9, 1, "Hello!")
        csv_data = await self.db.export_interactions_csv("1970-01-01", "2999-01-01")
        self.assertIn("trigger", csv_data)
        self.assertIn("Hello!", csv_data)


if __name__ == "__main__":
    unittest.main()
