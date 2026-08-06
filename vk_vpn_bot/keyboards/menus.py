"""
Клавиатуры VK-бота — выразительный интерфейс Paskod с эмодзи.
Документы читаются прямо в боте (reply-клавиатура), без зависимости от мини-приложения.
"""

from __future__ import annotations

from vkbottle import Callback, Keyboard, KeyboardButtonColor, OpenLink, Text

# ---- Кнопки: эмодзи + короткий ясный текст ----
BTN_CONNECT = "🚀 Подключиться"
BTN_TRIAL = "🎁 Бесплатный триал"
BTN_SUBSCRIPTION = "📦 Подписка"
BTN_BUY = "💎 Купить"
BTN_BALANCE = "💰 Баланс"
BTN_REFERRAL = "👥 Партнёрам"
BTN_PROMO = "🎟 Промокод"
BTN_APPS = "📱 Приложения"
BTN_GUIDE = "📖 Гайд"
BTN_SUPPORT = "💬 Помощь"
BTN_INFO = "ℹ️ Инфо"
BTN_PROFILE = "👤 Профиль"
BTN_MY_KEY = "🔑 Мой ключ"
BTN_BACK = "◀️ Назад"
BTN_CABINET = "🌐 Кабинет"
BTN_CABINET_LOGIN = "🔐 Войти"
BTN_PAY = "💳 Оплатить"
BTN_PAY_SBP = "🏦 СБП · QR"
BTN_PAY_CARD = "💳 Карта"
BTN_PAY_CRYPTO = "🪙 Крипта"
BTN_RENEW = "♻️ Продлить"
BTN_DEVICES = "📲 Устройства"

# Документы (всё в VK-боте)
BTN_PRIVACY = "🛡️ Приватность"
BTN_TERMS = "📜 Соглашение"
BTN_OFFER = "📄 Оферта"
BTN_RULES = "📋 Правила"
BTN_FAQ = "💬 FAQ"
BTN_DOC_PREV = "⬅️ Назад"
BTN_DOC_NEXT = "➡️ Далее"
BTN_DOC_LIST = "📚 К документам"

PAY_AMOUNT_BUTTONS: dict[str, int] = {
    "💵 50 ₽": 5000,
    "💵 100 ₽": 10000,
    "💎 150 ₽": 15000,
    "💎 500 ₽": 50000,
}

# Совместимость со старыми подписями клавиатур VK
BTN_GET_KEY = "🔑 Получить ключ"
BTN_CONNECT_OLD = "🚀 Подключить VPN"


def cabinet_path(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def legal_url(cabinet_url: str, slug: str) -> str:
    """URL документа (опционально, если развёрнута статика /legal)."""
    return cabinet_path(cabinet_url, f"legal/{slug}.html")


def main_menu_keyboard(cabinet_url: str) -> str:
    """Главное меню: документы сразу видны (кнопка Инфо)."""
    _ = cabinet_url
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_CONNECT), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_SUBSCRIPTION), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text(BTN_PAY), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_BUY), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_CABINET), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_BALANCE), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_INFO), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_SUPPORT), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_APPS), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_GUIDE), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text(BTN_PROMO), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_REFERRAL), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def info_keyboard(cabinet_url: str = "") -> str:
    """Раздел документов — только кнопки VK, тексты внутри бота."""
    _ = cabinet_url
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_PRIVACY), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_TERMS), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text(BTN_OFFER), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_RULES), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_FAQ), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def doc_nav_keyboard(*, page: int, total: int) -> str:
    """Листание документа прямо в боте (reply-кнопки)."""
    kb = Keyboard(one_time=False, inline=False)
    if page > 1:
        kb.add(Text(BTN_DOC_PREV), color=KeyboardButtonColor.PRIMARY)
    if page < total:
        kb.add(Text(BTN_DOC_NEXT), color=KeyboardButtonColor.POSITIVE)
    if page > 1 or page < total:
        kb.row()
    kb.add(Text(BTN_DOC_LIST), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def doc_keyboard(cabinet_url: str, slug: str, *, page: int, total: int) -> str:
    """Совместимость: inline-навигация (если нужна)."""
    _ = cabinet_url
    kb = Keyboard(inline=True)
    if page > 1:
        kb.add(Callback("⬅️", payload={"cmd": "doc", "slug": slug, "page": page - 1}))
    if page < total:
        kb.add(Callback("➡️", payload={"cmd": "doc", "slug": slug, "page": page + 1}))
    if page > 1 or page < total:
        kb.row()
    kb.add(Callback("📚 К документам", payload={"cmd": "info"}))
    return kb.get_json()


def pay_methods_keyboard() -> str:
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
    labels = list(PAY_AMOUNT_BUTTONS.keys())
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text(labels[0]), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text(labels[1]), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text(labels[2]), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text(labels[3]), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def payment_link_keyboard(payment_url: str) -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(payment_url, "✨ Оплатить сейчас"))
        .row()
        .add(Text(BTN_PAY), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def auto_login_keyboard(
    auto_login_url: str,
    *,
    button_text: str = "✨ Открыть",
) -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(auto_login_url, button_text))
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
    _ = cabinet_url
    kb = Keyboard(one_time=False, inline=False)

    if has_active:
        kb.add(Text(BTN_MY_KEY), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
        kb.add(Text(BTN_CABINET), color=KeyboardButtonColor.PRIMARY)
    elif trial_available:
        kb.add(Text(BTN_TRIAL), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text(BTN_BUY), color=KeyboardButtonColor.PRIMARY)
    else:
        kb.add(Text(BTN_BUY), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)

    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def subscription_keyboard(cabinet_url: str, has_key: bool = False) -> str:
    _ = cabinet_url
    kb = Keyboard(one_time=False, inline=False)
    if has_key:
        kb.add(Text(BTN_MY_KEY), color=KeyboardButtonColor.POSITIVE)
        kb.row()
    kb.add(Text(BTN_CABINET), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text(BTN_BUY), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text(BTN_PAY), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def promo_keyboard(cabinet_url: str) -> str:
    _ = cabinet_url
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_CABINET), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def apps_keyboard(cabinet_url: str) -> str:
    _ = cabinet_url
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_GUIDE), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_CABINET), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def guide_os_keyboard() -> str:
    return (
        Keyboard(inline=True)
        .add(Callback("🍎 iOS", payload={"cmd": "guide", "os": "ios"}))
        .add(Callback("🤖 Android", payload={"cmd": "guide", "os": "android"}))
        .row()
        .add(Callback("💻 Windows", payload={"cmd": "guide", "os": "windows"}))
        .add(Callback("🖥 macOS", payload={"cmd": "guide", "os": "macos"}))
        .get_json()
    )


def support_keyboard(support_url: str, cabinet_url: str) -> str:
    _ = cabinet_url
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(support_url, "💌 Написать нам"))
        .row()
        .add(Text(BTN_INFO), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def back_keyboard() -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )
