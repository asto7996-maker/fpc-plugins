"""
Панель уведомлений — premium grid.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton

from config import load_settings
from handlers.tg.loading import loading_skeleton
from keyboards import cbt as CBT
from keyboards.factory import build_keyboard, flag, nav_row

logger = logging.getLogger("starvell.handlers.notify")

NOTIFY_FIELDS = [
    ("notify_orders", "🛒 Заказы", "Новые оплаты и смена статусов"),
    ("notify_chats", "💬 Чаты", "Сообщения покупателей"),
    ("notify_delivery", "📦 Выдача", "Автовыдача и ошибки склада"),
    ("notify_bump", "📈 Бамп", "Поднятие лотов"),
    ("notify_auth", "🔐 Сессия", "Проблемы авторизации Starvell"),
]


def create_notifications_router(ctx: Any) -> Router:
    router = Router(name="notifications")

    @router.callback_query(F.data == f"{CBT.CATEGORY}tg")
    async def cb_notify_panel(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        await _render_notify(call, ctx)

    @router.callback_query(F.data.startswith(CBT.NOTIFY))
    async def cb_notify_toggle(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        field = call.data.replace(CBT.NOTIFY, "")
        if field in ("menu", "all"):
            await _render_notify(call, ctx)
            return
        new_val = await ctx.db.toggle_notify(call.from_user.id, field)
        await call.answer("🟢 Вкл" if new_val else "🔴 Выкл")
        await _render_notify(call, ctx, edit_only=True)

    @router.callback_query(F.data == CBT.NOTIFY_ALL_ON)
    async def cb_all_on(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        for f, _, _ in NOTIFY_FIELDS:
            user = await ctx.db.get_user(call.from_user.id)
            if not user.get(f):
                await ctx.db.toggle_notify(call.from_user.id, f)
        await call.answer("✅ Все включены")
        await _render_notify(call, ctx, edit_only=True)

    @router.callback_query(F.data == CBT.NOTIFY_ALL_OFF)
    async def cb_all_off(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        for f, _, _ in NOTIFY_FIELDS:
            user = await ctx.db.get_user(call.from_user.id)
            if user.get(f):
                await ctx.db.toggle_notify(call.from_user.id, f)
        await call.answer("🔴 Все выключены")
        await _render_notify(call, ctx, edit_only=True)

    return router


async def _render_notify(call: CallbackQuery, ctx: Any, edit_only: bool = False) -> None:
    async def render():
        user = await ctx.db.get_user(call.from_user.id)
        s = load_settings()
        lines = [
            "🔔 <b>Уведомления Telegram</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"Аккаунт: <code>{s.starvell_username or '—'}</code>",
            "",
        ]
        for field, title, desc in NOTIFY_FIELDS:
            on = bool(user.get(field, 1))
            lines.append(f"{flag(on)} <b>{title}</b>\n   <i>{desc}</i>")
        lines.append("\n<i>Нажмите кнопку для переключения</i>")

        rows: list[list[InlineKeyboardButton]] = []
        for field, title, _ in NOTIFY_FIELDS:
            on = bool(user.get(field, 1))
            rows.append([
                InlineKeyboardButton(text=f"{flag(on)} {title}", callback_data=f"{CBT.NOTIFY}{field}"),
            ])
        rows.append([
            InlineKeyboardButton(text="🟢 Все вкл", callback_data=CBT.NOTIFY_ALL_ON),
            InlineKeyboardButton(text="🔴 Все выкл", callback_data=CBT.NOTIFY_ALL_OFF),
        ])
        rows.append(nav_row(back=CBT.SETTINGS, home=CBT.HOME, refresh=f"{CBT.CATEGORY}tg"))

        await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=build_keyboard(rows))

    if edit_only:
        await render()
    else:
        async with loading_skeleton(call):
            await render()
