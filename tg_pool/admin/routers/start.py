""" /start and invite-code activation FSM."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from tg_pool.admin.keyboards import main_menu_kb, reply_menu_kb
from tg_pool.admin.states import InviteStates
from tg_pool.admin.texts import (
    invite_redeemed_notify,
    welcome_approved,
    welcome_locked,
)
from tg_pool.config import Settings
from tg_pool.db.session import session_scope
from tg_pool.services.access_service import AccessService

logger = logging.getLogger(__name__)


def build_start_router(settings: Settings) -> Router:
    router = Router(name="start")

    @router.message(CommandStart())
    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext, is_creator: bool = False) -> None:
        assert message.from_user is not None
        async with session_scope() as session:
            access = AccessService(session, creator_id=settings.creator_id)
            user = await access.ensure_user(
                message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )
            allowed = access.has_access(user, message.from_user.id)

        # Always clear leftover FSM (TData / proxy add) on /start
        await state.clear()
        if allowed:
            creator = is_creator or user.telegram_id == settings.creator_id
            # Persistent bottom keyboard first, then inline card
            await message.answer(
                welcome_approved(user),
                parse_mode="HTML",
                reply_markup=reply_menu_kb(is_creator=creator),
            )
            await message.answer(
                "🏠 <b>Главное меню</b>\nВыберите раздел:",
                parse_mode="HTML",
                reply_markup=main_menu_kb(is_creator=creator),
            )
            return

        await state.set_state(InviteStates.waiting_code)
        await message.answer(welcome_locked(message.from_user.username), parse_mode="HTML")

    @router.message(InviteStates.waiting_code)
    async def on_invite_code(message: Message, state: FSMContext, bot: Bot) -> None:
        assert message.from_user is not None
        code = (message.text or "").strip()
        if not code:
            await message.answer("Введите инвайт-код, например <code>ALT-XXXX-YYYY</code>", parse_mode="HTML")
            return

        try:
            async with session_scope() as session:
                access = AccessService(session, creator_id=settings.creator_id)
                user = await access.redeem_invite(
                    code,
                    message.from_user.id,
                    username=message.from_user.username,
                    full_name=message.from_user.full_name,
                )
                redeemed_code = code.strip().upper()
        except ValueError as exc:
            await message.answer(
                f"❌ <b>Код не принят</b>\n<blockquote>{exc}</blockquote>\n"
                f"Попробуйте ещё раз или обратитесь к администратору.",
                parse_mode="HTML",
            )
            return

        await state.clear()
        creator = user.telegram_id == settings.creator_id
        await message.answer(
            "✅ <b>Доступ открыт!</b>\n\n" + welcome_approved(user),
            parse_mode="HTML",
            reply_markup=reply_menu_kb(is_creator=creator),
        )
        await message.answer(
            "🏠 <b>Главное меню</b>\nВыберите раздел:",
            parse_mode="HTML",
            reply_markup=main_menu_kb(is_creator=creator),
        )

        # Notify creator
        try:
            await bot.send_message(
                settings.creator_id,
                invite_redeemed_notify(
                    message.from_user.username,
                    message.from_user.id,
                    redeemed_code,
                ),
                parse_mode="HTML",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to notify creator about invite redeem")

    return router
