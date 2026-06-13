"""Клавиатуры Telegram (аналог static_keyboards.py + keyboards.py FPC)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Settings, load_settings
from tg_bot import cbt as CBT


def flag(on: bool) -> str:
    return "🟢" if on else "🔴"


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Меню", callback_data=CBT.MAIN)],
    ])


def settings_page1() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Глобальные", callback_data=f"{CBT.CATEGORY}main")],
        [InlineKeyboardButton(text="🔔 Уведомления", callback_data=f"{CBT.CATEGORY}tg")],
        [InlineKeyboardButton(text="💬 Автоответчик", callback_data=f"{CBT.CATEGORY}ar")],
        [InlineKeyboardButton(text="📦 Автовыдача", callback_data=f"{CBT.CATEGORY}ad")],
        [InlineKeyboardButton(text="🔌 Плагины", callback_data=CBT.PLUGINS)],
        [InlineKeyboardButton(text="📝 Шаблоны", callback_data=CBT.TMPLT_LIST)],
        [InlineKeyboardButton(text="▶️ Далее", callback_data=CBT.MAIN2)],
    ])


def settings_page2() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👋 Приветствие", callback_data=f"{CBT.CATEGORY}gr")],
        [InlineKeyboardButton(text="✅ Подтверждение заказа", callback_data=f"{CBT.CATEGORY}oc")],
        [InlineKeyboardButton(text="⭐ Ответы на отзывы", callback_data=f"{CBT.CATEGORY}rr")],
        [InlineKeyboardButton(text="🚫 Чёрный список", callback_data=f"{CBT.CATEGORY}bl")],
        [InlineKeyboardButton(text="🤖 Gemini", callback_data=CBT.GEMINI)],
        [InlineKeyboardButton(text="👤 Профиль Starvell", callback_data=CBT.PROFILE)],
        [InlineKeyboardButton(text="🛠 Настройка", callback_data=CBT.SETUP)],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=CBT.MAIN)],
    ])


def main_menu() -> InlineKeyboardMarkup:
    s = load_settings()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data=CBT.STATUS)],
        [
            InlineKeyboardButton(text=f"{flag(s.auto_delivery_enabled)} Выдача", callback_data=f"{CBT.SWITCH}auto_delivery"),
            InlineKeyboardButton(text=f"{flag(s.auto_bump_enabled)} Бамп", callback_data=f"{CBT.SWITCH}auto_bump"),
        ],
        [
            InlineKeyboardButton(text=f"{flag(s.auto_response_enabled)} Автоответ", callback_data=f"{CBT.SWITCH}auto_response"),
            InlineKeyboardButton(text=f"{flag(s.ai_replies_enabled)} Gemini", callback_data=f"{CBT.SWITCH}ai_replies"),
        ],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data=CBT.SETTINGS)],
        [InlineKeyboardButton(text="📦 Склад", callback_data=CBT.ADEL)],
    ])


def category_main(s: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{flag(s.auto_delivery_enabled)} Автовыдача", callback_data=f"{CBT.SWITCH}auto_delivery")],
        [InlineKeyboardButton(text=f"{flag(s.auto_bump_enabled)} Автоподнятие", callback_data=f"{CBT.SWITCH}auto_bump")],
        [InlineKeyboardButton(text=f"{flag(s.auto_response_enabled)} Автоответчик", callback_data=f"{CBT.SWITCH}auto_response")],
        [InlineKeyboardButton(text=f"{flag(s.auto_welcome_enabled)} Приветствие", callback_data=f"{CBT.SWITCH}auto_welcome")],
        [InlineKeyboardButton(text=f"{flag(s.auto_review_enabled)} Отзывы", callback_data=f"{CBT.SWITCH}auto_review")],
        [InlineKeyboardButton(text=f"{flag(s.order_confirm_enabled)} Подтв. заказа", callback_data=f"{CBT.SWITCH}order_confirm")],
        [InlineKeyboardButton(text=f"{flag(s.ai_replies_enabled)} Gemini в чатах", callback_data=f"{CBT.SWITCH}ai_replies")],
        [InlineKeyboardButton(text="◀️ Меню", callback_data=CBT.MAIN)],
    ])


def order_actions(order_id: str, chat_id: str = "") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💸 Возврат", callback_data=f"{CBT.REFUND_ORDER}{order_id}")],
    ]
    if chat_id:
        rows.append([InlineKeyboardButton(text="💬 Ответить", callback_data=f"{CBT.REPLY_CHAT}{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chat_actions(chat_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить в Starvell", callback_data=f"{CBT.REPLY_CHAT}{chat_id}")],
        [InlineKeyboardButton(text="📝 Шаблоны", callback_data=CBT.TMPLT_LIST)],
    ])


def setup_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Session Starvell", callback_data=CBT.SET_SESSION)],
        [InlineKeyboardButton(text="2️⃣ Gemini API", callback_data=CBT.SET_GEMINI)],
        [InlineKeyboardButton(text="3️⃣ Склад автовыдачи", callback_data=CBT.ADEL_ADD)],
        [InlineKeyboardButton(text="✅ Проверить Starvell", callback_data=CBT.CHECK_AUTH)],
        [InlineKeyboardButton(text="✅ Проверить Gemini", callback_data=CBT.CHECK_GEMINI)],
        [InlineKeyboardButton(text="◀️ Меню", callback_data=CBT.MAIN)],
    ])
