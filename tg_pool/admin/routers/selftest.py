""" /selftest — superadmin-only full system diagnostics."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from tg_pool.config import Settings
from tg_pool.health.runner import run_startup_self_test

logger = logging.getLogger(__name__)


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🩺 Запустить Self-Test",
                    callback_data="selftest:run",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔬 Deep (прокси + get_me)",
                    callback_data="selftest:deep",
                )
            ],
        ]
    )


def build_selftest_router(settings: Settings, *, listeners=None) -> Router:
    router = Router(name="selftest")

    def _is_super(user_id: int | None, is_creator: bool) -> bool:
        return bool(is_creator or (user_id is not None and user_id == settings.creator_id))

    @router.message(Command("selftest"))
    async def cmd_selftest(
        message: Message,
        is_creator: bool = False,
    ) -> None:
        uid = message.from_user.id if message.from_user else None
        if not _is_super(uid, is_creator):
            await message.answer("🔒 Только для суперадминистратора.")
            return
        await message.answer(
            "🩺 <b>Self-Testing</b>\n\n"
            "Быстрый прогон критических узлов, либо deep-режим "
            "(TCP-проверка привязанных прокси + <code>get_me</code> агентов).",
            parse_mode="HTML",
            reply_markup=_kb(),
        )

    async def _run(
        target: Message | CallbackQuery,
        *,
        deep: bool,
    ) -> None:
        status_msg: Message
        if isinstance(target, CallbackQuery):
            await target.answer("Запуск…")
            assert target.message is not None
            status_msg = await target.message.answer(
                "⏳ Диагностика… это займёт несколько секунд.",
                parse_mode="HTML",
            )
            bot = target.bot
        else:
            status_msg = await target.answer(
                "⏳ Диагностика… это займёт несколько секунд.",
                parse_mode="HTML",
            )
            bot = target.bot

        report = await run_startup_self_test(
            settings=settings,
            bot=bot,
            alerts=None,  # report goes to chat, not duplicate alert
            listeners=listeners,
            deep_proxies=deep,
            live_agents=deep,
            notify=False,
        )
        text = report.format_html()
        # Telegram message limit soft-cap
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        try:
            await status_msg.edit_text(text, parse_mode="HTML", reply_markup=_kb())
        except Exception:  # noqa: BLE001
            await status_msg.answer(text, parse_mode="HTML", reply_markup=_kb())

    @router.callback_query(F.data == "selftest:run")
    async def cb_run(callback: CallbackQuery, is_creator: bool = False) -> None:
        uid = callback.from_user.id if callback.from_user else None
        if not _is_super(uid, is_creator):
            await callback.answer("🔒 Только creator", show_alert=True)
            return
        await _run(callback, deep=False)

    @router.callback_query(F.data == "selftest:deep")
    async def cb_deep(callback: CallbackQuery, is_creator: bool = False) -> None:
        uid = callback.from_user.id if callback.from_user else None
        if not _is_super(uid, is_creator):
            await callback.answer("🔒 Только creator", show_alert=True)
            return
        await _run(callback, deep=True)

    return router
