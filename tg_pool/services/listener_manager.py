"""Telethon NewMessage listeners for draft-engine trigger monitoring."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Dict, Optional

from sqlalchemy import select
from telethon import events

from tg_pool.clients.session_wrapper import SessionWrapper
from tg_pool.db.models import Account, AccountStatus
from tg_pool.db.session import session_scope
from tg_pool.services.draft_engine import DraftEngine

if TYPE_CHECKING:
    from aiogram import Bot
    from redis.asyncio import Redis

    from tg_pool.config import Settings
    from tg_pool.services.alerts import AlertService

logger = logging.getLogger(__name__)


class ListenerManager:
    """Connects assistant-enabled accounts and routes matches to DraftEngine."""

    def __init__(
        self,
        settings: "Settings",
        redis: "Redis",
        *,
        alert_service: Optional["AlertService"] = None,
        bot: Optional["Bot"] = None,
    ) -> None:
        self.settings = settings
        self.engine = DraftEngine(
            settings,
            redis,
            alert_service=alert_service,
            bot=bot,
        )
        self._wrappers: Dict[int, SessionWrapper] = {}
        self._handlers: Dict[int, object] = {}
        self._lock = asyncio.Lock()

    def bind_bot(self, bot: "Bot") -> None:
        self.engine.bind_bot(bot)

    async def start(self) -> None:
        async with session_scope() as session:
            result = await session.execute(
                select(Account).where(
                    Account.assistant_enabled.is_(True),
                    Account.status == AccountStatus.active,
                )
            )
            account_ids = [a.id for a in result.scalars().all()]

        for account_id in account_ids:
            try:
                await self.attach_account(account_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to attach listener for account %s: %s", account_id, exc)

        logger.info("ListenerManager started with %s account(s)", len(self._wrappers))

    async def stop(self) -> None:
        async with self._lock:
            for account_id in list(self._wrappers.keys()):
                await self._detach_unlocked(account_id)
        logger.info("ListenerManager stopped")

    async def attach_account(self, account_id: int) -> None:
        async with self._lock:
            if account_id in self._wrappers:
                return

            async with session_scope() as session:
                account = (
                    await session.execute(select(Account).where(Account.id == account_id))
                ).scalar_one_or_none()
                if account is None:
                    raise ValueError(f"Account {account_id} not found")
                if not account.assistant_enabled:
                    logger.info("Account %s assistant disabled — skip attach", account_id)
                    return
                if account.status != AccountStatus.active:
                    logger.info("Account %s not ACTIVE — skip attach", account_id)
                    return
                # Eager-load proxy for sticky session
                _ = account.proxy
                wrapper = SessionWrapper(
                    account,
                    proxy=account.proxy,
                    settings=self.settings,
                )

            client = await wrapper.connect()
            handler = self._make_handler(account_id)
            client.add_event_handler(handler, events.NewMessage(incoming=True))
            self._wrappers[account_id] = wrapper
            self._handlers[account_id] = handler
            logger.info("Listener attached for account %s", account_id)

    async def detach_account(self, account_id: int) -> None:
        async with self._lock:
            await self._detach_unlocked(account_id)

    async def _detach_unlocked(self, account_id: int) -> None:
        wrapper = self._wrappers.pop(account_id, None)
        handler = self._handlers.pop(account_id, None)
        if wrapper is None:
            return
        try:
            if handler is not None and wrapper.client is not None:
                wrapper.client.remove_event_handler(handler)
        except Exception as exc:  # noqa: BLE001
            logger.debug("remove_event_handler failed: %s", exc)
        await wrapper.disconnect()
        logger.info("Listener detached for account %s", account_id)

    async def refresh_account(self, account_id: int) -> None:
        """Re-attach or detach based on current DB flags."""
        async with session_scope() as session:
            account = (
                await session.execute(select(Account).where(Account.id == account_id))
            ).scalar_one_or_none()
            enabled = bool(
                account
                and account.assistant_enabled
                and account.status == AccountStatus.active
            )
        if enabled:
            if account_id not in self._wrappers:
                await self.attach_account(account_id)
        else:
            await self.detach_account(account_id)

    def _make_handler(self, account_id: int):
        engine = self.engine

        async def _on_new_message(event: events.NewMessage.Event) -> None:
            try:
                if event.out:
                    return
                message = event.message
                if message is None or not (message.message or "").strip():
                    return

                chat = await event.get_chat()
                chat_id = int(getattr(chat, "id", 0) or 0)
                if chat_id == 0:
                    return

                # Skip 1:1 private user dialogs (keep groups / megagroups / channels)
                title = getattr(chat, "title", None)
                if title is None and not getattr(chat, "megagroup", False) and not getattr(
                    chat, "broadcast", False
                ):
                    return

                chat_title = title or getattr(chat, "username", None) or str(chat_id)
                sender = await event.get_sender()
                username = getattr(sender, "username", None) if sender else None
                sender_id = int(getattr(sender, "id", 0) or 0) if sender else 0

                await engine.handle_incoming_message(
                    account_id=account_id,
                    chat_id=chat_id,
                    chat_title=str(chat_title),
                    message_id=int(message.id),
                    sender_id=sender_id,
                    sender_username=username,
                    text=message.message or "",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Listener handler error account=%s: %s", account_id, exc)

        return _on_new_message

    def get_wrapper(self, account_id: int) -> Optional[SessionWrapper]:
        return self._wrappers.get(account_id)
