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
BTN_PAY = "💳 Оплатить"
BTN_PAY_SBP = "🏦 СБП (QR)"
BTN_PAY_CARD = "💳 Банк. карта"
BTN_PAY_CRYPTO = "🪙 Крипта"
BTN_RENEW = "♻️ Продлить"
BTN_DEVICES = "📲 Устройства"

# Быстрые суммы пополнения (как в мини-приложении Platega)
PAY_AMOUNT_BUTTONS: dict[str, int] = {
    "50 ₽": 5000,
    "100 ₽": 10000,
    "150 ₽": 15000,
    "500 ₽": 50000,
}

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
        .add(Text(BTN_PAY), color=KeyboardButtonColor.POSITIVE)
        .add(OpenLink(cabinet_path(cabinet_url, "subscription/purchase"), BTN_BUY))
        .row()
        .add(OpenLink(cabinet_path(cabinet_url, "balance"), BTN_BALANCE))
        .add(OpenLink(cabinet_path(cabinet_url, "referral"), BTN_REFERRAL))
        .row()
        .add(Text(BTN_PROMO), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_CABINET_LOGIN), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_APPS), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_GUIDE), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text(BTN_SUPPORT), color=KeyboardButtonColor.SECONDARY)
    )
    return kb.get_json()


def pay_methods_keyboard() -> str:
    """Выбор способа оплаты Platega — как в мини-приложении."""
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_PAY_SBP), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text(BTN_PAY_CARD), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_PAY_CRYPTO), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def pay_amounts_keyboard() -> str:
    """Быстрые суммы пополнения (50 / 100 / 150 / 500 ₽)."""
    labels = list(PAY_AMOUNT_BUTTONS.keys())
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text(labels[0]), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text(labels[1]), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text(labels[2]), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text(labels[3]), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text(BTN_PAY), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def payment_link_keyboard(payment_url: str) -> str:
    """Ссылка на оплату Platega + назад."""
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(payment_url, "🚀 Оплатить сейчас"))
        .row()
        .add(Text(BTN_PAY), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


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
