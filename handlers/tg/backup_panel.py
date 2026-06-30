"""Бэкап: создание, загрузка, восстановление."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import BACKUP_DIR
from keyboards import cbt as CBT
from tg_bot import keyboards as KB
from utils.tools import create_backup, export_settings_snapshot, restore_backup


class BackupStates(StatesGroup):
    waiting_file = State()


def create_backup_router(ctx: Any) -> Router:
    router = Router(name="backup_panel")

    async def _access(user_id: int) -> bool:
        return await ctx._has_access(user_id)

    @router.callback_query(F.data == CBT.BACKUP)
    async def cb_backup_menu(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await state.clear()
        text = (
            "💾 <b>Резервное копирование</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "• <b>Создать</b> — архив config + storage\n"
            "• <b>Загрузить</b> — восстановить из .zip\n"
            "• <b>Текущие данные</b> — JSON настроек (секреты скрыты)\n\n"
            "Команды:\n"
            "/backup — создать и отправить\n"
            "/upload — загрузить архив\n"
            "/export — выгрузить текущие данные"
        )
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=KB.backup_kb())
        await call.answer()

    @router.callback_query(F.data == CBT.BACKUP_DL)
    async def cb_backup_download(call: CallbackQuery) -> None:
        if not await _access(call.from_user.id):
            return
        await call.answer("Создаю бэкап…")
        path = create_backup()
        await call.message.answer_document(
            open(path, "rb"),
            caption=f"💾 Бэкап: <code>{path.name}</code>",
            parse_mode="HTML",
        )

    @router.callback_query(F.data == CBT.BACKUP_EXPORT)
    async def cb_backup_export(call: CallbackQuery) -> None:
        if not await _access(call.from_user.id):
            return
        await call.answer("Формирую…")
        path = export_settings_snapshot()
        await call.message.answer_document(
            open(path, "rb"),
            caption="📋 Текущие настройки (секреты маскированы)",
        )

    @router.callback_query(F.data == CBT.BACKUP_UL)
    async def cb_backup_upload(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await state.set_state(BackupStates.waiting_file)
        await call.message.answer(
            "📥 Отправьте файл <b>.zip</b> с бэкапом (config + storage):",
            parse_mode="HTML",
        )
        await call.answer()

    @router.message(BackupStates.waiting_file, F.document)
    async def on_backup_file(message: Message, state: FSMContext) -> None:
        if not await _access(message.from_user.id):
            return
        doc = message.document
        if not doc or not (doc.file_name or "").endswith(".zip"):
            await message.answer("❌ Нужен файл .zip")
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = BACKUP_DIR / f"upload_{doc.file_name}"
        file = await message.bot.get_file(doc.file_id)
        await message.bot.download_file(file.file_path, dest)
        try:
            restore_backup(dest)
            await message.answer("✅ Бэкап восстановлен. Рекомендуется /restart", reply_markup=KB.back_menu())
        except Exception as exc:
            await message.answer(f"❌ Ошибка восстановления: {exc}")
        await state.clear()

    return router
