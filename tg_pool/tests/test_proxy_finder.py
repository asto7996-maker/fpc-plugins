"""Tests for public proxy list parsing (no live network checks)."""

from __future__ import annotations

import unittest

from tg_pool.db.models import ProxyProtocol
from tg_pool.services.proxy_finder import parse_proxy_candidates


class ProxyCandidateParseTests(unittest.TestCase):
    def test_ip_port(self) -> None:
        text = "1.2.3.4:1080\n# comment\nbad\n5.6.7.8:9050\n"
        rows = parse_proxy_candidates(text, ProxyProtocol.socks5)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].ip, "1.2.3.4")
        self.assertEqual(rows[0].port, 1080)

    def test_user_pass(self) -> None:
        rows = parse_proxy_candidates(
            "user:pass@10.0.0.1:3128",
            ProxyProtocol.http,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].username, "user")
        self.assertEqual(rows[0].password, "pass")
        self.assertEqual(rows[0].protocol, ProxyProtocol.http)

    def test_scheme_stripped(self) -> None:
        rows = parse_proxy_candidates(
            "socks5://8.8.8.8:1080",
            ProxyProtocol.socks5,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ip, "8.8.8.8")


if __name__ == "__main__":
    unittest.main()
