"""
UserbotManager — session validation facade over ListenerManager / SessionWrapper.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tg_pool.clients.session_wrapper import SessionWrapper
from tg_pool.db.models import Account, AccountStatus
from tg_pool.db.session import session_scope

if TYPE_CHECKING:
    from tg_pool.services.listener_manager import ListenerManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentValidation:
    agent_id: int
    ok: bool
    phone: str
    status: str
    detail: str
    flood_remaining_sec: int = 0


class UserbotManager:
    """
    Validates Telethon sessions without messaging public chats (`get_me` only).
    """

    def __init__(self, listeners: Optional["ListenerManager"] = None) -> None:
        self.listeners = listeners

    def bind_listeners(self, listeners: "ListenerManager") -> None:
        self.listeners = listeners

    async def validate_agent_session(self, agent_id: int) -> AgentValidation:
        async with session_scope() as session:
            account = (
                await session.execute(
                    select(Account)
                    .options(selectinload(Account.proxy))
                    .where(Account.id == agent_id)
                )
            ).scalar_one_or_none()
            if account is None:
                return AgentValidation(
                    agent_id=agent_id,
                    ok=False,
                    phone="?",
                    status="missing",
                    detail="Account not found",
                )
            phone = account.phone_number
            status = (
                account.status.value
                if hasattr(account.status, "value")
                else str(account.status)
            )
            flood_left = 0
            if account.flood_until is not None:
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
                fu = account.flood_until
                if fu.tzinfo is None:
                    fu = fu.replace(tzinfo=timezone.utc)
                flood_left = max(0, int((fu - now).total_seconds()))
            proxy = account.proxy

            if account.status == AccountStatus.banned:
                return AgentValidation(
                    agent_id=agent_id,
                    ok=False,
                    phone=phone,
                    status=status,
                    detail="Account banned",
                    flood_remaining_sec=flood_left,
                )
            if flood_left > 0 or account.status == AccountStatus.flood_wait:
                return AgentValidation(
                    agent_id=agent_id,
                    ok=False,
                    phone=phone,
                    status=status,
                    detail=f"FloodWait: {flood_left}s remaining",
                    flood_remaining_sec=flood_left,
                )

        wrapper: Optional[SessionWrapper] = None
        if self.listeners is not None:
            wrapper = self.listeners.get_wrapper(agent_id)

        created = False
        try:
            if wrapper is None:
                wrapper = SessionWrapper(account, proxy=proxy)
                created = True
                await wrapper.connect()
            assert wrapper.client is not None
            me = await wrapper.client.get_me()
            if me is None:
                return AgentValidation(
                    agent_id=agent_id,
                    ok=False,
                    phone=phone,
                    status=status,
                    detail="get_me() returned empty",
                    flood_remaining_sec=flood_left,
                )
            uname = getattr(me, "username", None) or me.id
            return AgentValidation(
                agent_id=agent_id,
                ok=True,
                phone=phone,
                status=status,
                detail=f"get_me ok ({uname})",
                flood_remaining_sec=flood_left,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("validate_agent_session #%s failed: %s", agent_id, exc)
            return AgentValidation(
                agent_id=agent_id,
                ok=False,
                phone=phone,
                status=status,
                detail=f"{type(exc).__name__}: {exc}",
                flood_remaining_sec=flood_left,
            )
        finally:
            if created and wrapper is not None:
                try:
                    await wrapper.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    async def list_agent_ids(self) -> list[int]:
        async with session_scope() as session:
            rows = (
                await session.execute(select(Account.id).order_by(Account.id))
            ).scalars().all()
            return [int(x) for x in rows]
