"""
Telethon session wrapper with anti-spam / flood-safe primitives.

Why Telethon (not Pyrogram) for multi-session pools
-------------------------------------------------
* Mature StringSession format and explicit FloodWaitError taxonomy
* Fine-grained client kwargs (device_model / system_version / app_version / lang_code)
* Battle-tested concurrent client pools without shared global state

Critical safety rules enforced here
-----------------------------------
1. Sticky proxy — never swap proxy under a live authorized session
2. Sticky fingerprint — device params come from DB, never regenerated on connect
3. Jitter before side-effecting actions
4. FloodWait → persist flood_until + status, notify, do NOT crash the pool
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    FloodWaitError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession

from tg_pool.config import Settings, get_settings
from tg_pool.db.models import Account, AccountStatus, Proxy

logger = logging.getLogger(__name__)

# Callback: async (account_id, phone, level, message) -> None
AlertCallback = Callable[[int, str, str, str], Awaitable[None]]


class SessionWrapper:
    """Owns one Telethon client bound to a single Account row."""

    def __init__(
        self,
        account: Account,
        proxy: Optional[Proxy] = None,
        *,
        settings: Optional[Settings] = None,
        on_alert: Optional[AlertCallback] = None,
    ) -> None:
        self.account = account
        self.proxy = proxy
        self.settings = settings or get_settings()
        self.on_alert = on_alert
        self.client: Optional[TelegramClient] = None
        self._connected = False

    # ------------------------------------------------------------------ lifecycle

    def _build_client(self) -> TelegramClient:
        """
        Construct Telethon client with persisted fingerprint + sticky proxy.

        Changing device_model / proxy after authorization is a frequent cause
        of AuthKeyDuplicatedError and sudden session drops — do not do it.
        """
        proxy_tuple = None
        if self.proxy is not None:
            try:
                proxy_tuple = self.proxy.as_telethon_tuple()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Invalid proxy for account #{self.account.id}: {exc}"
                ) from exc

        return TelegramClient(
            StringSession(self.account.session_string),
            self.account.api_id,
            self.account.api_hash,
            proxy=proxy_tuple,
            device_model=self.account.device_model,
            system_version=self.account.system_version,
            app_version=self.account.app_version,
            lang_code=self.account.lang_code or "ru",
            system_lang_code=self.account.lang_code or "ru",
            connection_retries=2,
            retry_delay=1,
            auto_reconnect=True,
        )

    async def connect(self) -> TelegramClient:
        if self.client is not None and self._connected:
            return self.client

        if not self.account.session_string:
            raise RuntimeError(f"Account #{self.account.id} has empty session_string")

        self.client = self._build_client()
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.disconnect()
            raise RuntimeError(f"Account #{self.account.id} session is not authorized")

        self._connected = True
        me = await self.client.get_me()
        logger.info(
            "Account #%s online as %s (proxy=%s device=%s)",
            self.account.id,
            getattr(me, "username", None) or me.id,
            bool(self.proxy),
            self.account.device_model,
        )
        return self.client

    async def disconnect(self) -> None:
        client = self.client
        self.client = None
        self._connected = False
        if client is None:
            return
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:  # noqa: BLE001
            logger.debug("disconnect error account=%s", self.account.id, exc_info=True)

    # ------------------------------------------------------------------ jitter

    async def jitter(self, min_sec: float | None = None, max_sec: float | None = None) -> float:
        """Random human-like pause before a side-effecting Telegram call."""
        lo = self.settings.jitter_min_sec if min_sec is None else min_sec
        hi = self.settings.jitter_max_sec if max_sec is None else max_sec
        if hi < lo:
            lo, hi = hi, lo
        delay = random.uniform(lo, hi)
        logger.debug("Account #%s jitter %.2fs", self.account.id, delay)
        await asyncio.sleep(delay)
        return delay

    # ------------------------------------------------------------------ safe execute

    async def execute(self, coro_factory: Callable[[TelegramClient], Awaitable[Any]]) -> Any:
        """
        Run a Telegram API call with jitter + FloodWait / ban interception.

        `coro_factory` receives a connected client and returns an awaitable.
        On FloodWait / deactivation this method:
          * updates in-memory account fields (caller persists to DB)
          * fires alert callback
          * re-raises FloodWaitError / FatalSessionError for the task router
        """
        client = await self.connect()
        await self.jitter()
        try:
            return await coro_factory(client)
        except FloodWaitError as exc:
            await self._handle_flood_wait(exc)
            raise
        except (UserDeactivatedError, UserDeactivatedBanError, AuthKeyDuplicatedError) as exc:
            await self._handle_fatal(exc)
            raise FatalSessionError(str(exc)) from exc

    async def _handle_flood_wait(self, exc: FloodWaitError) -> None:
        """
        Persist flood window on the Account.

        Telegram's `seconds` is authoritative. We add a small safety buffer
        so we do not hammer the API the instant the window opens.
        """
        buffer = 15
        wait_for = int(exc.seconds) + buffer
        until = datetime.now(timezone.utc) + timedelta(seconds=wait_for)
        self.account.status = AccountStatus.flood_wait
        self.account.flood_until = until
        self.account.last_error = f"FloodWaitError: {wait_for}s (until {until.isoformat()})"
        logger.warning(
            "Account #%s FloodWait %ss → flood_until=%s",
            self.account.id,
            wait_for,
            until.isoformat(),
        )
        level = "critical" if wait_for >= self.settings.flood_alert_threshold_sec else "warning"
        await self._alert(
            level,
            f"🟡 FloodWait {wait_for}s\nuntil={until.isoformat()}",
        )

    async def _handle_fatal(self, exc: BaseException) -> None:
        self.account.status = AccountStatus.banned
        self.account.last_error = f"{type(exc).__name__}: {exc}"
        logger.error("Account #%s FATAL: %s", self.account.id, self.account.last_error)
        await self._alert("critical", f"🔴 BAN / session dead\n{self.account.last_error}")
        await self.disconnect()

    async def _alert(self, level: str, message: str) -> None:
        if self.on_alert is None:
            return
        try:
            await self.on_alert(
                self.account.id,
                self.account.phone_number,
                level,
                message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Alert callback failed for account #%s", self.account.id)


class FatalSessionError(RuntimeError):
    """Session is permanently unusable (banned / auth key duplicated)."""
