"""
Клавиатуры VK-бота в стиле «Бедолага».

Используем постоянную (не one_time) клавиатуру с Text-кнопками —
это аналог Reply Keyboard в Telegram: удобно и привычно пользователю.
Для выбора ОС — inline-клавиатура с Callback.
"""

from __future__ import annotations

from vkbottle import Callback, Keyboard, KeyboardButtonColor, OpenLink, Text

# ---- Тексты кнопок главного меню (сверяем в обработчиках) ----
BTN_CONNECT = "🚀 Подключить VPN"
BTN_GET_KEY = "🔑 Получить ключ"
BTN_PROFILE = "👤 Мой профиль"
BTN_GUIDE = "📖 Инструкция"
BTN_SUPPORT = "💬 Поддержка"
BTN_BACK = "◀️ Назад в меню"
BTN_RENEW = "💳 Продлить подписку"
BTN_MY_KEY = "📋 Показать мой ключ"

# ОС для инструкции
OS_IOS = "ios"
OS_ANDROID = "android"
OS_WINDOWS = "windows"
OS_MACOS = "macos"


def main_menu_keyboard() -> str:
    """
    Главное меню бота.
    one_time=False — клавиатура остаётся под полем ввода.
    """
    kb = (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_CONNECT), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_GET_KEY), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text(BTN_PROFILE), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_GUIDE), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text(BTN_SUPPORT), color=KeyboardButtonColor.SECONDARY)
    )
    return kb.get_json()


def connect_keyboard(has_active: bool = False, trial_available: bool = True) -> str:
    """Клавиатура экрана подключения / получения ключа."""
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

    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def profile_keyboard() -> str:
    """Клавиатура профиля."""
    kb = (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_MY_KEY), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    )
    return kb.get_json()


def guide_os_keyboard() -> str:
    """
    Inline-клавиатура выбора ОС для инструкции.
    Callback-кнопки обрабатываются через MESSAGE_EVENT.
    """
    kb = (
        Keyboard(inline=True)
        .add(Callback("📱 iOS", payload={"cmd": "guide", "os": OS_IOS}))
        .add(Callback("🤖 Android", payload={"cmd": "guide", "os": OS_ANDROID}))
        .row()
        .add(Callback("💻 Windows", payload={"cmd": "guide", "os": OS_WINDOWS}))
        .add(Callback("🍎 macOS", payload={"cmd": "guide", "os": OS_MACOS}))
    )
    return kb.get_json()


def support_keyboard(support_url: str) -> str:
    """Клавиатура поддержки: ссылка + возврат в меню."""
    kb = (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(support_url, "✉️ Написать в поддержку"))
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    )
    return kb.get_json()


def back_keyboard() -> str:
    """Простая клавиатура «назад»."""
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )
