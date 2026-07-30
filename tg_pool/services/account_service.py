"""CRUD + health transitions for Account / Proxy entities."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tg_pool.clients.fingerprint import generate_fingerprint
from tg_pool.db.models import Account, AccountStatus, Proxy, ProxyProtocol

logger = logging.getLogger(__name__)


class AccountService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ queries

    async def list_accounts(self) -> Sequence[Account]:
        result = await self.session.execute(
            select(Account).options(selectinload(Account.proxy)).order_by(Account.id)
        )
        return result.scalars().all()

    async def get_account(self, account_id: int) -> Optional[Account]:
        result = await self.session.execute(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(Account.id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_available_accounts(self, daily_limit: int) -> list[Account]:
        """
        Accounts eligible to receive work right now.

        Filters:
        * status == active
        * not spambot-restricted
        * flood_until expired / null
        * daily action counter under limit (auto-reset by UTC day)
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = await self.session.execute(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(Account.status == AccountStatus.active)
            .where(Account.is_spambot_restricted.is_(False))
            .where(Account.session_string != "")
            .order_by(Account.total_actions_today.asc(), Account.id.asc())
        )
        accounts = list(result.scalars().all())
        available: list[Account] = []
        now = datetime.now(timezone.utc)
        for acc in accounts:
            # Reset daily counter on new UTC day
            if acc.actions_day_key != today:
                acc.actions_day_key = today
                acc.total_actions_today = 0

            if acc.flood_until is not None:
                flood_until = acc.flood_until
                if flood_until.tzinfo is None:
                    flood_until = flood_until.replace(tzinfo=timezone.utc)
                if flood_until > now:
                    continue

            if acc.total_actions_today >= daily_limit:
                continue
            available.append(acc)
        return available

    # ------------------------------------------------------------------ mutations

    async def create_account(
        self,
        *,
        phone_number: str,
        session_string: str,
        api_id: int,
        api_hash: str,
        proxy_id: Optional[int] = None,
        status: AccountStatus = AccountStatus.paused,
        device_model: Optional[str] = None,
        system_version: Optional[str] = None,
        app_version: Optional[str] = None,
        lang_code: Optional[str] = None,
        display_name: Optional[str] = None,
        telegram_user_id: Optional[int] = None,
    ) -> Account:
        # Prefer organic fingerprints from TData/opentele when provided;
        # otherwise generate a stable random mobile profile.
        fp = generate_fingerprint()
        if proxy_id is not None:
            await self.heal_proxy_bindings()
            taken = (
                await self.session.execute(
                    select(Account.id).where(Account.proxy_id == proxy_id)
                )
            ).scalar_one_or_none()
            proxy = await self.session.get(Proxy, proxy_id)
            if taken is not None or (
                proxy is not None and proxy.assigned_account_id is not None
            ):
                logger.warning(
                    "Proxy #%s already bound — creating account without proxy",
                    proxy_id,
                )
                proxy_id = None

        def _build(pid: Optional[int]) -> Account:
            return Account(
                phone_number=phone_number,
                session_string=session_string,
                api_id=api_id,
                api_hash=api_hash,
                device_model=device_model or fp.device_model,
                system_version=system_version or fp.system_version,
                app_version=app_version or fp.app_version,
                lang_code=lang_code or fp.lang_code,
                proxy_id=pid,
                status=status,
                display_name=display_name,
                telegram_user_id=telegram_user_id,
                actions_day_key=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            )

        account = _build(proxy_id)
        self.session.add(account)
        try:
            await self.session.flush()
        except IntegrityError:
            if proxy_id is None:
                raise
            logger.warning(
                "proxy_id=%s UNIQUE conflict — retry insert without proxy",
                proxy_id,
            )
            await self.session.rollback()
            account = _build(None)
            self.session.add(account)
            await self.session.flush()

        if account.proxy_id is not None:
            proxy = await self.session.get(Proxy, account.proxy_id)
            if proxy is not None:
                proxy.assigned_account_id = account.id

        logger.info(
            "Created account #%s phone=%s device=%s proxy=%s",
            account.id,
            phone_number,
            account.device_model,
            account.proxy_id,
        )
        return account

    async def upsert_from_tdata(
        self,
        *,
        phone_number: str,
        session_string: str,
        api_id: int,
        api_hash: str,
        device_model: str,
        system_version: str,
        app_version: str,
        lang_code: str,
        proxy_id: Optional[int],
        display_name: Optional[str],
        telegram_user_id: Optional[int],
        status: AccountStatus = AccountStatus.active,
    ) -> Account:
        """Insert or refresh an account imported from TData ZIP."""
        existing = (
            await self.session.execute(
                select(Account).where(Account.phone_number == phone_number)
            )
        ).scalar_one_or_none()

        if existing is None and telegram_user_id is not None:
            existing = (
                await self.session.execute(
                    select(Account).where(Account.telegram_user_id == telegram_user_id)
                )
            ).scalar_one_or_none()

        if existing is None:
            return await self.create_account(
                phone_number=phone_number,
                session_string=session_string,
                api_id=api_id,
                api_hash=api_hash,
                proxy_id=proxy_id,
                status=status,
                device_model=device_model,
                system_version=system_version,
                app_version=app_version,
                lang_code=lang_code,
                display_name=display_name,
                telegram_user_id=telegram_user_id,
            )

        existing.session_string = session_string
        existing.api_id = api_id
        existing.api_hash = api_hash
        existing.device_model = device_model
        existing.system_version = system_version
        existing.app_version = app_version
        existing.lang_code = lang_code
        existing.status = status
        existing.display_name = display_name
        existing.telegram_user_id = telegram_user_id
        existing.last_error = None
        existing.flood_until = None
        existing.is_spambot_restricted = False
        if proxy_id is not None:
            taken = (
                await self.session.execute(
                    select(Account.id).where(
                        Account.proxy_id == proxy_id,
                        Account.id != existing.id,
                    )
                )
            ).scalar_one_or_none()
            proxy = await self.session.get(Proxy, proxy_id)
            if taken is not None or (
                proxy is not None
                and proxy.assigned_account_id is not None
                and proxy.assigned_account_id != existing.id
            ):
                logger.warning(
                    "Proxy #%s already bound — keeping account #%s proxy unchanged",
                    proxy_id,
                    existing.id,
                )
            else:
                existing.proxy_id = proxy_id
                if proxy is not None:
                    proxy.assigned_account_id = existing.id
        await self.session.flush()
        return existing

    async def create_proxy(
        self,
        *,
        ip: str,
        port: int,
        protocol: ProxyProtocol = ProxyProtocol.socks5,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> Proxy:
        proxy = Proxy(
            ip=ip,
            port=port,
            protocol=protocol,
            username=username,
            password=password,
        )
        self.session.add(proxy)
        await self.session.flush()
        return proxy

    async def used_proxy_ids(self) -> set[int]:
        """Proxy IDs currently referenced by Account.proxy_id (source of truth)."""
        rows = (
            await self.session.execute(
                select(Account.proxy_id).where(Account.proxy_id.is_not(None))
            )
        ).scalars().all()
        return {int(x) for x in rows if x is not None}

    async def heal_proxy_bindings(self) -> None:
        """
        Keep Proxy.assigned_account_id in sync with Account.proxy_id.

        Drift used to make pick_random_proxy return an already-taken proxy and
        blow up with UNIQUE(accounts.proxy_id).
        """
        accounts = list(
            (
                await self.session.execute(
                    select(Account).where(Account.proxy_id.is_not(None))
                )
            ).scalars().all()
        )
        by_proxy: dict[int, int] = {}
        for acc in accounts:
            if acc.proxy_id is None:
                continue
            by_proxy[int(acc.proxy_id)] = int(acc.id)

        proxies = list((await self.session.execute(select(Proxy))).scalars().all())
        for proxy in proxies:
            owner = by_proxy.get(int(proxy.id))
            if owner is not None:
                if proxy.assigned_account_id != owner:
                    proxy.assigned_account_id = owner
            elif proxy.assigned_account_id is not None:
                proxy.assigned_account_id = None
        await self.session.flush()

    async def pick_random_proxy(self, *, prefer_free: bool = True) -> Optional[Proxy]:
        """
        Pick a random alive proxy for account import.

        When prefer_free=True (default), never return a proxy already bound to an
        account — Account.proxy_id is UNIQUE.
        """
        import random

        await self.heal_proxy_bindings()
        used = await self.used_proxy_ids()

        free = list(
            (
                await self.session.execute(
                    select(Proxy).where(
                        Proxy.is_alive.is_(True),
                        Proxy.assigned_account_id.is_(None),
                    )
                )
            ).scalars().all()
        )
        free = [p for p in free if int(p.id) not in used]
        if free:
            return random.choice(free)
        if prefer_free:
            return None

        any_alive = list(
            (
                await self.session.execute(
                    select(Proxy).where(Proxy.is_alive.is_(True))
                )
            ).scalars().all()
        )
        any_alive = [p for p in any_alive if int(p.id) not in used]
        if any_alive:
            return random.choice(any_alive)
        return None

    async def get_or_create_proxy(
        self,
        *,
        ip: str,
        port: int,
        protocol: ProxyProtocol = ProxyProtocol.socks5,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> Proxy:
        """Reuse existing (ip, port, username) row — unique constraint safe."""
        stmt = select(Proxy).where(
            Proxy.ip == ip,
            Proxy.port == int(port),
        )
        if username is None:
            stmt = stmt.where(Proxy.username.is_(None))
        else:
            stmt = stmt.where(Proxy.username == username)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            # Refresh credentials / protocol if operator re-sent the line
            existing.password = password
            existing.protocol = protocol
            existing.is_alive = True
            await self.session.flush()
            return existing
        return await self.create_proxy(
            ip=ip,
            port=port,
            protocol=protocol,
            username=username,
            password=password,
        )

    async def bind_proxy(self, account_id: int, proxy_id: int) -> Account:
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account #{account_id} not found")
        proxy = await self.session.get(Proxy, proxy_id)
        if proxy is None:
            raise ValueError(f"Proxy #{proxy_id} not found")

        # Unbind previous owner of this proxy
        if proxy.assigned_account_id and proxy.assigned_account_id != account_id:
            prev = await self.get_account(proxy.assigned_account_id)
            if prev is not None:
                prev.proxy_id = None

        account.proxy_id = proxy.id
        proxy.assigned_account_id = account.id
        await self.session.flush()
        return account

    async def set_status(
        self,
        account_id: int,
        status: AccountStatus,
        *,
        last_error: Optional[str] = None,
        flood_until: Optional[datetime] = None,
        is_spambot_restricted: Optional[bool] = None,
    ) -> None:
        values: dict = {
            "status": status,
            "last_error": last_error,
            "flood_until": flood_until,
        }
        if is_spambot_restricted is not None:
            values["is_spambot_restricted"] = is_spambot_restricted
        await self.session.execute(
            update(Account).where(Account.id == account_id).values(**values)
        )

    async def persist_runtime_state(self, account: Account) -> None:
        """Write back in-memory status fields mutated by SessionWrapper."""
        await self.session.execute(
            update(Account)
            .where(Account.id == account.id)
            .values(
                status=account.status,
                flood_until=account.flood_until,
                is_spambot_restricted=account.is_spambot_restricted,
                last_error=account.last_error,
                total_actions_today=account.total_actions_today,
                actions_day_key=account.actions_day_key,
                telegram_user_id=account.telegram_user_id,
                display_name=account.display_name,
            )
        )

    async def increment_actions(self, account_id: int) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        account = await self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account #{account_id} not found")
        if account.actions_day_key != today:
            account.actions_day_key = today
            account.total_actions_today = 0
        account.total_actions_today += 1
        await self.session.flush()
        return account.total_actions_today
