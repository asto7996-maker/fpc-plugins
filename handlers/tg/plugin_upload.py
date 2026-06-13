"""
Загрузка нативных плагинов Starvell через Telegram (.py файл).
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from keyboards import cbt as CBT
from keyboards.plugins import plugins_panel_keyboard, plugins_panel_text, PLUGINS_PER_PAGE

logger = logging.getLogger("starvell.handlers.plugin_upload")


class PluginUploadStates(StatesGroup):
    waiting_file = State()


def create_plugin_upload_router(ctx: Any) -> Router:
    router = Router(name="plugin_upload")
    pm = ctx.plugin_manager

    @router.callback_query(F.data == CBT.PLUGIN_UPLOAD)
    async def cb_plugin_upload(call: CallbackQuery, state: FSMContext) -> None:
        if not await ctx._has_access(call.from_user.id):
            await ctx._deny(call)
            return
        await state.set_state(PluginUploadStates.waiting_file)
        await call.message.answer(
            "📤 <b>Загрузка плагина Starvell</b>\n\n"
            "Отправьте файл <code>.py</code> с плагином.\n\n"
            "Требования:\n"
            "• <code>NAME</code>, <code>UUID</code>, <code>VERSION</code>\n"
            "• <code>class Plugin(StarvellPlugin)</code>\n"
            "• <code>from starvell_sdk import ...</code>\n\n"
            "<i>/cancel — отмена</i>",
            parse_mode="HTML",
        )
        await call.answer()

    @router.message(PluginUploadStates.waiting_file, F.document)
    async def on_plugin_document(message: Message, state: FSMContext) -> None:
        if not await ctx._has_access(message.from_user.id):
            return
        doc = message.document
        if not doc or not (doc.file_name or "").endswith(".py"):
            await message.answer("❌ Нужен файл с расширением <code>.py</code>", parse_mode="HTML")
            return

        try:
            file = await ctx.bot.get_file(doc.file_id)
            data = await ctx.bot.download_file(file.file_path)
            source = data.read().decode("utf-8")
        except Exception as exc:
            await message.answer(f"❌ Не удалось прочитать файл: {exc}")
            return

        ok, reason = pm.validate_plugin_source(source)
        if not ok:
            await message.answer(f"❌ Плагин не прошёл проверку:\n<code>{reason}</code>", parse_mode="HTML")
            return

        try:
            path = pm.save_plugin_file(doc.file_name or "plugin.py", source)
            pm.load_all()
            await pm.startup_starvell_plugins()
        except Exception as exc:
            logger.exception("plugin upload failed")
            await message.answer(f"❌ Ошибка сохранения: {exc}")
            return

        await state.clear()
        records = list(pm.plugins.values())
        total = max(1, (len(records) + PLUGINS_PER_PAGE - 1) // PLUGINS_PER_PAGE)
        await message.answer(
            f"✅ Плагин сохранён: <code>{path}</code>\n"
            f"Перезагружено плагинов: {len(records)}",
            parse_mode="HTML",
            reply_markup=plugins_panel_keyboard(records, 1),
        )
        await message.answer(
            plugins_panel_text(records, 1, total),
            parse_mode="HTML",
        )

    @router.message(PluginUploadStates.waiting_file)
    async def on_plugin_upload_invalid(message: Message) -> None:
        if not await ctx._has_access(message.from_user.id):
            return
        if message.text and message.text.strip().lower() in ("/cancel", "отмена"):
            return
        await message.answer("Отправьте <code>.py</code> файл или /cancel", parse_mode="HTML")

    return router
