"""Клавиатуры страницы настроек плагина."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards import cbt as CBT


def plugin_settings_nav(uuid: str) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(text="🔄 Сброс", callback_data=f"{CBT.PLUGIN_RESET}{uuid}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"{CBT.PLUGIN_VIEW}{uuid}"),
        ],
    ]


def plugin_select_keyboard(uuid: str, field_idx: int, options: list[Any], current: Any) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx, opt in enumerate(options):
        if isinstance(opt, dict):
            value = opt.get("value", opt.get("label", ""))
            label = str(opt.get("label", value))
        else:
            value = opt
            label = str(opt)
        mark = "✅ " if str(value) == str(current) else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{mark}{label}",
                callback_data=f"{CBT.PLUGIN_SELECT_SET}{uuid}:{field_idx}:{idx}",
            ),
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}{uuid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
