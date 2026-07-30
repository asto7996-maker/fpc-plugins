"""Tests for flexible proxy line parsing."""

from __future__ import annotations

import unittest

from tg_pool.db.models import ProxyProtocol
from tg_pool.services.proxy_parse import ProxyParseError, parse_proxy_line


class GetOrCreateProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import tempfile
        from pathlib import Path

        from tg_pool.config import CREATOR_TELEGRAM_ID, Settings
        from tg_pool.db.session import create_all, dispose_engine, init_engine

        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "proxy.db"
        settings = Settings(
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
        init_engine(settings)
        await create_all()

    async def asyncTearDown(self) -> None:
        from tg_pool.db.session import dispose_engine

        await dispose_engine()
        self.tmp.cleanup()

    async def test_reuses_same_endpoint(self) -> None:
        from tg_pool.db.session import session_scope
        from tg_pool.services.account_service import AccountService

        async with session_scope() as session:
            svc = AccountService(session)
            a = await svc.get_or_create_proxy(
                ip="1.2.3.4", port=1080, username="u", password="p1"
            )
            b = await svc.get_or_create_proxy(
                ip="1.2.3.4", port=1080, username="u", password="p2"
            )
            self.assertEqual(a.id, b.id)
            self.assertEqual(b.password, "p2")

    async def test_pick_random_proxy(self) -> None:
        from tg_pool.db.session import session_scope
        from tg_pool.services.account_service import AccountService

        async with session_scope() as session:
            svc = AccountService(session)
            self.assertIsNone(await svc.pick_random_proxy())
            await svc.get_or_create_proxy(
                ip="9.9.9.9", port=1080, username="a", password="b"
            )
            picked = await svc.pick_random_proxy()
            self.assertIsNotNone(picked)
            assert picked is not None
            self.assertEqual(picked.ip, "9.9.9.9")


class ProxyParseTests(unittest.TestCase):
    def test_user_pass_at_ip_port(self) -> None:
        p = parse_proxy_line("alice:s3cret@1.2.3.4:1080")
        self.assertEqual(p.protocol, ProxyProtocol.socks5)
        self.assertEqual(p.username, "alice")
        self.assertEqual(p.password, "s3cret")
        self.assertEqual(p.ip, "1.2.3.4")
        self.assertEqual(p.port, 1080)

    def test_password_with_colon(self) -> None:
        p = parse_proxy_line("u:p:ass:word@10.0.0.1:9050")
        self.assertEqual(p.username, "u")
        self.assertEqual(p.password, "p:ass:word")
        self.assertEqual(p.ip, "10.0.0.1")
        self.assertEqual(p.port, 9050)

    def test_socks5_url(self) -> None:
        p = parse_proxy_line("socks5://user:pass@8.8.8.8:1080")
        self.assertEqual(p.protocol, ProxyProtocol.socks5)
        self.assertEqual(p.username, "user")
        self.assertEqual(p.password, "pass")

    def test_http_url_no_auth(self) -> None:
        p = parse_proxy_line("http://1.1.1.1:8080")
        self.assertEqual(p.protocol, ProxyProtocol.http)
        self.assertIsNone(p.username)
        self.assertEqual(p.port, 8080)

    def test_ip_port_only(self) -> None:
        p = parse_proxy_line("9.9.9.9:1234")
        self.assertEqual(p.protocol, ProxyProtocol.socks5)
        self.assertIsNone(p.username)
        self.assertEqual(p.ip, "9.9.9.9")

    def test_invalid(self) -> None:
        with self.assertRaises(ProxyParseError):
            parse_proxy_line("not-a-proxy")
        with self.assertRaises(ProxyParseError):
            parse_proxy_line("user@host")


if __name__ == "__main__":
    unittest.main()
