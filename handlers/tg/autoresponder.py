"""
Панель автоответчика — FPC-style CRUD с пагинацией.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from handlers.tg.loading import loading_skeleton
from keyboards import cbt as CBT
from keyboards.factory import build_keyboard, nav_row, pagination_row

logger = logging.getLogger("starvell.handlers.ar")

AR_PER_PAGE = 6


def create_autoresponder_router(ctx: Any) -> Router:
    router = Router(name="autoresponder")

    @router.callback_query(F.data == CBT.AR_LIST)
    async def cb_ar_list(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        await _render_ar_page(call, ctx, page=1)

    @router.callback_query(F.data.startswith(CBT.AR_PAGE))
    async def cb_ar_page(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        try:
            page = int(call.data.replace(CBT.AR_PAGE, ""))
        except ValueError:
            page = 1
        await _render_ar_page(call, ctx, page=page)

    @router.callback_query(F.data.startswith(CBT.AR_DEL))
    async def cb_ar_del(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        cmd_id = int(call.data.replace(CBT.AR_DEL, ""))
        await ctx.db.delete_ar_command(cmd_id)
        await call.answer("🗑 Удалено")
        await _render_ar_page(call, ctx, page=1)

    @router.callback_query(F.data.startswith(CBT.AR_TOGGLE))
    async def cb_ar_toggle(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        cmd_id = int(call.data.replace(CBT.AR_TOGGLE, ""))
        await ctx.db.toggle_ar_command(cmd_id, "enabled")
        await call.answer("Переключено")
        await _render_ar_page(call, ctx, page=1)

    @router.callback_query(F.data.startswith(CBT.AR_NOTIFY))
    async def cb_ar_notify(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        cmd_id = int(call.data.replace(CBT.AR_NOTIFY, ""))
        await ctx.db.toggle_ar_command(cmd_id, "notify")
        await call.answer("Уведомления переключены")
        await _render_ar_page(call, ctx, page=1)

    @router.callback_query(F.data.startswith(CBT.PAGE_PREV) & F.data.contains("ar:"))
    async def cb_ar_prev(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        page = int(call.data.split(":")[-1]) - 1
        await _render_ar_page(call, ctx, page=max(1, page))

    @router.callback_query(F.data.startswith(CBT.PAGE_NEXT) & F.data.contains("ar:"))
    async def cb_ar_next(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        page = int(call.data.split(":")[-1]) + 1
        await _render_ar_page(call, ctx, page=page)

    return router


async def _render_ar_page(call: CallbackQuery, ctx: Any, page: int) -> None:
    async with loading_skeleton(call):
        cmds = await ctx.db.list_ar_commands()
        total = max(1, (len(cmds) + AR_PER_PAGE - 1) // AR_PER_PAGE)
        page = max(1, min(page, total))
        start = (page - 1) * AR_PER_PAGE
        chunk = cmds[start : start + AR_PER_PAGE]

        lines = [
            "💬 <b>Автоответчик</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"Команд: <b>{len(cmds)}</b> · стр. {page}/{total}",
            "",
        ]
        if not chunk:
            lines.append("<i>Нет команд. Добавьте первую!</i>")
        else:
            for c in chunk:
                st = "🟢" if c.get("enabled") else "🔴"
                nt = "🔔" if c.get("notify") else "🔕"
                resp = (c.get("response") or "")[:40]
                lines.append(f"{st}{nt} <code>{c['command']}</code> → {resp}…")

        rows: list[list[InlineKeyboardButton]] = []
        for c in chunk:
            cid = c["id"]
            rows.append([
                InlineKeyboardButton(text=f"{'🟢' if c.get('enabled') else '🔴'} {c['command']}", callback_data=f"{CBT.AR_TOGGLE}{cid}"),
                InlineKeyboardButton(text="🔔", callback_data=f"{CBT.AR_NOTIFY}{cid}"),
                InlineKeyboardButton(text="🗑", callback_data=f"{CBT.AR_DEL}{cid}"),
            ])
        rows.append([InlineKeyboardButton(text="➕ Добавить команду", callback_data=CBT.AR_ADD)])
        pag = pagination_row(page, total, "ar:")
        if pag:
            rows.append(pag)
        rows.append(nav_row(back=CBT.SETTINGS, home=CBT.HOME, refresh=CBT.AR_LIST))

        await call.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=build_keyboard(rows),
        )
