"""Shared helpers for routers."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tg_pool.admin.keyboards import main_menu_kb
from tg_pool.admin.texts import main_menu_text


async def show_main_menu(
    target: Message | CallbackQuery,
    *,
    is_creator: bool,
    edit: bool = False,
) -> None:
    text = main_menu_text()
    kb = main_menu_kb(is_creator=is_creator)
    if isinstance(target, CallbackQuery):
        if edit and target.message:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        elif target.message:
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


async def safe_edit(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


async def clear_state(state: FSMContext | None) -> None:
    if state is not None:
        await state.clear()
