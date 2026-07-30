"""Invite-code access control for the panel bot."""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_pool.config import CREATOR_TELEGRAM_ID
from tg_pool.db.models import InviteCode, PanelUser, UserRole

logger = logging.getLogger(__name__)


def generate_invite_code() -> str:
    """Format: ALT-XXXX-YYYY (cryptographically random alnum)."""
    alphabet = string.ascii_uppercase + string.digits
    left = "".join(secrets.choice(alphabet) for _ in range(4))
    right = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"ALT-{left}-{right}"


class AccessService:
    def __init__(self, session: AsyncSession, *, creator_id: int = CREATOR_TELEGRAM_ID) -> None:
        self.session = session
        self.creator_id = creator_id

    async def ensure_user(
        self,
        telegram_id: int,
        *,
        username: Optional[str] = None,
        full_name: Optional[str] = None,
    ) -> PanelUser:
        user = await self.session.get(PanelUser, telegram_id)
        if user is None:
            is_creator = telegram_id == self.creator_id
            user = PanelUser(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                role=UserRole.creator if is_creator else UserRole.user,
                is_approved=is_creator,
            )
            self.session.add(user)
            await self.session.flush()
            logger.info(
                "Registered panel user %s creator=%s",
                telegram_id,
                is_creator,
            )
            return user

        # Keep profile fields fresh
        if username is not None:
            user.username = username
        if full_name is not None:
            user.full_name = full_name

        # Creator always retains full access
        if telegram_id == self.creator_id:
            user.role = UserRole.creator
            user.is_approved = True

        await self.session.flush()
        return user

    async def get_user(self, telegram_id: int) -> Optional[PanelUser]:
        return await self.session.get(PanelUser, telegram_id)

    def has_access(self, user: Optional[PanelUser], telegram_id: int) -> bool:
        if telegram_id == self.creator_id:
            return True
        return bool(user and user.is_approved)

    def is_creator(self, telegram_id: int) -> bool:
        return telegram_id == self.creator_id

    async def create_invite(self, created_by: int) -> InviteCode:
        # Rare collision retry
        for _ in range(5):
            code = generate_invite_code()
            exists = (
                await self.session.execute(
                    select(InviteCode).where(InviteCode.code == code)
                )
            ).scalar_one_or_none()
            if exists is None:
                invite = InviteCode(
                    code=code,
                    created_by=created_by,
                    is_used=False,
                )
                self.session.add(invite)
                await self.session.flush()
                return invite
        raise RuntimeError("Failed to generate unique invite code")

    async def list_invites(self, *, only_active: Optional[bool] = None) -> Sequence[InviteCode]:
        stmt = select(InviteCode).order_by(InviteCode.id.desc())
        if only_active is True:
            stmt = stmt.where(InviteCode.is_used.is_(False))
        elif only_active is False:
            stmt = stmt.where(InviteCode.is_used.is_(True))
        result = await self.session.execute(stmt.limit(50))
        return result.scalars().all()

    async def redeem_invite(
        self,
        code: str,
        telegram_id: int,
        *,
        username: Optional[str] = None,
        full_name: Optional[str] = None,
    ) -> PanelUser:
        normalized = code.strip().upper()
        invite = (
            await self.session.execute(
                select(InviteCode).where(InviteCode.code == normalized)
            )
        ).scalar_one_or_none()
        if invite is None:
            raise ValueError("Код не найден")
        if invite.is_used:
            raise ValueError("Код уже использован")

        user = await self.ensure_user(
            telegram_id,
            username=username,
            full_name=full_name,
        )
        if user.is_approved and telegram_id != self.creator_id:
            # Already approved — still consume? Prefer reject to keep code unused
            raise ValueError("У вас уже есть доступ")

        invite.is_used = True
        invite.used_by = telegram_id
        invite.used_at = datetime.now(timezone.utc)
        user.is_approved = True
        if user.role == UserRole.user:
            user.role = UserRole.admin
        await self.session.flush()
        logger.info("Invite %s redeemed by %s", normalized, telegram_id)
        return user

    async def list_users(self) -> Sequence[PanelUser]:
        result = await self.session.execute(
            select(PanelUser).order_by(PanelUser.created_at.desc()).limit(100)
        )
        return result.scalars().all()
