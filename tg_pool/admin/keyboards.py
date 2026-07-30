"""Inline keyboards for the admin control panel."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tg_pool.db.models import Account, AccountStatus

STATUS_ICON = {
    AccountStatus.active: "🟢",
    AccountStatus.flood_wait: "🟡",
    AccountStatus.banned: "🔴",
    AccountStatus.paused: "⏸",
    AccountStatus.spambot: "🚫",
}


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Accounts", callback_data="menu:accounts"),
                InlineKeyboardButton(text="➕ Add account", callback_data="menu:add_account"),
            ],
            [
                InlineKeyboardButton(text="🛡 Proxies", callback_data="menu:proxies"),
                InlineKeyboardButton(text="➕ Add proxy", callback_data="menu:add_proxy"),
            ],
            [
                InlineKeyboardButton(
                    text="🧪 SpamBot check all",
                    callback_data="menu:spambot_all",
                ),
            ],
        ]
    )


def accounts_kb(accounts: list[Account]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for acc in accounts:
        icon = STATUS_ICON.get(acc.status, "❓")
        label = f"{icon} #{acc.id} {acc.phone_number} [{acc.status.value}]"
        rows.append(
            [InlineKeyboardButton(text=label[:64], callback_data=f"acc:{acc.id}")]
        )
    rows.append([InlineKeyboardButton(text="◀️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_actions_kb(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Activate",
                    callback_data=f"accact:{account_id}:activate",
                ),
                InlineKeyboardButton(
                    text="⏸ Pause",
                    callback_data=f"accact:{account_id}:pause",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧪 SpamBot",
                    callback_data=f"accact:{account_id}:spambot",
                ),
                InlineKeyboardButton(
                    text="🏓 Ping",
                    callback_data=f"accact:{account_id}:ping",
                ),
            ],
            [InlineKeyboardButton(text="◀️ Back", callback_data="menu:accounts")],
        ]
    )
