"""
Фабрика инлайн-клавиатур: пагинация, навигация, статусы.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from keyboards import cbt as CBT


def flag(on: bool) -> str:
    return "🟢" if on else "🔴"


def nav_row(
    *,
    back: str = CBT.BACK,
    home: str = CBT.HOME,
    refresh: str | None = CBT.REFRESH,
) -> list[InlineKeyboardButton]:
    row = [InlineKeyboardButton(text="◀️ Назад", callback_data=back)]
    if refresh:
        row.append(InlineKeyboardButton(text="🔄 Обновить", callback_data=refresh))
    row.append(InlineKeyboardButton(text="🏠 Меню", callback_data=home))
    return row


def pagination_row(
    page: int,
    total_pages: int,
    prefix: str,
) -> list[InlineKeyboardButton] | None:
    if total_pages <= 1:
        return None
    buttons: list[InlineKeyboardButton] = []
    if page > 1:
        buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"{CBT.PAGE_PREV}{prefix}{page}"))
    buttons.append(InlineKeyboardButton(text=f"· {page} / {total_pages} ·", callback_data="sc:noop"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"{CBT.PAGE_NEXT}{prefix}{page}"))
    return buttons


def build_keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*row)
    return builder.as_markup()


def divider_header(title: str) -> str:
    return f"━━━━━━━━━━━━━━━━━━\n{title}\n━━━━━━━━━━━━━━━━━━"
