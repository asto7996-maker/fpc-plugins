"""
Access middleware — invite-gated panel with creator bypass.

Rules
-----
* telegram_id == creator_id → always allowed (role creator)
* registered + is_approved → allowed
* otherwise → only /start and invite-code FSM messages pass
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from tg_pool.admin.states import InviteStates
from tg_pool.admin.texts import welcome_locked
from tg_pool.config import Settings
from tg_pool.db.session import session_scope
from tg_pool.services.access_service import AccessService

logger = logging.getLogger(__name__)

# Commands / prefixes allowed before approval
_PUBLIC_COMMANDS = {"/start", "/help"}


def _extract_user(event: TelegramObject) -> Optional[User]:
    if isinstance(event, Message) and event.from_user:
        return event.from_user
    if isinstance(event, CallbackQuery) and event.from_user:
        return event.from_user
    return None


def _is_public_message(event: TelegramObject) -> bool:
    if not isinstance(event, Message) or not event.text:
        return False
    text = event.text.strip()
    cmd = text.split()[0].split("@")[0].lower()
    return cmd in _PUBLIC_COMMANDS


class AccessMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, bot: Bot) -> None:
        self.settings = settings
        self.bot = bot

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _extract_user(event)
        if user is None:
            return await handler(event, data)

        full_name = " ".join(
            x for x in (user.first_name, user.last_name) if x
        ).strip() or None

        async with session_scope() as session:
            access = AccessService(session, creator_id=self.settings.creator_id)
            panel_user = await access.ensure_user(
                user.id,
                username=user.username,
                full_name=full_name,
            )
            # Detach simple flags for handlers
            data["panel_user"] = panel_user
            data["is_creator"] = access.is_creator(user.id)
            allowed = access.has_access(panel_user, user.id)

        if allowed:
            return await handler(event, data)

        # Allow public commands
        if _is_public_message(event):
            return await handler(event, data)

        # Allow invite-code input while in InviteStates.waiting_code
        state: Optional[FSMContext] = data.get("state")
        if state is not None:
            current = await state.get_state()
            if current == InviteStates.waiting_code.state and isinstance(event, Message):
                return await handler(event, data)

        # Block everything else with a soft prompt
        if isinstance(event, CallbackQuery):
            await event.answer("🔒 Нужен инвайт-код", show_alert=True)
            try:
                await event.message.answer(  # type: ignore[union-attr]
                    welcome_locked(user.username),
                    parse_mode="HTML",
                )
            except Exception:  # noqa: BLE001
                pass
            # Put user into invite FSM
            if state is not None:
                await state.set_state(InviteStates.waiting_code)
            return None

        if isinstance(event, Message):
            if state is not None:
                await state.set_state(InviteStates.waiting_code)
            await event.answer(welcome_locked(user.username), parse_mode="HTML")
        return None
