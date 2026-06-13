"""Панель управления плагинами с пагинацией."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton

from keyboards import cbt as CBT
from keyboards.factory import build_keyboard, flag, nav_row, pagination_row

PLUGINS_PER_PAGE = 5


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
        pin = "📌 " if getattr(rec, "pinned", False) else ""
        native = " ⭐" if getattr(rec, "is_starvell_native", False) else ""
        fpc = " 🎮" if getattr(rec, "is_fpc_only", False) else ""
        hook = " 🔗" if getattr(rec, "hook_only", False) else ""
        st = flag(rec.enabled)
        err = " ⚠️" if rec.load_error else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{pin}{st} {rec.name} v{rec.version}{native}{fpc}{hook}{err}",
                callback_data=f"{CBT.PLUGIN_TOGGLE}{rec.uuid}",
            ),
        ])
        action_row = [
            InlineKeyboardButton(text="🔄", callback_data=f"{CBT.PLUGIN_RELOAD}{rec.uuid}"),
            InlineKeyboardButton(text="📌", callback_data=f"{CBT.PLUGIN_PIN}{rec.uuid}"),
        ]
        if getattr(rec, "has_settings_page", False) or getattr(rec, "is_base_plugin", False):
            action_row.append(
                InlineKeyboardButton(text="⚙️", callback_data=f"{CBT.PLUGIN_SETTINGS}{rec.uuid}")
            )
        rows.append(action_row)

    pag = pagination_row(page, total, "plugins:")
    if pag:
        rows.append(pag)
    rows.append([
        InlineKeyboardButton(text="📤 Загрузить плагин", callback_data=CBT.PLUGIN_UPLOAD),
    ])
    rows.append(nav_row(back=CBT.HOME, refresh=CBT.PLUGINS))
    return build_keyboard(rows)


def plugins_panel_text(records: list[Any], page: int, total_pages: int) -> str:
    enabled = sum(1 for r in records if r.enabled)
    return (
        "🔌 <b>Панель плагинов</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Активно: <b>{enabled}</b> / {len(records)}\n"
        f"Страница {page} / {total_pages}\n\n"
        "<i>🟢 Вкл · 🔴 Выкл · ⭐ Starvell · 🎮 FPC · 📌 Закреп · ⚙️ Настройки</i>"
    )
