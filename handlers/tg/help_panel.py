"""Справка и команды бота."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from config import VERSION
from keyboards import cbt as CBT
from tg_bot import keyboards as KB


HELP_TEXT = (
    f"❓ <b>Справка Starvell Cardinal</b> v{VERSION}\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "<b>Основные команды:</b>\n"
    "/start — главное меню\n"
    "/menu — главное меню\n"
    "/profile — профиль и баланс\n"
    "/status — краткая статистика\n"
    "/session — привязать Starvell cookie\n"
    "/restart — перезапуск бота\n"
    "/cancel — выход из режима в меню\n"
    "/plugins — список плагинов\n"
    "/backup — создать бэкап\n"
    "/upload — загрузить бэкап\n"
    "/export — текущие данные (JSON)\n"
    "/parser — парсер лотов FunPay\n"
    "/logs — архив логов\n"
    "/sys — системная информация\n"
    "/help — эта справка\n\n"
    "<b>Разделы меню:</b>\n"
    "👤 Профиль — баланс, заказы, session\n"
    "🔍 Парсер — копирование лотов с FunPay\n"
    "💾 Бэкап — сохранение и восстановление\n"
    "🔌 Плагины — VexBoost AutoSMM и др."
)


def create_help_router(ctx: Any) -> Router:
    router = Router(name="help_panel")

    @router.callback_query(F.data == CBT.HELP)
    async def cb_help(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        await call.message.edit_text(HELP_TEXT, parse_mode="HTML", reply_markup=KB.back_menu())
        await call.answer()

    @router.message(F.text == "/help")
    async def cmd_help(message: Message) -> None:
        if not await ctx._has_access(message.from_user.id):
            return
        await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=KB.back_menu())

    return router
