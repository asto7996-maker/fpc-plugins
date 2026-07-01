"""Переключатели и настройки главного меню (в hub — до legacy router)."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery

from config import load_settings
from keyboards import cbt as CBT
from tg_bot import keyboards as KB


def create_menu_actions_router(ctx: Any) -> Router:
    router = Router(name="menu_actions")

    @router.callback_query(F.data.startswith(CBT.SWITCH))
    async def cb_switch(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            await ctx._deny(call)
            return
        key = call.data[len(CBT.SWITCH):]
        await ctx.toggle_feature(call, key)

    @router.callback_query(F.data == CBT.SETTINGS)
    async def cb_settings(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            await ctx._deny(call)
            return
        await call.message.edit_text(
            "⚙️ <b>Настройки</b> (стр. 1)",
            parse_mode="HTML",
            reply_markup=KB.settings_page1(),
        )
        await call.answer()

    @router.callback_query(F.data == CBT.MAIN2)
    async def cb_settings2(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            await ctx._deny(call)
            return
        await call.message.edit_text(
            "⚙️ <b>Настройки</b> (стр. 2)",
            parse_mode="HTML",
            reply_markup=KB.settings_page2(),
        )
        await call.answer()

    @router.callback_query(F.data.startswith(CBT.CATEGORY))
    async def cb_category(call: CallbackQuery, state) -> None:
        await ctx.cb_category(call, state)

    return router
