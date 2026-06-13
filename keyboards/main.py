"""Premium главное меню."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from config import load_settings
from keyboards import cbt as CBT
from keyboards.factory import build_keyboard, flag, nav_row


def premium_main_menu() -> object:
    s = load_settings()
    rows = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data=CBT.STATUS)],
        [
            InlineKeyboardButton(text=f"{flag(s.auto_delivery_enabled)} Выдача", callback_data=f"{CBT.SWITCH}auto_delivery"),
            InlineKeyboardButton(text=f"{flag(s.auto_bump_enabled)} Бамп", callback_data=f"{CBT.SWITCH}auto_bump"),
        ],
        [
            InlineKeyboardButton(text=f"{flag(s.auto_response_enabled)} Автоответ", callback_data=f"{CBT.SWITCH}auto_response"),
            InlineKeyboardButton(text=f"{flag(s.ai_replies_enabled)} Gemini", callback_data=f"{CBT.SWITCH}ai_replies"),
        ],
        [
            InlineKeyboardButton(text="🔌 Плагины", callback_data=CBT.PLUGINS),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data=CBT.SETTINGS),
        ],
        [InlineKeyboardButton(text="📦 Склад", callback_data=CBT.ADEL)],
    ]
    return build_keyboard(rows)


def premium_main_text(version: str) -> str:
    s = load_settings()
    starvell = "🟢" if s.is_starvell_configured() else "🔴"
    gemini = "🟢" if s.is_gemini_configured() else "🔴"
    return (
        f"🤖 <b>Starvell Cardinal</b> v{version}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Starvell {starvell}  ·  Gemini {gemini}\n"
        f"<i>Премиум-панель · ULTIMATE Edition</i>"
    )
