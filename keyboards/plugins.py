"""Панель плагинов — список как в FunPay Cardinal."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton

from keyboards import cbt as CBT
from keyboards.factory import build_keyboard, nav_row, pagination_row

PLUGINS_PER_PAGE = 8


def plugins_panel_keyboard(
    records: list[Any],
    page: int = 1,
) -> object:
    total = max(1, (len(records) + PLUGINS_PER_PAGE - 1) // PLUGINS_PER_PAGE)
    page = max(1, min(page, total))
    start = (page - 1) * PLUGINS_PER_PAGE
    chunk = records[start : start + PLUGINS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    for rec in chunk:
        st = "🟢" if rec.enabled else "🔴"
        pin = "📌 " if getattr(rec, "pinned", False) else ""
        err = " ⚠️" if rec.load_error else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{pin}{rec.name} {st}{err}",
                callback_data=f"{CBT.PLUGIN_VIEW}{rec.uuid}",
            ),
        ])

    pag = pagination_row(page, total, "plugins:")
    if pag:
        rows.append(pag)
    rows.append([
        InlineKeyboardButton(text="➕ Добавить плагин", callback_data=CBT.PLUGIN_UPLOAD),
    ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=CBT.HOME)])
    return build_keyboard(rows)


def plugins_panel_text(records: list[Any], page: int, total_pages: int) -> str:
    return (
        "🔌 <b>Плагины</b>\n\n"
        "Здесь ты можешь получить информацию о плагинах, а также настроить их.\n\n"
        "⚠️ <i>После активации / деактивации / добавления / удаления плагина "
        "рекомендуется перезапустить бота!</i> <code>/restart</code>\n\n"
        f"Всего: <b>{len(records)}</b> · стр. {page}/{total_pages}"
    )
