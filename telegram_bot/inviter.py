"""
Inviter / DM Spammer module.

Manages a pool of Telegram user accounts (Telethon sessions) and uses them
in round-robin fashion to either:
  1. Invite parsed users to a target channel/group.
  2. Send them a personalised DM with an inline button linking to a private channel.

Anti-ban measures implemented:
  - Per-account daily caps on invites and DMs.
  - Randomised human-like delays between every action.
  - Graceful FloodWait handling with back-off.
  - Automatic account rotation on errors.
  - Account health monitoring — mark as banned after repeated failures.
  - Proxy assignment per account.
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    RPCError,
    UserBlockedError,
    UserBotError,
    UserChannelsTooMuchError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserIdInvalidError,
    UserIsBlockedError,
    UserKickedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import (
    InputPeerUser,
    KeyboardButtonUrl,
    ReplyInlineMarkup,
    KeyboardButtonRow,
)

from config import AppConfig
from database import Database
from utils import human_delay, normalise_channel_ref, proxy_to_telethon

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Account wrapper
# ---------------------------------------------------------------------------

class AccountSession:
    """Wraps a Telethon TelegramClient tied to one session file."""

    def __init__(
        self,
        session_name: str,
        api_id: int,
        api_hash: str,
        sessions_dir: str,
        proxy: Optional[Dict] = None,
    ):
        self.session_name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        self.sessions_dir = sessions_dir
        self.proxy = proxy

        self._client: Optional[TelegramClient] = None
        self.is_connected = False
        self.invites_today = 0
        self.dms_today = 0
        self.errors_streak = 0
        self.is_banned = False

    def _build_client(self) -> TelegramClient:
        session_path = str(Path(self.sessions_dir) / self.session_name)
        proxy_tuple = proxy_to_telethon(self.proxy) if self.proxy else None
        client = TelegramClient(
            session=session_path,
            api_id=self.api_id,
            api_hash=self.api_hash,
            proxy=proxy_tuple,
            connection_retries=5,
            retry_delay=3,
            timeout=20,
            request_retries=3,
        )
        return client

    async def connect(self) -> bool:
        """Open the Telethon connection (no login — session file must exist)."""
        if self.is_connected:
            return True
        try:
            self._client = self._build_client()
            await self._client.connect()
            if not await self._client.is_user_authorized():
                logger.warning("Session %s is not authorised", self.session_name)
                await self._client.disconnect()
                return False
            self.is_connected = True
            me = await self._client.get_me()
            logger.info(
                "Account connected: %s (@%s)",
                self.session_name,
                getattr(me, "username", "?"),
            )
            return True
        except Exception as exc:
            logger.error("Cannot connect session %s: %s", self.session_name, exc)
            self.is_connected = False
            return False

    async def disconnect(self) -> None:
        if self._client and self.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self.is_connected = False

    @property
    def client(self) -> Optional[TelegramClient]:
        return self._client

    def __repr__(self) -> str:
        return f"<AccountSession {self.session_name} connected={self.is_connected}>"


# ---------------------------------------------------------------------------
# Account Manager
# ---------------------------------------------------------------------------

class AccountManager:
    """
    Manages the pool of AccountSession objects.
    Persists metadata to DB; session files live on disk.
    """

    def __init__(self, db: Database, cfg: AppConfig):
        self.db = db
        self.cfg = cfg
        self.acc_cfg = cfg.accounts

        self._sessions: List[AccountSession] = []
        self._index = 0  # round-robin pointer

    async def load(self) -> int:
        """Discover session files on disk and connect them. Returns count of connected accounts."""
        sessions_dir = Path(self.acc_cfg.sessions_dir)
        sessions_dir.mkdir(parents=True, exist_ok=True)

        session_files = list(sessions_dir.glob("*.session"))
        if not session_files:
            logger.warning("No .session files found in %s", sessions_dir)
            return 0

        proxies = await self.db.get_active_proxies()
        proxy_cycle = proxies if proxies else [None]
        proxy_idx = 0

        connected = 0
        for sf in session_files:
            session_name = sf.stem
            db_account = await self.db.get_account(session_name)

            if db_account and db_account.get("is_banned"):
                logger.info("Skipping banned account: %s", session_name)
                continue

            proxy = proxy_cycle[proxy_idx % len(proxy_cycle)] if proxy_cycle[0] else None
            proxy_idx += 1

            acc = AccountSession(
                session_name=session_name,
                api_id=self.cfg.telegram.api_id,
                api_hash=self.cfg.telegram.api_hash,
                sessions_dir=str(sessions_dir),
                proxy=proxy,
            )

            if db_account:
                acc.invites_today = db_account.get("invites_today", 0)
                acc.dms_today = db_account.get("dms_today", 0)
                acc.errors_streak = db_account.get("errors_streak", 0)

            if await acc.connect():
                self._sessions.append(acc)
                await self.db.upsert_account(session_name)
                connected += 1
            else:
                logger.warning("Failed to connect: %s", session_name)

        logger.info("AccountManager: %d/%d sessions connected", connected, len(session_files))
        return connected

    async def disconnect_all(self) -> None:
        for acc in self._sessions:
            await acc.disconnect()
        self._sessions.clear()

    def get_next_available(
        self, action_type: str, max_invites: int, max_dms: int
    ) -> Optional[AccountSession]:
        """
        Round-robin through sessions; return the first one that hasn't
        hit its daily cap for the given action_type.
        """
        if not self._sessions:
            return None

        start = self._index
        attempts = 0
        while attempts < len(self._sessions):
            idx = (start + attempts) % len(self._sessions)
            acc = self._sessions[idx]

            if acc.is_banned or not acc.is_connected:
                attempts += 1
                continue

            if acc.errors_streak >= self.cfg.inviter.max_consecutive_errors:
                attempts += 1
                continue

            if action_type == "invite" and acc.invites_today >= max_invites:
                attempts += 1
                continue

            if action_type == "dm" and acc.dms_today >= max_dms:
                attempts += 1
                continue

            self._index = (idx + 1) % len(self._sessions)
            return acc

        return None

    async def reset_daily_counters(self) -> None:
        """Reset per-account daily counters (call at midnight)."""
        for acc in self._sessions:
            acc.invites_today = 0
            acc.dms_today = 0
        await self.db.reset_daily_counters()
        logger.info("AccountManager: daily counters reset")

    def active_count(self) -> int:
        return sum(1 for a in self._sessions if a.is_connected and not a.is_banned)

    def banned_count(self) -> int:
        return sum(1 for a in self._sessions if a.is_banned)


# ---------------------------------------------------------------------------
# Inviter
# ---------------------------------------------------------------------------

class Inviter:
    """
    Reads "new" users from the database and either:
      - Invites them to self.cfg.inviter.invite_target channel, or
      - Sends them a personalised DM.

    Uses AccountManager for account rotation.
    """

    def __init__(self, account_manager: AccountManager, db: Database, cfg: AppConfig):
        self.account_manager = account_manager
        self.db = db
        self.cfg = cfg
        self.inv_cfg = cfg.inviter

        self._running = False
        self._invite_task: Optional[asyncio.Task] = None
        self._dm_task: Optional[asyncio.Task] = None
        self._mode = "invite"  # 'invite' | 'dm' | 'both'
        self._actions_session = 0

    # ------------------------------------------------------------------
    # Public control interface
    # ------------------------------------------------------------------

    async def start(self, mode: str = "invite") -> None:
        """
        Start the inviter loop.
        mode: 'invite' | 'dm' | 'both'
        """
        if self._running:
            logger.warning("Inviter already running")
            return
        self._mode = mode
        self._running = True

        if mode in ("invite", "both"):
            self._invite_task = asyncio.create_task(
                self._invite_loop(), name="inviter_invite_loop"
            )
        if mode in ("dm", "both"):
            self._dm_task = asyncio.create_task(
                self._dm_loop(), name="inviter_dm_loop"
            )

        logger.info("Inviter started in mode: %s", mode)

    async def stop(self) -> None:
        """Stop the inviter loop gracefully."""
        self._running = False
        for task in (self._invite_task, self._dm_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._invite_task = None
        self._dm_task = None
        logger.info("Inviter stopped")

    def is_running(self) -> bool:
        tasks = [self._invite_task, self._dm_task]
        return self._running and any(t and not t.done() for t in tasks)

    def get_status(self) -> Dict:
        return {
            "running": self.is_running(),
            "mode": self._mode,
            "actions_session": self._actions_session,
            "active_accounts": self.account_manager.active_count(),
            "banned_accounts": self.account_manager.banned_count(),
        }

    # ------------------------------------------------------------------
    # Internal: invite loop
    # ------------------------------------------------------------------

    async def _invite_loop(self) -> None:
        logger.info("Invite loop started")
        while self._running:
            try:
                users = await self.db.get_new_users(limit=50)
                if not users:
                    logger.debug("No new users to invite — sleeping 60s")
                    await asyncio.sleep(60)
                    continue

                for user in users:
                    if not self._running:
                        break
                    await self._process_invite(user)
                    await human_delay(
                        self.inv_cfg.invite_delay, jitter_ratio=0.4
                    )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Invite loop error: %s", exc, exc_info=True)
                await asyncio.sleep(30)

        logger.info("Invite loop exited")

    async def _process_invite(self, user: Dict) -> None:
        """Attempt to invite a single user to the target channel."""
        account = self.account_manager.get_next_available(
            "invite",
            self.inv_cfg.max_invites_per_account,
            self.inv_cfg.max_dm_per_account,
        )
        if account is None:
            logger.warning("No available accounts for inviting — pausing %ds",
                           self.inv_cfg.error_pause_seconds)
            await asyncio.sleep(self.inv_cfg.error_pause_seconds)
            return

        user_id = user["user_id"]
        username = user.get("username")
        target = normalise_channel_ref(self.inv_cfg.invite_target)

        try:
            target_entity = await account.client.get_entity(target)
            user_entity = await account.client.get_entity(user_id)

            await account.client(
                InviteToChannelRequest(channel=target_entity, users=[user_entity])
            )

            account.invites_today += 1
            self._actions_session += 1
            account.errors_streak = 0

            await self.db.increment_account_invites(account.session_name)
            await self.db.reset_account_errors(account.session_name)
            await self.db.record_action(
                user_id=user_id,
                username=username,
                action_type="invite",
                account_session=account.session_name,
                status="sent",
            )
            await self.db.mark_user_status(user_id, "invited")

            logger.info(
                "Invited user %s (@%s) via %s",
                user_id, username or "?", account.session_name,
            )

        except FloodWaitError as exc:
            logger.warning(
                "FloodWait inviting %s via %s: %ds",
                user_id, account.session_name, exc.seconds,
            )
            await asyncio.sleep(exc.seconds + 5)

        except (UserPrivacyRestrictedError, UserChannelsTooMuchError,
                UserNotMutualContactError, UserKickedError) as exc:
            logger.debug("Cannot invite %s (%s): %s", user_id, username, type(exc).__name__)
            await self.db.record_action(user_id, username, "invite",
                                        account.session_name, "failed", str(exc))
            await self.db.mark_user_status(user_id, "skipped")

        except (UserDeactivatedBanError, UserDeactivatedError, InputUserDeactivatedError):
            logger.info("User %s deactivated/deleted, marking skipped", user_id)
            await self.db.record_action(user_id, username, "invite",
                                        account.session_name, "failed", "deactivated")
            await self.db.mark_user_status(user_id, "skipped")

        except PeerFloodError:
            logger.warning("PeerFlood on account %s — pausing", account.session_name)
            account.errors_streak += 1
            await self.db.increment_account_error(account.session_name)
            await asyncio.sleep(self.inv_cfg.error_pause_seconds)

        except RPCError as exc:
            logger.error("RPC error inviting %s via %s: %s", user_id, account.session_name, exc)
            streak = await self.db.increment_account_error(account.session_name)
            account.errors_streak = streak
            if streak >= self.inv_cfg.max_consecutive_errors:
                logger.warning("Disabling account %s after %d errors", account.session_name, streak)
                account.is_banned = True
                await self.db.ban_account(account.session_name)

        except Exception as exc:
            logger.error("Unexpected error inviting %s: %s", user_id, exc, exc_info=True)

    # ------------------------------------------------------------------
    # Internal: DM loop
    # ------------------------------------------------------------------

    async def _dm_loop(self) -> None:
        logger.info("DM loop started")
        while self._running:
            try:
                users = await self.db.get_new_users(limit=30)
                if not users:
                    logger.debug("No new users to DM — sleeping 60s")
                    await asyncio.sleep(60)
                    continue

                for user in users:
                    if not self._running:
                        break
                    # Skip users already invited (avoid double-action)
                    if await self.db.was_user_contacted(user["user_id"], "invite"):
                        continue
                    await self._process_dm(user)
                    await human_delay(
                        self.inv_cfg.invite_delay * 1.5, jitter_ratio=0.4
                    )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("DM loop error: %s", exc, exc_info=True)
                await asyncio.sleep(30)

        logger.info("DM loop exited")

    async def _process_dm(self, user: Dict) -> None:
        """Send a personalised DM to a single user."""
        account = self.account_manager.get_next_available(
            "dm",
            self.inv_cfg.max_invites_per_account,
            self.inv_cfg.max_dm_per_account,
        )
        if account is None:
            logger.warning("No available accounts for DM — pausing %ds",
                           self.inv_cfg.error_pause_seconds)
            await asyncio.sleep(self.inv_cfg.error_pause_seconds)
            return

        user_id = user["user_id"]
        username = user.get("username")
        first_name = user.get("first_name") or "друг"

        # Build message text
        link = self.inv_cfg.dm_button_url or self.inv_cfg.invite_target
        text = self.inv_cfg.dm_message.format(
            username=username or "",
            first_name=first_name,
            link=link,
        )

        # Build inline button
        buttons = None
        if self.inv_cfg.dm_button_url:
            buttons = ReplyInlineMarkup(rows=[
                KeyboardButtonRow(buttons=[
                    KeyboardButtonUrl(
                        text=self.inv_cfg.dm_button_text,
                        url=self.inv_cfg.dm_button_url,
                    )
                ])
            ])

        try:
            await account.client.send_message(
                entity=user_id,
                message=text,
                buttons=buttons,
                parse_mode="md",
            )

            account.dms_today += 1
            self._actions_session += 1
            account.errors_streak = 0

            await self.db.increment_account_dms(account.session_name)
            await self.db.reset_account_errors(account.session_name)
            await self.db.record_action(
                user_id=user_id,
                username=username,
                action_type="dm",
                account_session=account.session_name,
                status="sent",
            )
            await self.db.mark_user_status(user_id, "dm_sent")

            logger.info(
                "DM sent to user %s (@%s) via %s",
                user_id, username or "?", account.session_name,
            )

        except FloodWaitError as exc:
            logger.warning(
                "FloodWait sending DM to %s via %s: %ds",
                user_id, account.session_name, exc.seconds,
            )
            await asyncio.sleep(exc.seconds + 5)

        except (UserBlockedError, UserIsBlockedError):
            logger.debug("User %s has blocked DMs", user_id)
            await self.db.record_action(user_id, username, "dm",
                                        account.session_name, "blocked")
            await self.db.mark_user_status(user_id, "skipped")

        except (UserPrivacyRestrictedError,):
            logger.debug("Privacy restricted: %s", user_id)
            await self.db.record_action(user_id, username, "dm",
                                        account.session_name, "failed", "privacy")
            await self.db.mark_user_status(user_id, "skipped")

        except (UserDeactivatedBanError, UserDeactivatedError, InputUserDeactivatedError):
            await self.db.record_action(user_id, username, "dm",
                                        account.session_name, "failed", "deactivated")
            await self.db.mark_user_status(user_id, "skipped")

        except PeerFloodError:
            logger.warning("PeerFlood (DM) on account %s — pausing", account.session_name)
            account.errors_streak += 1
            await self.db.increment_account_error(account.session_name)
            await asyncio.sleep(self.inv_cfg.error_pause_seconds)

        except ChatWriteForbiddenError:
            logger.debug("Cannot write to chat for user %s", user_id)
            await self.db.record_action(user_id, username, "dm",
                                        account.session_name, "failed", "write_forbidden")
            await self.db.mark_user_status(user_id, "skipped")

        except RPCError as exc:
            logger.error("RPC error sending DM to %s via %s: %s", user_id, account.session_name, exc)
            streak = await self.db.increment_account_error(account.session_name)
            account.errors_streak = streak
            if streak >= self.inv_cfg.max_consecutive_errors:
                logger.warning("Disabling account %s after %d DM errors", account.session_name, streak)
                account.is_banned = True
                await self.db.ban_account(account.session_name)

        except Exception as exc:
            logger.error("Unexpected DM error for %s: %s", user_id, exc, exc_info=True)

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------

    async def run_once(self, action_type: str = "invite", limit: int = 10) -> int:
        """
        Manually process up to `limit` users with the specified action_type.
        Returns number of actions performed.
        """
        users = await self.db.get_new_users(limit=limit)
        count = 0
        for user in users:
            if not self._running:
                break
            if action_type == "invite":
                await self._process_invite(user)
            else:
                await self._process_dm(user)
            count += 1
            await human_delay(self.inv_cfg.invite_delay, jitter_ratio=0.3)
        return count
