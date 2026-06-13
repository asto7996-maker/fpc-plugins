"""Карточка плагина — как в FunPay Cardinal."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards import cbt as CBT


def plugin_card_text(rec: Any, extra: str = "") -> str:
    loaded = datetime.fromtimestamp(rec.loaded_at or time.time()).strftime("%H:%M:%S")
    status = "🟢 активен" if rec.enabled else "🔴 выключен"
    lines = [
        f"<i><b>{rec.name} v{rec.version}</b></i>",
        rec.description or "—",
        "",
        f"<b><i>UUID:</i></b> <code>{rec.uuid}</code>",
        f"<b><i>Автор:</i></b> {rec.credits or '—'}",
        f"<b><i>Статус:</i></b> {status}",
        f"<b><i>Последнее обновление:</i></b> {loaded}",
    ]
    if rec.load_error:
        lines.append(f"\n⚠️ <b>Ошибка:</b> {rec.load_error}")
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


def plugin_card_keyboard(rec: Any, *, has_commands: bool = False, has_panel: bool = False, has_settings: bool = False) -> InlineKeyboardMarkup:
    uuid = rec.uuid
    rows: list[list[InlineKeyboardButton]] = []

    toggle_label = "🔴 Деактивировать" if rec.enabled else "🟢 Активировать"
    rows.append([InlineKeyboardButton(text=toggle_label, callback_data=f"{CBT.PLUGIN_ACTIVATE}{uuid}")])

    pin_label = "📌 Открепить" if getattr(rec, "pinned", False) else "📌 Закрепить"
    rows.append([InlineKeyboardButton(text=pin_label, callback_data=f"{CBT.PLUGIN_PIN}{uuid}")])

    if has_panel:
        rows.append([InlineKeyboardButton(text="🎛 Панель плагина", callback_data=f"{CBT.PLUGIN_PANEL}{uuid}")])

    if has_commands:
        rows.append([InlineKeyboardButton(text="⌨️ Команды", callback_data=f"{CBT.PLUGIN_COMMANDS}{uuid}")])

    if has_settings:
        rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"{CBT.PLUGIN_SETTINGS}{uuid}")])

    rows.append([
        InlineKeyboardButton(text="🔄 Reload", callback_data=f"{CBT.PLUGIN_RELOAD}{uuid}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{CBT.PLUGIN_DELETE}{uuid}"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=CBT.PLUGINS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plugin_commands_text(rec: Any, commands: list[dict]) -> str:
    lines = [
        f"⌨️ <b>Команды плагина {rec.name}</b>",
        "",
    ]
    if commands:
        for cmd in commands:
            name = cmd.get("command", "").lstrip("/")
            desc = cmd.get("description", "")
            lines.append(f"/{name} — {desc}")
    else:
        lines.append("<i>У плагина нет зарегистрированных команд.</i>")
    return "\n".join(lines)


def plugin_commands_keyboard(uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"{CBT.PLUGIN_VIEW}{uuid}")],
    ])


def plugin_delete_confirm_keyboard(uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"{CBT.PLUGIN_DELETE_OK}{uuid}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"{CBT.PLUGIN_VIEW}{uuid}"),
        ],
    ])
