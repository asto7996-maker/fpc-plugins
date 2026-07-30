"""Superadmin access / invite management (`/admin`)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from tg_pool.admin.keyboards import access_kb
from tg_pool.admin.routers.common import safe_edit
from tg_pool.admin.texts import (
    access_users_text,
    admin_panel_text,
    invite_created_text,
    invites_text,
)
from tg_pool.config import Settings
from tg_pool.db.session import session_scope
from tg_pool.services.access_service import AccessService


def build_access_router(settings: Settings) -> Router:
    router = Router(name="access")

    def _guard(is_creator: bool) -> bool:
        return bool(is_creator)

    @router.message(Command("admin"))
    async def cmd_admin(message: Message, is_creator: bool = False) -> None:
        if not _guard(is_creator):
            await message.answer(
                "🛡 <b>Доступ запрещён</b>\n"
                "<blockquote>Команда /admin только для суперадминистратора.</blockquote>",
                parse_mode="HTML",
            )
            return
        await message.answer(
            admin_panel_text(),
            parse_mode="HTML",
            reply_markup=access_kb(),
        )

    @router.callback_query(F.data == "menu:access")
    async def cb_access(callback: CallbackQuery, is_creator: bool = False) -> None:
        if not _guard(is_creator):
            await callback.answer("Только для creator", show_alert=True)
            return
        await safe_edit(callback, admin_panel_text(), access_kb())
        await callback.answer()

    @router.callback_query(F.data == "access:create")
    async def cb_create(callback: CallbackQuery, is_creator: bool = False) -> None:
        if not _guard(is_creator):
            await callback.answer("Forbidden", show_alert=True)
            return
        assert callback.from_user is not None
        async with session_scope() as session:
            invite = await AccessService(
                session, creator_id=settings.creator_id
            ).create_invite(callback.from_user.id)
            code = invite.code
        await callback.message.answer(  # type: ignore[union-attr]
            invite_created_text(code),
            parse_mode="HTML",
            reply_markup=access_kb(),
        )
        await callback.answer("Код создан")

    @router.callback_query(F.data == "access:list_active")
    async def cb_list_active(callback: CallbackQuery, is_creator: bool = False) -> None:
        if not _guard(is_creator):
            await callback.answer("Forbidden", show_alert=True)
            return
        async with session_scope() as session:
            invites = await AccessService(
                session, creator_id=settings.creator_id
            ).list_invites(only_active=True)
        await safe_edit(callback, invites_text(invites), access_kb())
        await callback.answer()

    @router.callback_query(F.data == "access:list_used")
    async def cb_list_used(callback: CallbackQuery, is_creator: bool = False) -> None:
        if not _guard(is_creator):
            await callback.answer("Forbidden", show_alert=True)
            return
        async with session_scope() as session:
            invites = await AccessService(
                session, creator_id=settings.creator_id
            ).list_invites(only_active=False)
        await safe_edit(callback, invites_text(invites), access_kb())
        await callback.answer()

    @router.callback_query(F.data == "access:users")
    async def cb_users(callback: CallbackQuery, is_creator: bool = False) -> None:
        if not _guard(is_creator):
            await callback.answer("Forbidden", show_alert=True)
            return
        async with session_scope() as session:
            users = await AccessService(
                session, creator_id=settings.creator_id
            ).list_users()
        await safe_edit(callback, access_users_text(users), access_kb())
        await callback.answer()

    return router
