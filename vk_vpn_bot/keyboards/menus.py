"""
Клавиатуры VK-бота — паритет с Telegram Bedolaga / Paskod.

В Telegram часть кнопок — WebApp в кабинет.
В VK то же самое через OpenLink на разделы cabinet.paskod.ru.
"""

from __future__ import annotations

from vkbottle import Callback, Keyboard, KeyboardButtonColor, OpenLink, Text

# ---- Тексты кнопок (как в Telegram-боте) ----
BTN_CONNECT = "🚀 Подключиться"
BTN_TRIAL = "🎁 Активировать триал"
BTN_SUBSCRIPTION = "📦 Моя подписка"
BTN_BUY = "💳 Купить"
BTN_BALANCE = "💰 Баланс"
BTN_REFERRAL = "👥 Партнёрка"
BTN_PROMO = "🎟 Промокод"
BTN_APPS = "📱 Приложения"
BTN_GUIDE = "📖 Инструкция"
BTN_SUPPORT = "💬 Поддержка"
BTN_PROFILE = "👤 Профиль"
BTN_MY_KEY = "🔑 Мой ключ"
BTN_BACK = "◀️ Назад в меню"
BTN_CABINET = "🌐 Кабинет"
BTN_CABINET_LOGIN = "🔐 Войти без регистрации"
BTN_RENEW = "♻️ Продлить"
BTN_DEVICES = "📲 Устройства"

# Совместимость со старыми подписями
BTN_GET_KEY = "🔑 Получить ключ"
BTN_CONNECT_OLD = "🚀 Подключить VPN"


def cabinet_path(base: str, path: str) -> str:
    """Собирает URL раздела кабинета."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def main_menu_keyboard(cabinet_url: str) -> str:
    """
    Главное меню как у Bedolaga (default/cabinet hybrid):
    действия в боте + ссылки в веб-кабинет.
    """
    kb = (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_CONNECT), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_SUBSCRIPTION), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(OpenLink(cabinet_path(cabinet_url, "subscription/purchase"), BTN_BUY))
        .add(OpenLink(cabinet_path(cabinet_url, "balance"), BTN_BALANCE))
        .row()
        .add(OpenLink(cabinet_path(cabinet_url, "referral"), BTN_REFERRAL))
        .add(Text(BTN_PROMO), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_APPS), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_GUIDE), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text(BTN_SUPPORT), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_CABINET_LOGIN), color=KeyboardButtonColor.PRIMARY)
    )
    return kb.get_json()


def auto_login_keyboard(auto_login_url: str) -> str:
    """Клавиатура с одноразовой ссылкой входа (как Telegram WebApp auth)."""
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(auto_login_url, "🚀 Открыть кабинет"))
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def connect_keyboard(
    *,
    has_active: bool,
    trial_available: bool,
    cabinet_url: str,
) -> str:
    kb = Keyboard(one_time=False, inline=False)

    if has_active:
        kb.add(Text(BTN_MY_KEY), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
        kb.add(OpenLink(cabinet_path(cabinet_url, "subscription"), "⚡ Открыть подписку"))
    elif trial_available:
        kb.add(Text(BTN_TRIAL), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(OpenLink(cabinet_path(cabinet_url, "subscription/purchase"), BTN_BUY))
    else:
        kb.add(OpenLink(cabinet_path(cabinet_url, "subscription/purchase"), BTN_BUY))
        kb.row()
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)

    kb.row()
    kb.add(OpenLink(cabinet_path(cabinet_url, "subscription"), "📲 Устройства / Happ"))
    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def subscription_keyboard(cabinet_url: str, has_key: bool = False) -> str:
    kb = Keyboard(one_time=False, inline=False)
    if has_key:
        kb.add(Text(BTN_MY_KEY), color=KeyboardButtonColor.POSITIVE)
        kb.row()
    kb.add(OpenLink(cabinet_path(cabinet_url, "subscription"), "⚡ Кабинет подписки"))
    kb.add(OpenLink(cabinet_path(cabinet_url, "subscription/purchase"), BTN_BUY))
    kb.row()
    kb.add(OpenLink(cabinet_path(cabinet_url, "balance"), BTN_BALANCE))
    kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def promo_keyboard(cabinet_url: str) -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(cabinet_path(cabinet_url, "subscription"), "🎟 Ввести в кабинете"))
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def apps_keyboard(cabinet_url: str) -> str:
    """Ссылки на приложения / кабинет (Happ)."""
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(cabinet_path(cabinet_url, "subscription"), "⬇️ Скачать Happ"))
        .row()
        .add(Text(BTN_GUIDE), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def guide_os_keyboard() -> str:
    kb = (
        Keyboard(inline=True)
        .add(Callback("📱 iOS", payload={"cmd": "guide", "os": "ios"}))
        .add(Callback("🤖 Android", payload={"cmd": "guide", "os": "android"}))
        .row()
        .add(Callback("💻 Windows", payload={"cmd": "guide", "os": "windows"}))
        .add(Callback("🍎 macOS", payload={"cmd": "guide", "os": "macos"}))
    )
    return kb.get_json()


def support_keyboard(support_url: str, cabinet_url: str) -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(support_url, "✉️ Написать в поддержку"))
        .row()
        .add(OpenLink(cabinet_url, BTN_CABINET))
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def back_keyboard() -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )
