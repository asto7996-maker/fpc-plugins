"""
Клавиатуры VK-бота в стиле «Бедолага» / Paskod.
"""

from __future__ import annotations

from vkbottle import Callback, Keyboard, KeyboardButtonColor, OpenLink, Text

BTN_CONNECT = "🚀 Подключить VPN"
BTN_GET_KEY = "🔑 Получить ключ"
BTN_PROFILE = "👤 Мой профиль"
BTN_GUIDE = "📖 Инструкция"
BTN_SUPPORT = "💬 Поддержка"
BTN_BACK = "◀️ Назад в меню"
BTN_RENEW = "💳 Продлить подписку"
BTN_MY_KEY = "📋 Показать мой ключ"
BTN_CABINET = "🌐 Личный кабинет"

OS_IOS = "ios"
OS_ANDROID = "android"
OS_WINDOWS = "windows"
OS_MACOS = "macos"


def main_menu_keyboard(cabinet_url: str | None = None) -> str:
    kb = (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_CONNECT), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_GET_KEY), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text(BTN_PROFILE), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_GUIDE), color=KeyboardButtonColor.SECONDARY)
        .row()
    )
    if cabinet_url:
        kb.add(OpenLink(cabinet_url, BTN_CABINET))
        kb.add(Text(BTN_SUPPORT), color=KeyboardButtonColor.SECONDARY)
    else:
        kb.add(Text(BTN_SUPPORT), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def connect_keyboard(
    has_active: bool = False,
    trial_available: bool = True,
    cabinet_url: str | None = None,
) -> str:
    kb = Keyboard(one_time=False, inline=False)

    if has_active:
        kb.add(Text(BTN_MY_KEY), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
    elif trial_available:
        kb.add(Text(BTN_GET_KEY), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
    else:
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.POSITIVE)

    if cabinet_url:
        kb.row()
        kb.add(OpenLink(cabinet_url, BTN_CABINET))

    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def profile_keyboard(cabinet_url: str | None = None) -> str:
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text(BTN_MY_KEY), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
    if cabinet_url:
        kb.row()
        kb.add(OpenLink(cabinet_url, BTN_CABINET))
    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def guide_os_keyboard() -> str:
    kb = (
        Keyboard(inline=True)
        .add(Callback("📱 iOS", payload={"cmd": "guide", "os": OS_IOS}))
        .add(Callback("🤖 Android", payload={"cmd": "guide", "os": OS_ANDROID}))
        .row()
        .add(Callback("💻 Windows", payload={"cmd": "guide", "os": OS_WINDOWS}))
        .add(Callback("🍎 macOS", payload={"cmd": "guide", "os": OS_MACOS}))
    )
    return kb.get_json()


def support_keyboard(support_url: str, cabinet_url: str | None = None) -> str:
    kb = Keyboard(one_time=False, inline=False)
    kb.add(OpenLink(support_url, "✉️ Написать в поддержку"))
    if cabinet_url:
        kb.row()
        kb.add(OpenLink(cabinet_url, BTN_CABINET))
    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def back_keyboard() -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )
