"""Unit tests for tg_pool scaffold (no live Telegram / Redis required)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tg_pool.clients.fingerprint import generate_fingerprint
from tg_pool.clients.spambot import _classify
from tg_pool.db.models import Account, AccountStatus, Proxy, ProxyProtocol
from tg_pool.taskqueue.broker import PoolTask


class FingerprintTests(unittest.TestCase):
    def test_generate(self) -> None:
        fp = generate_fingerprint()
        self.assertTrue(fp.device_model)
        self.assertTrue(fp.system_version)
        self.assertTrue(fp.app_version)


class SpamBotClassifyTests(unittest.TestCase):
    def test_good(self) -> None:
        self.assertFalse(_classify("Good news, no limits on your account"))
        self.assertFalse(_classify("Ваш аккаунт не имеет ограничений"))

    def test_bad(self) -> None:
        self.assertTrue(_classify("Your account is limited until ..."))
        self.assertTrue(_classify("К сожалению, аккаунт ограничен"))


class PoolTaskTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        task = PoolTask(kind="ping_me", payload={"x": 1}, account_id=5)
        restored = PoolTask.from_json(task.to_json())
        self.assertEqual(restored.kind, "ping_me")
        self.assertEqual(restored.payload["x"], 1)
        self.assertEqual(restored.account_id, 5)


class ModelTests(unittest.TestCase):
    def test_proxy_tuple_socks5(self) -> None:
        proxy = Proxy(
            id=1,
            ip="1.2.3.4",
            port=1080,
            username="u",
            password="p",
            protocol=ProxyProtocol.socks5,
        )
        t = proxy.as_telethon_tuple()
        self.assertEqual(t[1], "1.2.3.4")
        self.assertEqual(t[2], 1080)
        self.assertEqual(t[4], "u")

    def test_account_status_values(self) -> None:
        self.assertEqual(AccountStatus.flood_wait.value, "flood_wait")
        acc = Account(
            phone_number="+100",
            session_string="s",
            api_id=1,
            api_hash="h",
            device_model="iPhone 13",
            system_version="iOS 16",
            app_version="10.0",
            status=AccountStatus.active,
            flood_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            is_spambot_restricted=False,
            total_actions_today=0,
        )
        self.assertEqual(acc.status, AccountStatus.active)


class JitterImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_jitter(self) -> None:
        from tg_pool.clients.session_wrapper import SessionWrapper
        from tg_pool.config import Settings

        acc = Account(
            id=1,
            phone_number="+100",
            session_string="s",
            api_id=1,
            api_hash="h",
            device_model="iPhone 13",
            system_version="iOS 16",
            app_version="10.0",
            status=AccountStatus.active,
            is_spambot_restricted=False,
            total_actions_today=0,
        )
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            redis_url="redis://localhost:6379/0",
            admin_bot_token="",
            admin_ids=(),
            creator_id=7835556726,
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
        wrapper = SessionWrapper(acc, settings=settings)
        delay = await wrapper.jitter()
        self.assertGreaterEqual(delay, 0.01)
        self.assertLessEqual(delay, 0.02)


if __name__ == "__main__":
    unittest.main()
