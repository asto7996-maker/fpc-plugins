"""Inline + reply keyboards for the control panel."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from tg_pool.db.models import Account, AccountStatus

STATUS_ICON = {
    AccountStatus.active: "🟢",
    AccountStatus.flood_wait: "🟡",
    AccountStatus.banned: "🔴",
    AccountStatus.paused: "⏸",
    AccountStatus.spambot: "🚫",
}


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def nav_row(*extra: InlineKeyboardButton) -> list[InlineKeyboardButton]:
    row = list(extra)
    row.append(_btn("🔄 Обновить", "nav:refresh"))
    row.append(_btn("⬅️ Назад", "nav:back"))
    return row


def main_menu_kb(*, is_creator: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [_btn("🚀 Мои Аккаунты", "menu:accounts"), _btn("➕ Добавить аккаунт", "menu:add")],
        [_btn("🌐 Прокси", "menu:proxies"), _btn("📊 Статистика", "menu:stats")],
        [_btn("🤖 Gemini / Черновики", "menu:gemini")],
        [_btn("👤 Профиль", "menu:profile"), _btn("📖 Справка", "menu:help")],
    ]
    if is_creator:
        rows.append([_btn("🔑 Доступ / Admin", "menu:access")])
    rows.append([_btn("🔄 Обновить", "menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reply_menu_kb(*, is_creator: bool = False) -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard so controls are always visible."""
    rows = [
        [KeyboardButton(text="🏠 Меню"), KeyboardButton(text="🚀 Аккаунты")],
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="🌐 Прокси")],
        [KeyboardButton(text="🤖 Gemini"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="📖 Справка")],
    ]
    if is_creator:
        rows.append([KeyboardButton(text="🔑 Admin")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел или команду…",
    )


def add_account_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📦 TData ZIP", "add:tdata"), _btn("🧬 StringSession", "add:session")],
            nav_row(),
        ]
    )


def accounts_kb(accounts: list[Account]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for acc in accounts:
        icon = STATUS_ICON.get(acc.status, "❓")
        label = f"{icon} #{acc.id} {acc.phone_number}"
        rows.append([_btn(label[:64], f"acc:{acc.id}")])
    rows.append(nav_row(_btn("🏠 Меню", "menu:home")))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_actions_kb(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("▶️ Старт", f"accact:{account_id}:activate"),
                _btn("⏸ Пауза", f"accact:{account_id}:pause"),
            ],
            [
                _btn("🧪 SpamBot", f"accact:{account_id}:spambot"),
                _btn("🏓 Ping", f"accact:{account_id}:ping"),
            ],
            [_btn("🤖 Assistant On/Off", f"drafts:assistant:{account_id}")],
            nav_row(_btn("📋 К списку", "menu:accounts")),
        ]
    )


def draft_card_kb(draft_id: int, *, auto_on: bool = False) -> InlineKeyboardMarkup:
    auto_label = "⚡️ Авто-режим: ON" if auto_on else "⚡️ Авто-режим: OFF"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("🚀 Отправить сейчас", f"draft:send:{draft_id}"),
                _btn("✏️ Изменить", f"draft:edit:{draft_id}"),
            ],
            [
                _btn("❌ Отклонить", f"draft:reject:{draft_id}"),
                _btn(auto_label, f"draft:auto:{draft_id}"),
            ],
            nav_row(_btn("🏠 Меню", "menu:home")),
        ]
    )


def drafts_settings_kb(settings) -> InlineKeyboardMarkup:
    mon = "🟢 Мониторинг: ON" if settings.enabled else "🔴 Мониторинг: OFF"
    auto = "⚡️ Авто: ON" if settings.auto_approve_enabled else "⚡️ Авто: OFF"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(mon, "drafts:toggle_enabled"), _btn(auto, "drafts:toggle_auto")],
            [_btn("📥 Pending черновики", "drafts:pending")],
            [
                _btn("🔑 API key", "drafts:set_key"),
                _btn("📣 Username", "drafts:set_promote"),
            ],
            nav_row(_btn("🏠 Меню", "menu:home")),
        ]
    )


def proxies_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("➕ Добавить прокси", "proxy:add")],
            nav_row(_btn("🏠 Меню", "menu:home")),
        ]
    )


def stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[nav_row(_btn("🏠 Меню", "menu:home"))]
    )


def profile_kb(*, is_creator: bool = False) -> InlineKeyboardMarkup:
    rows = [nav_row(_btn("🏠 Меню", "menu:home"))]
    if is_creator:
        rows.insert(0, [_btn("🛡 Admin", "menu:access")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def access_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🆕 Создать инвайт", "access:create")],
            [
                _btn("⏳ Активные", "access:list_active"),
                _btn("✅ Использованные", "access:list_used"),
            ],
            [_btn("👥 Пользователи", "access:users")],
            nav_row(_btn("🏠 Меню", "menu:home")),
        ]
    )


def tdata_success_kb(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("👤 Открыть", f"acc:{account_id}"),
                _btn("🧪 SpamBot", f"accact:{account_id}:spambot"),
            ],
            [_btn("🏠 Меню", "menu:home")],
        ]
    )


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("🏠 Меню", "menu:home"), _btn("⬅️ Назад", "nav:back")]]
    )
