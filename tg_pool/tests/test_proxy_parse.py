"""Tests for flexible proxy line parsing."""

from __future__ import annotations

import unittest

from tg_pool.db.models import ProxyProtocol
from tg_pool.services.proxy_parse import ProxyParseError, parse_proxy_line


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
