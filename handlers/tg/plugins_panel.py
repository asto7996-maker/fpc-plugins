"""
Premium handlers: главное меню и плагины.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery

from config import VERSION
from core.i18n import t
from handlers.tg.loading import loading_skeleton
from keyboards import cbt as CBT
from keyboards.main import premium_main_menu, premium_main_text
from keyboards.plugins import PLUGINS_PER_PAGE, plugins_panel_keyboard, plugins_panel_text

logger = logging.getLogger("starvell.handlers.tg")


def create_premium_router(bot_context: Any) -> Router:
    """Создаёт Router с premium UI handlers."""
    router = Router(name="premium")
    pm = bot_context.plugin_manager

    @router.callback_query(F.data == CBT.HOME)
    async def cb_premium_home(call: CallbackQuery) -> None:
        if not await bot_context._has_access(call.from_user.id):
            await bot_context._deny(call)
            return
        async with loading_skeleton(call):
            await call.message.edit_text(
                premium_main_text(VERSION),
                parse_mode="HTML",
                reply_markup=premium_main_menu(),
            )

    @router.callback_query(F.data == CBT.PLUGINS)
    async def cb_plugins_panel(call: CallbackQuery) -> None:
        if not await bot_context._has_access(call.from_user.id):
            await bot_context._deny(call)
            return
        await _render_plugins_page(call, pm, page=1)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_PAGE))
    async def cb_plugins_page(call: CallbackQuery) -> None:
        if not await bot_context._has_access(call.from_user.id):
            return
        try:
            page = int(call.data.rsplit(":", 1)[-1])
        except ValueError:
            page = 1
        await _render_plugins_page(call, pm, page=page)

    @router.callback_query(F.data.startswith(CBT.PAGE_PREV) & F.data.contains("plugins:"))
    async def cb_page_prev(call: CallbackQuery) -> None:
        if not await bot_context._has_access(call.from_user.id):
            return
        page = int(call.data.split(":")[-1]) - 1
        await _render_plugins_page(call, pm, page=max(1, page))

    @router.callback_query(F.data.startswith(CBT.PAGE_NEXT) & F.data.contains("plugins:"))
    async def cb_page_next(call: CallbackQuery) -> None:
        if not await bot_context._has_access(call.from_user.id):
            return
        page = int(call.data.split(":")[-1]) + 1
        await _render_plugins_page(call, pm, page=page)

    @router.callback_query(F.data == CBT.REFRESH)
    async def cb_refresh(call: CallbackQuery) -> None:
        if not await bot_context._has_access(call.from_user.id):
            return
        async with loading_skeleton(call):
            await call.message.edit_text(
                premium_main_text(VERSION),
                parse_mode="HTML",
                reply_markup=premium_main_menu(),
            )

    @router.callback_query(F.data == "sc:noop")
    async def cb_noop(call: CallbackQuery) -> None:
        await call.answer()

    return router


async def _render_plugins_page(call: CallbackQuery, pm: Any, page: int, db: Any = None) -> None:
    async with loading_skeleton(call):
        records = list(pm.plugins.values()) if pm.plugins else pm.load_all()
        if hasattr(pm, "sort_records_pinned") and db:
            records = await pm.sort_records_pinned(records)
        total = max(1, (len(records) + PLUGINS_PER_PAGE - 1) // PLUGINS_PER_PAGE)
        page = max(1, min(page, total))
        await call.message.edit_text(
            plugins_panel_text(records, page, total),
            parse_mode="HTML",
            reply_markup=plugins_panel_keyboard(records, page),
        )
