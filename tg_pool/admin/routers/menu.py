"""Main menu, help, profile, stats navigation."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from tg_pool.admin.keyboards import (
    profile_kb,
    stats_kb,
)
from tg_pool.admin.routers.common import safe_edit, show_main_menu
from tg_pool.admin.texts import help_text, profile_text, stats_text
from tg_pool.config import Settings
from tg_pool.db.models import Account, AccountStatus, Proxy
from tg_pool.db.session import session_scope


def build_menu_router(settings: Settings) -> Router:
    router = Router(name="menu")

    @router.message(Command("menu"))
    async def cmd_menu(message: Message, is_creator: bool = False) -> None:
        await show_main_menu(message, is_creator=is_creator)

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        from tg_pool.admin.keyboards import back_home_kb

        await message.answer(help_text(), parse_mode="HTML", reply_markup=back_home_kb())

    @router.message(Command("profile"))
    async def cmd_profile(message: Message, panel_user=None, is_creator: bool = False) -> None:
        if panel_user is None:
            await message.answer("Профиль недоступен.")
            return
        await message.answer(
            profile_text(panel_user),
            parse_mode="HTML",
            reply_markup=profile_kb(is_creator=is_creator),
        )

    @router.callback_query(F.data == "menu:home")
    @router.callback_query(F.data == "nav:back")
    @router.callback_query(F.data == "nav:refresh")
    async def cb_home(callback: CallbackQuery, is_creator: bool = False) -> None:
        await show_main_menu(callback, is_creator=is_creator, edit=True)

    @router.callback_query(F.data == "menu:help")
    async def cb_help(callback: CallbackQuery) -> None:
        from tg_pool.admin.keyboards import back_home_kb

        await safe_edit(callback, help_text(), back_home_kb())
        await callback.answer()

    @router.callback_query(F.data == "menu:profile")
    async def cb_profile(
        callback: CallbackQuery,
        panel_user=None,
        is_creator: bool = False,
    ) -> None:
        if panel_user is None:
            await callback.answer("Нет профиля", show_alert=True)
            return
        await safe_edit(
            callback,
            profile_text(panel_user),
            profile_kb(is_creator=is_creator),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:stats")
    async def cb_stats(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            total = (
                await session.execute(select(func.count()).select_from(Account))
            ).scalar_one()
            counts = {}
            for st in AccountStatus:
                counts[st] = (
                    await session.execute(
                        select(func.count())
                        .select_from(Account)
                        .where(Account.status == st)
                    )
                ).scalar_one()
            actions = (
                await session.execute(select(func.coalesce(func.sum(Account.total_actions_today), 0)))
            ).scalar_one()
            proxies = (
                await session.execute(select(func.count()).select_from(Proxy))
            ).scalar_one()

        text = stats_text(
            total=int(total),
            active=int(counts.get(AccountStatus.active, 0)),
            flood=int(counts.get(AccountStatus.flood_wait, 0)),
            banned=int(counts.get(AccountStatus.banned, 0)),
            paused=int(counts.get(AccountStatus.paused, 0)),
            spambot=int(counts.get(AccountStatus.spambot, 0)),
            actions_today=int(actions or 0),
            proxies=int(proxies),
        )
        await safe_edit(callback, text, stats_kb())
        await callback.answer()

    return router
