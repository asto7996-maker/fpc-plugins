"""Parse proxy connection strings into structured fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tg_pool.db.models import ProxyProtocol


@dataclass(frozen=True)
class ParsedProxy:
    ip: str
    port: int
    protocol: ProxyProtocol
    username: Optional[str] = None
    password: Optional[str] = None


class ProxyParseError(ValueError):
    """Raised when a proxy line cannot be parsed."""


def parse_proxy_line(
    raw: str,
    *,
    default_protocol: ProxyProtocol = ProxyProtocol.socks5,
) -> ParsedProxy:
    """
    Accept common proxy notations:

    * ``user:password@ip:port``          → socks5 (default)
    * ``socks5://user:password@ip:port``
    * ``http://user:password@ip:port``
    * ``socks5://ip:port`` / ``http://ip:port``
    * ``ip:port``                        → socks5 without auth

    Password may contain ``:``; host/port are taken from the rightmost ``@`` / ``:``.
    """
    text = (raw or "").strip()
    if not text:
        raise ProxyParseError("пустая строка")

    protocol = default_protocol
    rest = text
    if "://" in text:
        proto_s, rest = text.split("://", 1)
        proto_key = proto_s.strip().lower()
        if proto_key not in {"socks5", "http"}:
            raise ProxyParseError("протокол должен быть socks5 или http")
        protocol = ProxyProtocol(proto_key)

    username: Optional[str] = None
    password: Optional[str] = None
    hostport = rest

    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        if not creds:
            raise ProxyParseError("пустой login перед @")
        if ":" in creds:
            username, password = creds.split(":", 1)
        else:
            username, password = creds, None
        username = username.strip() or None
        password = password if password is not None else None
        if password is not None:
            password = password  # keep as-is (may be empty)

    hostport = hostport.strip()
    if ":" not in hostport:
        raise ProxyParseError("нужен host:port")

    ip, port_s = hostport.rsplit(":", 1)
    ip = ip.strip().strip("[]")  # allow [ipv6]-ish brackets lightly
    if not ip:
        raise ProxyParseError("пустой host")
    if not port_s.isdigit():
        raise ProxyParseError("порт должен быть числом")
    port = int(port_s)
    if not (1 <= port <= 65535):
        raise ProxyParseError("порт вне диапазона 1–65535")

    return ParsedProxy(
        ip=ip,
        port=port,
        protocol=protocol,
        username=username,
        password=password,
    )
