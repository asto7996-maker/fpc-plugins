"""
Auto-discover working proxies from public verified lists.

Fetches SOCKS5/HTTP candidates from known sources, probes Telegram DC
connectivity, and persists live proxies into the DB pool.

Important: network I/O never holds an open DB session — that used to lock
SQLite and make admin-bot reply buttons look dead.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from typing import Optional, Sequence

import aiohttp
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_pool.db.models import Proxy, ProxyProtocol
from tg_pool.db.session import session_scope
from tg_pool.services.account_service import AccountService

logger = logging.getLogger(__name__)

# Public, frequently updated proxy list endpoints (SOCKS5 first — better for MTProto)
PROXY_SOURCES: tuple[tuple[str, ProxyProtocol], ...] = (
    (
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5"
        "&timeout=10000&country=all&ssl=all&anonymity=all",
        ProxyProtocol.socks5,
    ),
    (
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
        ProxyProtocol.socks5,
    ),
    (
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        ProxyProtocol.socks5,
    ),
    (
        "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        ProxyProtocol.socks5,
    ),
    (
        "https://www.proxy-list.download/api/v1/get?type=socks5",
        ProxyProtocol.socks5,
    ),
    (
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http"
        "&timeout=10000&country=all&ssl=all&anonymity=all",
        ProxyProtocol.http,
    ),
    (
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        ProxyProtocol.http,
    ),
)

# Telegram DC2 — used only as a TCP reachability probe through the proxy
_TG_PROBE_HOST = "149.154.167.51"
_TG_PROBE_PORT = 443

_IP_PORT_RE = re.compile(
    r"^\s*(?:(?P<user>[^:\s]+):(?P<password>[^@\s]+)@)?"
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3}):(?P<port>\d{2,5})\s*$"
)

# Global lock so overlapping proxy scans do not thrash the thread pool
_scan_lock = asyncio.Lock()


@dataclass(frozen=True)
class ProxyCandidate:
    ip: str
    port: int
    protocol: ProxyProtocol
    username: Optional[str] = None
    password: Optional[str] = None


def parse_proxy_candidates(text: str, protocol: ProxyProtocol) -> list[ProxyCandidate]:
    """Parse plain `ip:port` / `user:pass@ip:port` lines from a list body."""
    out: list[ProxyCandidate] = []
    seen: set[tuple[str, int, str]] = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # strip accidental scheme
        if "://" in line:
            line = line.split("://", 1)[1]
        m = _IP_PORT_RE.match(line)
        if not m:
            continue
        ip = m.group("ip")
        port = int(m.group("port"))
        if not (1 <= port <= 65535):
            continue
        key = (ip, port, protocol.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ProxyCandidate(
                ip=ip,
                port=port,
                protocol=protocol,
                username=m.group("user"),
                password=m.group("password"),
            )
        )
    return out


def check_proxy_tcp(candidate: ProxyCandidate, *, timeout: float = 6.0) -> bool:
    """Blocking TCP probe to Telegram DC via the proxy (run in a thread)."""
    import socks

    proxy_type = (
        socks.SOCKS5 if candidate.protocol == ProxyProtocol.socks5 else socks.HTTP
    )
    sock = socks.socksocket()
    try:
        sock.set_proxy(
            proxy_type,
            candidate.ip,
            int(candidate.port),
            rdns=True,
            username=candidate.username,
            password=candidate.password,
        )
        sock.settimeout(timeout)
        sock.connect((_TG_PROBE_HOST, _TG_PROBE_PORT))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


async def fetch_candidates(
    *,
    session: Optional[aiohttp.ClientSession] = None,
    limit_per_source: int = 120,
) -> list[ProxyCandidate]:
    """Download proxy lists and return a shuffled unique candidate pool."""
    owns = session is None
    if session is None:
        timeout = aiohttp.ClientTimeout(total=15)
        session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "tg_pool-proxy-finder/1.0"},
        )
    candidates: list[ProxyCandidate] = []
    try:
        for url, protocol in PROXY_SOURCES:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning("Proxy source HTTP %s: %s", resp.status, url)
                        continue
                    text = await resp.text(errors="ignore")
                parsed = parse_proxy_candidates(text, protocol)[:limit_per_source]
                logger.info("Source %s → %s candidates", url[:60], len(parsed))
                candidates.extend(parsed)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Proxy source failed %s: %s", url, exc)
    finally:
        if owns:
            await session.close()

    uniq: dict[tuple[str, int, str], ProxyCandidate] = {}
    for c in candidates:
        uniq[(c.ip, c.port, c.protocol.value)] = c
    out = list(uniq.values())
    random.shuffle(out)
    # Probe SOCKS5 first — HTTP CONNECT often passes TCP but fails MTProto
    out.sort(key=lambda c: 0 if c.protocol == ProxyProtocol.socks5 else 1)
    return out


async def find_working_proxies(
    *,
    needed: int = 5,
    max_checks: int = 60,
    concurrency: int = 12,
    timeout: float = 3.5,
) -> list[ProxyCandidate]:
    """
    Fetch public lists and return up to `needed` proxies that can reach Telegram.

    Does not touch the database.
    """
    async with _scan_lock:
        pool = await fetch_candidates()
        if not pool:
            return []
        pool = pool[: max(1, max_checks)]
        logger.info("Checking %s candidates for Telegram reachability…", len(pool))

        sem = asyncio.Semaphore(concurrency)
        found: list[ProxyCandidate] = []
        lock = asyncio.Lock()
        stop = asyncio.Event()

        async def _one(c: ProxyCandidate) -> None:
            if stop.is_set():
                return
            async with sem:
                if stop.is_set():
                    return
                ok = await asyncio.to_thread(check_proxy_tcp, c, timeout=timeout)
            if not ok:
                return
            async with lock:
                found.append(c)
                logger.info(
                    "Live proxy %s://%s:%s",
                    c.protocol.value,
                    c.ip,
                    c.port,
                )
                if len(found) >= needed:
                    stop.set()

        await asyncio.gather(*(_one(c) for c in pool), return_exceptions=True)
        return found[:needed]


async def _save_candidates(
    session: AsyncSession, live: Sequence[ProxyCandidate]
) -> list[Proxy]:
    svc = AccountService(session)
    saved: list[Proxy] = []
    for c in live:
        proxy = await svc.get_or_create_proxy(
            ip=c.ip,
            port=c.port,
            protocol=c.protocol,
            username=c.username,
            password=c.password,
        )
        proxy.is_alive = True
        saved.append(proxy)
    await session.flush()
    return saved


async def refresh_proxy_pool(
    session: AsyncSession | None = None,
    *,
    needed: int = 8,
    replace: bool = True,
) -> Sequence[Proxy]:
    """
    Find working proxies and store them.

    Network scan runs without a DB session. `session` is only used for the
    short persist/replace phase; if omitted, a fresh scope is opened.
    """
    live = await find_working_proxies(needed=needed)

    async def _persist(db: AsyncSession) -> list[Proxy]:
        if replace:
            await db.execute(delete(Proxy).where(Proxy.assigned_account_id.is_(None)))
            await db.flush()
        return await _save_candidates(db, live)

    if session is not None:
        return await _persist(session)

    async with session_scope() as db:
        return await _persist(db)


async def ensure_working_proxy(*, prefer_free: bool = True) -> Optional[Proxy]:
    """
    Return a committed live proxy, scanning public lists if the pool is empty.

    Never holds a DB transaction across network I/O. Callers should use
    `proxy.id` (and re-fetch inside their own session if needed).
    """
    # 1) Short read of an existing candidate (heal bindings first)
    existing_id: int | None = None
    cand: ProxyCandidate | None = None
    async with session_scope() as db:
        svc = AccountService(db)
        await svc.heal_proxy_bindings()
        existing = await svc.pick_random_proxy(prefer_free=prefer_free)
        if existing is not None:
            existing_id = int(existing.id)
            cand = ProxyCandidate(
                ip=existing.ip,
                port=existing.port,
                protocol=existing.protocol,
                username=existing.username,
                password=existing.password,
            )

    if cand is not None and existing_id is not None:
        ok = await asyncio.to_thread(check_proxy_tcp, cand, timeout=4.0)
        if ok:
            async with session_scope() as db:
                return await db.get(Proxy, existing_id)
        async with session_scope() as db:
            dead = await db.get(Proxy, existing_id)
            if dead is not None:
                dead.is_alive = False

    # 2) Fresh public scan (no DB held)
    live = await find_working_proxies(needed=3, max_checks=80, concurrency=12)
    if not live:
        return None

    socks_first = [c for c in live if c.protocol == ProxyProtocol.socks5] or list(live)
    random.shuffle(socks_first)

    async with session_scope() as db:
        svc = AccountService(db)
        await svc.heal_proxy_bindings()
        used = await svc.used_proxy_ids()
        saved = await _save_candidates(db, live)
        # Only unbound proxies — Account.proxy_id is UNIQUE
        free = [
            p
            for p in saved
            if p.assigned_account_id is None and int(p.id) not in used
        ]
        if not free:
            return None
        for c in socks_first:
            for p in free:
                if (
                    p.ip == c.ip
                    and int(p.port) == int(c.port)
                    and p.protocol == c.protocol
                ):
                    return p
        return random.choice(free)


async def wipe_all_proxies(session: AsyncSession) -> int:
    """Unbind accounts and delete every proxy row."""
    from tg_pool.db.models import Account

    accounts = (
        await session.execute(select(Account).where(Account.proxy_id.is_not(None)))
    ).scalars().all()
    for acc in accounts:
        acc.proxy_id = None
    result = await session.execute(delete(Proxy))
    await session.flush()
    return int(result.rowcount or 0)
