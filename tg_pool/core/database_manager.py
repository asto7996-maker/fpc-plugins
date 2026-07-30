"""
DatabaseManager — thin facade over SQLAlchemy async engine with integrity self-test.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from tg_pool.db.models import Account, PendingDraft, Proxy
from tg_pool.db.session import session_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    response_ms: float
    tables: tuple[str, ...]
    missing_tables: tuple[str, ...]
    orphan_proxy_links: int
    duplicate_proxy_bindings: int
    stale_drafts_purged: int
    detail: str


class DatabaseManager:
    """
    Operational DB helper used by startup self-tests and `/selftest`.
    """

    REQUIRED_TABLES = (
        "accounts",
        "proxies",
        "panel_users",
        "invite_codes",
        "auto_reply_settings",
        "pending_drafts",
    )

    def __init__(self, engine: Optional[AsyncEngine] = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is not None:
            return self._engine
        from tg_pool.db.session import _engine

        if _engine is None:
            raise RuntimeError("Database engine is not initialized")
        return _engine

    async def ping(self) -> float:
        """Return round-trip latency in milliseconds for `SELECT 1`."""
        t0 = time.perf_counter()
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return (time.perf_counter() - t0) * 1000.0

    async def crud_smoke(self) -> None:
        """Insert/read/delete a throwaway invite-code marker row via raw SQL-safe ORM."""
        from tg_pool.db.models import InviteCode

        marker = f"SELFTEST-{int(time.time())}"
        async with session_scope() as session:
            row = InviteCode(code=marker, created_by=0, is_used=False)
            session.add(row)
            await session.flush()
            rid = row.id
            loaded = await session.get(InviteCode, rid)
            if loaded is None or loaded.code != marker:
                raise RuntimeError("CRUD smoke read-back failed")
            await session.delete(loaded)

    async def self_test_integrity(
        self,
        *,
        purge_stale_drafts_days: int = 14,
    ) -> IntegrityReport:
        """
        Check schema, broken links, duplicate proxy ownership; purge ancient drafts.
        """
        t0 = time.perf_counter()
        missing: list[str] = []
        tables: list[str] = []
        orphan = 0
        dupes = 0
        purged = 0

        async with self.engine.connect() as conn:
            def _list_tables(sync_conn) -> list[str]:
                insp = inspect(sync_conn)
                return list(insp.get_table_names())

            tables = await conn.run_sync(_list_tables)

        for name in self.REQUIRED_TABLES:
            if name not in tables:
                missing.append(name)

        async with session_scope() as session:
            # Accounts pointing at missing proxies
            accounts = list(
                (await session.execute(select(Account))).scalars().all()
            )
            proxy_ids = {
                int(p.id)
                for p in (await session.execute(select(Proxy))).scalars().all()
            }
            for acc in accounts:
                if acc.proxy_id is not None and int(acc.proxy_id) not in proxy_ids:
                    orphan += 1

            # Duplicate Account.proxy_id (should be impossible with UNIQUE, still count)
            counts = (
                await session.execute(
                    select(Account.proxy_id, func.count())
                    .where(Account.proxy_id.is_not(None))
                    .group_by(Account.proxy_id)
                    .having(func.count() > 1)
                )
            ).all()
            dupes = len(counts)

            # Heal reverse pointer drift
            from tg_pool.services.account_service import AccountService

            await AccountService(session).heal_proxy_bindings()

            # Purge very old non-pending drafts
            if purge_stale_drafts_days > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(
                    days=purge_stale_drafts_days
                )
                from tg_pool.db.models import DraftStatus

                stale = list(
                    (
                        await session.execute(
                            select(PendingDraft).where(
                                PendingDraft.status != DraftStatus.pending,
                                PendingDraft.created_at < cutoff,
                            )
                        )
                    ).scalars().all()
                )
                for row in stale:
                    await session.delete(row)
                purged = len(stale)

        ms = (time.perf_counter() - t0) * 1000.0
        ok = not missing and orphan == 0 and dupes == 0
        detail = (
            f"tables={len(tables)} missing={missing or '-'} "
            f"orphan_proxy_links={orphan} duplicate_bindings={dupes} "
            f"stale_drafts_purged={purged}"
        )
        return IntegrityReport(
            ok=ok,
            response_ms=ms,
            tables=tuple(sorted(tables)),
            missing_tables=tuple(missing),
            orphan_proxy_links=orphan,
            duplicate_proxy_bindings=dupes,
            stale_drafts_purged=purged,
            detail=detail,
        )
