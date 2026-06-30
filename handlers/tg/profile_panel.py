"""Профиль и статистика Starvell."""

from __future__ import annotations

from typing import Any

import logging

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import VERSION, load_settings
from keyboards import cbt as CBT
from tg_bot import keyboards as KB
from utils.starvell_format import format_hold_balance, format_rub_balance
from utils.tools import system_stats

logger = logging.getLogger("starvell.profile")


async def build_profile_brief(ctx: Any) -> tuple[str, InlineKeyboardMarkup]:
    status = await ctx.automation.get_status()
    s = load_settings()
    lines = [
        "👤 <b>Профиль</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🆔 Telegram ID: <code>{s.owner_id or '—'}</code>",
        f"🤖 Бот: v{VERSION}",
        "",
    ]
    for acc in status.get("accounts", []):
        if acc.get("error"):
            lines.append(f"❌ {acc.get('name')}: {acc['error']}")
            continue
        if not acc.get("authorized"):
            lines.append("❌ Starvell не авторизован")
            continue
        bal = acc.get("balance_formatted") or format_rub_balance(acc.get("balance"))
        hold = acc.get("hold_formatted") or format_hold_balance(acc.get("balance"))
        lines += [
            f"👤 <b>{acc.get('username', '?')}</b>",
            f"💰 Баланс: <b>{bal}</b>",
        ]
        if hold:
            lines.append(f"🔒 В холде: {hold}")
        lines += [
            f"📦 Активных заказов: <b>{acc.get('active_orders', 0)}</b>",
            f"📋 В ленте: <b>{acc.get('total_orders', 0)}</b>",
        ]
        if acc.get("lots_count") is not None:
            lines.append(f"🏷 Лотов: <b>{acc['lots_count']}</b>")
        pe_total = acc.get("plugins_total")
        if pe_total is not None:
            lines.append(
                f"🔌 Плагинов: <b>{acc.get('plugins_enabled', 0)}</b> / {pe_total}"
            )
    lines += [
        "",
        f"Session: {'✅ задан' if s.session_cookie else '❌ не задан'}",
        f"Gemini: {'✅' if s.is_gemini_configured() else '❌'}",
    ]
    return "\n".join(lines), KB.profile_kb()


async def build_profile_detail(ctx: Any) -> str:
    status = await ctx.automation.get_status()
    s = load_settings()
    lines = [
        "📈 <b>Подробная статистика</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for acc in status.get("accounts", []):
        if acc.get("error"):
            lines.append(f"❌ {acc.get('name')}: {acc['error']}\n")
            continue
        bal = acc.get("balance_formatted") or format_rub_balance(acc.get("balance"))
        lines += [
            f"👤 <b>{acc.get('username', '?')}</b> ({acc.get('name', 'default')})",
            f"💰 Баланс: <b>{bal}</b>",
            f"📦 Активных: {acc.get('active_orders', 0)}",
            f"📋 Всего в ленте: {acc.get('total_orders', 0)}",
            f"🏷 Лотов: {acc.get('lots_count', '—')}",
            "",
        ]
    lines += [
        "<b>⚙️ Модули</b>",
        f"• Автовыдача: {'🟢' if s.auto_delivery_enabled else '🔴'}",
        f"• Автобамп: {'🟢' if s.auto_bump_enabled else '🔴'}",
        f"• Приветствие: {'🟢' if s.auto_welcome_enabled else '🔴'}",
        f"• Gemini чаты: {'🟢' if s.ai_replies_enabled else '🔴'}",
        f"• Poll чатов: {s.chat_poll_interval}с",
        f"• Poll заказов: {s.orders_poll_interval}с",
        "",
        system_stats(),
    ]
    return "\n".join(lines)


async def build_status_text(ctx: Any) -> str:
    text, _ = await build_profile_brief(ctx)
    return text.replace("👤 <b>Профиль</b>", "📊 <b>Статистика</b>", 1)


def create_profile_router(ctx: Any):
    from aiogram import F, Router
    from aiogram.fsm.context import FSMContext

    router = Router(name="profile_panel")

    async def _access(user_id: int) -> bool:
        return await ctx._has_access(user_id)

    @router.callback_query(F.data == CBT.PROFILE)
    async def cb_profile(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            await ctx._deny(call)
            return
        await state.clear()
        await call.answer()
        try:
            await call.message.edit_text("⏳ Загружаю профиль…")
        except Exception:
            pass
        try:
            text, kb = await build_profile_brief(ctx)
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            logger.exception("profile: %s", exc)
            await call.message.edit_text(
                f"❌ Не удалось загрузить профиль.\n<code>{exc}</code>\n\nПопробуйте /ping или /start",
                parse_mode="HTML",
                reply_markup=KB.back_menu(),
            )

    @router.callback_query(F.data == CBT.PROFILE_DETAIL)
    async def cb_profile_detail(call: CallbackQuery) -> None:
        if not await _access(call.from_user.id):
            return
        await call.answer()
        try:
            await call.message.edit_text("⏳ Загружаю статистику…")
        except Exception:
            pass
        text = await build_profile_detail(ctx)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Профиль", callback_data=CBT.PROFILE)],
            [InlineKeyboardButton(text="🏠 Меню", callback_data=CBT.MAIN)],
        ])
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data == CBT.STATUS)
    async def cb_status(call: CallbackQuery) -> None:
        if not await _access(call.from_user.id):
            return
        await call.answer()
        try:
            await call.message.edit_text("⏳ Загружаю статистику…")
        except Exception:
            pass
        text = await build_status_text(ctx)
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=KB.back_menu())

    return router
