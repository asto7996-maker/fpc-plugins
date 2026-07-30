"""
Integrated startup + on-demand self-testing for tg_pool.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import aiohttp
from sqlalchemy import select

from tg_pool.core.database_manager import DatabaseManager
from tg_pool.core.formatting import sanitize_html, sanitize_markdown, strip_html_tags
from tg_pool.core.rate_limiter import RateLimiter
from tg_pool.core.spintax import SpintaxEngine
from tg_pool.core.userbot_manager import UserbotManager
from tg_pool.db.models import Account, Proxy
from tg_pool.db.session import session_scope
from tg_pool.services.proxy_finder import ProxyCandidate, check_proxy_tcp

if TYPE_CHECKING:
    from aiogram import Bot

    from tg_pool.config import Settings
    from tg_pool.services.alerts import AlertService
    from tg_pool.services.listener_manager import ListenerManager

logger = logging.getLogger(__name__)

# Complex nested template for startup spintax stress
_SPINTAX_STRESS = (
    "{Привет|Здравствуйте|Хей}, {друг|коллега}! "
    "{Нужен|Ищешь} {VPN|впн|прокси}? "
    "{Попробуй|Загляни в} {бот|сервис} {@PaskodVPN_bot|PaskodVPN} "
    "{—|-} {быстро|удобно} и {без ссылок|без рекламы} "
    "{😊|👍|🚀|{🔥|✨}}"
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    level: str  # OK | WARNING | CRITICAL
    detail: str
    duration_ms: float = 0.0

    def line(self) -> str:
        tag = {"OK": "OK", "WARNING": "WARNING", "CRITICAL": "CRITICAL"}.get(
            self.level, self.level
        )
        return f"[{tag}] {self.name} ({self.detail})"


@dataclass
class SelfTestReport:
    checks: list[CheckResult] = field(default_factory=list)
    started_ms: float = 0.0
    finished_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return all(c.level != "CRITICAL" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.level == "WARNING" for c in self.checks)

    def format_html(self) -> str:
        lines = ["🩺 <b>Self-Test Report</b>", ""]
        for c in self.checks:
            icon = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "❌"}.get(c.level, "•")
            lines.append(
                f"{icon} <code>[{c.level}]</code> <b>{c.name}</b>\n"
                f"<blockquote>{sanitize_html(c.detail, escape_all=True)}</blockquote>"
            )
        status = "PASS" if self.ok else "FAIL"
        lines.append("")
        lines.append(
            f"Итог: <b>{status}</b> · {len(self.checks)} checks · "
            f"{self.finished_ms - self.started_ms:.0f}ms"
        )
        return "\n".join(lines)

    def format_plain(self) -> str:
        return "\n".join(c.line() for c in self.checks)


async def _check_database() -> CheckResult:
    t0 = time.perf_counter()
    db = DatabaseManager()
    try:
        ping_ms = await db.ping()
        await db.crud_smoke()
        integrity = await db.self_test_integrity()
        ms = (time.perf_counter() - t0) * 1000.0
        if not integrity.ok:
            return CheckResult(
                "Database",
                False,
                "CRITICAL",
                f"integrity failed: {integrity.detail}; ping={ping_ms:.1f}ms",
                ms,
            )
        return CheckResult(
            "Database",
            True,
            "OK",
            f"Response time: {ping_ms:.1f}ms; {integrity.detail}",
            ms,
        )
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000.0
        return CheckResult(
            "Database", False, "CRITICAL", f"{type(exc).__name__}: {exc}", ms
        )


async def _check_spintax() -> CheckResult:
    t0 = time.perf_counter()
    engine = SpintaxEngine()
    report = engine.test_template_variety(_SPINTAX_STRESS, samples=100)
    ms = (time.perf_counter() - t0) * 1000.0
    if not report.ok:
        return CheckResult(
            "Spintax Engine", False, "CRITICAL", report.detail, ms
        )
    return CheckResult(
        "Spintax Engine",
        True,
        "OK",
        f"100% valid · {report.detail}",
        ms,
    )


async def _check_formatting() -> CheckResult:
    t0 = time.perf_counter()
    try:
        sample = '<b>Hi</b> <script>alert(1)</script> & co'
        cleaned = sanitize_html(sample)
        if "<script>" in cleaned.lower() and "&lt;script&gt;" not in cleaned.lower():
            raise RuntimeError("script tag not escaped")
        if "<b>Hi</b>" not in cleaned:
            raise RuntimeError("allowed <b> stripped unexpectedly")
        md = sanitize_markdown("hello_world *x*")
        if "_" not in md and "\\_" not in md:
            raise RuntimeError("markdown escape failed")
        plain = strip_html_tags("<b>x</b>")
        if plain != "x":
            raise RuntimeError("strip_html_tags failed")
        ms = (time.perf_counter() - t0) * 1000.0
        return CheckResult("Formatting", True, "OK", "HTML/Markdown sanitizers OK", ms)
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000.0
        return CheckResult(
            "Formatting", False, "CRITICAL", f"{type(exc).__name__}: {exc}", ms
        )


async def _check_rate_limiter() -> CheckResult:
    t0 = time.perf_counter()
    limiter = RateLimiter(limit=5, window_sec=60.0)
    sim = limiter.simulate_load(events=20, step=1.0)
    ms = (time.perf_counter() - t0) * 1000.0
    if not sim.ok:
        return CheckResult("RateLimiter", False, "CRITICAL", sim.detail, ms)
    return CheckResult("RateLimiter", True, "OK", sim.detail, ms)


async def _check_telegram_api(bot: Optional["Bot"]) -> CheckResult:
    t0 = time.perf_counter()
    try:
        if bot is not None:
            me = await bot.get_me()
            ms = (time.perf_counter() - t0) * 1000.0
            return CheckResult(
                "Telegram API",
                True,
                "OK",
                f"bot @{me.username} id={me.id}",
                ms,
            )
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.telegram.org") as resp:
                status = resp.status
        ms = (time.perf_counter() - t0) * 1000.0
        if status >= 500:
            return CheckResult(
                "Telegram API", False, "WARNING", f"HTTP {status}", ms
            )
        return CheckResult("Telegram API", True, "OK", f"HTTP {status}", ms)
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000.0
        return CheckResult(
            "Telegram API", False, "CRITICAL", f"{type(exc).__name__}: {exc}", ms
        )


async def _check_proxies(*, deep: bool = True) -> CheckResult:
    t0 = time.perf_counter()
    async with session_scope() as session:
        bound = list(
            (
                await session.execute(
                    select(Proxy).where(Proxy.assigned_account_id.is_not(None))
                )
            ).scalars().all()
        )
        # also include proxies referenced by accounts
        accounts = list(
            (await session.execute(select(Account).where(Account.proxy_id.is_not(None))))
            .scalars()
            .all()
        )
        by_id = {int(p.id): p for p in bound}
        for acc in accounts:
            if acc.proxy_id is not None and int(acc.proxy_id) not in by_id:
                row = await session.get(Proxy, acc.proxy_id)
                if row is not None:
                    by_id[int(row.id)] = row
        proxies = list(by_id.values())

    if not proxies:
        ms = (time.perf_counter() - t0) * 1000.0
        return CheckResult(
            "Active Proxies",
            True,
            "OK",
            "0 bound proxies (nothing to probe)",
            ms,
        )

    if not deep:
        alive = sum(1 for p in proxies if p.is_alive)
        ms = (time.perf_counter() - t0) * 1000.0
        level = "OK" if alive == len(proxies) else "WARNING"
        return CheckResult(
            "Active Proxies",
            level != "CRITICAL",
            level,
            f"{alive}/{len(proxies)} marked alive in DB",
            ms,
        )

    import asyncio

    online = 0
    for p in proxies:
        cand = ProxyCandidate(
            ip=p.ip,
            port=int(p.port),
            protocol=p.protocol,
            username=p.username,
            password=p.password,
        )
        ok = await asyncio.to_thread(check_proxy_tcp, cand, timeout=4.0)
        if ok:
            online += 1
        else:
            async with session_scope() as session:
                row = await session.get(Proxy, p.id)
                if row is not None:
                    row.is_alive = False

    ms = (time.perf_counter() - t0) * 1000.0
    if online == 0 and proxies:
        return CheckResult(
            "Active Proxies",
            False,
            "WARNING",
            f"{online}/{len(proxies)} online",
            ms,
        )
    level = "OK" if online == len(proxies) else "WARNING"
    return CheckResult(
        "Active Proxies",
        True,
        level,
        f"{online}/{len(proxies)} online",
        ms,
    )


async def _check_agents(
    listeners: Optional["ListenerManager"],
    *,
    live_connect: bool = False,
) -> list[CheckResult]:
    """Per-agent status. live_connect=True runs get_me (slower)."""
    um = UserbotManager(listeners)
    ids = await um.list_agent_ids()
    if not ids:
        return [
            CheckResult("Agents", True, "OK", "no accounts loaded", 0.0)
        ]

    results: list[CheckResult] = []
    if not live_connect:
        async with session_scope() as session:
            for aid in ids:
                acc = await session.get(Account, aid)
                if acc is None:
                    continue
                status = acc.status.value
                flood_left = 0
                if acc.flood_until is not None:
                    from datetime import datetime, timezone

                    now = datetime.now(timezone.utc)
                    fu = acc.flood_until
                    if fu.tzinfo is None:
                        fu = fu.replace(tzinfo=timezone.utc)
                    flood_left = max(0, int((fu - now).total_seconds()))
                if flood_left > 0:
                    results.append(
                        CheckResult(
                            f"Agent #{aid}",
                            False,
                            "WARNING",
                            f"FloodWait: {flood_left}s remaining · {acc.phone_number}",
                            0.0,
                        )
                    )
                elif status == "banned":
                    results.append(
                        CheckResult(
                            f"Agent #{aid}",
                            False,
                            "WARNING",
                            f"banned · {acc.phone_number}",
                            0.0,
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            f"Agent #{aid}",
                            True,
                            "OK",
                            f"{status} · {acc.phone_number}",
                            0.0,
                        )
                    )
        return results

    for aid in ids:
        t0 = time.perf_counter()
        v = await um.validate_agent_session(aid)
        ms = (time.perf_counter() - t0) * 1000.0
        if v.flood_remaining_sec > 0:
            results.append(
                CheckResult(
                    f"Agent #{aid}",
                    False,
                    "WARNING",
                    f"FloodWait: {v.flood_remaining_sec}s remaining",
                    ms,
                )
            )
        elif v.ok:
            results.append(
                CheckResult(f"Agent #{aid}", True, "OK", v.detail, ms)
            )
        else:
            results.append(
                CheckResult(
                    f"Agent #{aid}", False, "WARNING", v.detail, ms
                )
            )
    return results


async def run_startup_self_test(
    *,
    settings: Optional["Settings"] = None,
    bot: Optional["Bot"] = None,
    alerts: Optional["AlertService"] = None,
    listeners: Optional["ListenerManager"] = None,
    deep_proxies: bool = True,
    live_agents: bool = False,
    notify: bool = True,
) -> SelfTestReport:
    """
    Run critical subsystem checks. Logs failures to stderr and alerts admins.
    """
    report = SelfTestReport(started_ms=time.perf_counter() * 1000.0)

    report.checks.append(await _check_database())
    report.checks.append(await _check_spintax())
    report.checks.append(await _check_formatting())
    report.checks.append(await _check_rate_limiter())
    report.checks.append(await _check_telegram_api(bot))
    report.checks.append(await _check_proxies(deep=deep_proxies))
    report.checks.extend(
        await _check_agents(listeners, live_connect=live_agents)
    )

    report.finished_ms = time.perf_counter() * 1000.0

    plain = report.format_plain()
    if report.ok:
        logger.info("Startup self-test PASS\n%s", plain)
    else:
        print(f"STARTUP SELF-TEST FAILED\n{plain}", file=sys.stderr)
        logger.error("Startup self-test FAIL\n%s", plain)

    # Startup: alert only on CRITICAL failures (warnings logged, not spammed)
    if notify and alerts is not None and not report.ok:
        try:
            await alerts.send_system(
                level="critical",
                title="Startup Self-Test FAILED",
                body=report.format_html(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send self-test alert")

    return report
