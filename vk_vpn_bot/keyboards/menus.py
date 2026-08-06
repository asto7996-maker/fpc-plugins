"""
Клавиатуры VK-бота — компактный интерфейс (до 5 кнопок на экран).

Главное меню: 6 пунктов + админ. Вложенные экраны — не больше 5 кнопок.
Тарифы и документы — inline, чтобы не раздувать reply-клавиатуру.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vkbottle import Callback, Keyboard, KeyboardButtonColor, OpenLink, Text

if TYPE_CHECKING:
    from services.catalog import Offer

# ---- Главное меню ----
BTN_CONNECT = "🚀 Подключиться"
BTN_SUBSCRIPTION = "📦 Подписка"
BTN_TARIFFS = "💎 Тарифы"
BTN_PAY = "💳 Оплатить"
BTN_HELP = "📖 Помощь"
BTN_CABINET = "🌐 Кабинет"
BTN_BACK = "◀️ Назад"

# ---- Вложенные действия ----
BTN_TRIAL = "🎁 Триал"
BTN_MY_KEY = "🔑 Ключ"
BTN_RENEW = "♻️ Продлить"
BTN_ASK = "✍️ Вопрос"
BTN_GUIDE = "📖 Гайд"
BTN_INFO = "ℹ️ Документы"
BTN_PROMO = "🎟 Промо"

# Оплата
BTN_PAY_SBP = "🏦 СБП"
BTN_PAY_CARD = "💳 Карта"
BTN_PAY_CRYPTO = "🪙 Крипта"

# Админ
BTN_ADMIN = "🛠 Админ"
BTN_ADMIN_PANEL = "📊 Панель"
BTN_ADMIN_USERS = "👥 Юзеры"
BTN_ADMIN_TICKETS = "🎫 Тикеты"

# Документы (inline)
BTN_PRIVACY = "🛡️ Приватность"
BTN_TERMS = "📜 Соглашение"
BTN_OFFER = "📄 Оферта"
BTN_RULES = "📋 Правила"
BTN_FAQ = "💬 FAQ"
BTN_DOC_PREV = "⬅️"
BTN_DOC_NEXT = "➡️"
BTN_DOC_LIST = "📚 Документы"

PAY_AMOUNT_BUTTONS: dict[str, int] = {
    "50 ₽": 5000,
    "100 ₽": 10000,
    "150 ₽": 15000,
    "500 ₽": 50000,
}

# Совместимость со старыми подписями VK-клавиатур и текстовых команд
BTN_BUY = BTN_TARIFFS
BTN_BALANCE = BTN_PAY
BTN_SUPPORT = BTN_HELP
BTN_APPS = BTN_GUIDE
BTN_GET_KEY = BTN_MY_KEY
BTN_CONNECT_OLD = "🚀 Подключить VPN"
BTN_CABINET_LOGIN = "🔐 Войти"
BTN_PROFILE = "👤 Профиль"
BTN_REFERRAL = "👥 Рефералка"
BTN_DEVICES = "📲 Устройства"


def cabinet_path(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def legal_url(cabinet_url: str, slug: str) -> str:
    return cabinet_path(cabinet_url, f"legal/{slug}.html")


def main_menu_keyboard(cabinet_url: str, *, show_admin: bool = False) -> str:
    """6 кнопок: подключение, подписка, тарифы, оплата, помощь, кабинет."""
    _ = cabinet_url
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text(BTN_CONNECT), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text(BTN_SUBSCRIPTION), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text(BTN_TARIFFS), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text(BTN_PAY), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text(BTN_HELP), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text(BTN_CABINET), color=KeyboardButtonColor.SECONDARY)
    if show_admin:
        kb.row()
        kb.add(Text(BTN_ADMIN), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def help_keyboard(support_url: str = "") -> str:
    """Хаб помощи: вопрос, гайд, документы, рефералка, промо."""
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text(BTN_ASK), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text(BTN_GUIDE), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text(BTN_INFO), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text(BTN_REFERRAL), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text(BTN_PROMO), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    if support_url:
        kb.row()
        kb.add(OpenLink(support_url, "💌 Поддержка"))
    return kb.get_json()


def info_inline_keyboard() -> str:
    """Документы — inline, reply остаётся только «Назад»."""
    return (
        Keyboard(inline=True)
        .add(Callback(BTN_PRIVACY, payload={"cmd": "doc", "slug": "privacy", "page": 1}))
        .add(Callback(BTN_TERMS, payload={"cmd": "doc", "slug": "terms", "page": 1}))
        .row()
        .add(Callback(BTN_OFFER, payload={"cmd": "doc", "slug": "offer", "page": 1}))
        .add(Callback(BTN_RULES, payload={"cmd": "doc", "slug": "rules", "page": 1}))
        .row()
        .add(Callback(BTN_FAQ, payload={"cmd": "doc", "slug": "faq", "page": 1}))
        .get_json()
    )


def info_keyboard(cabinet_url: str = "") -> str:
    """Reply для раздела документов: назад + inline-список отдельным сообщением."""
    _ = cabinet_url
    return back_keyboard()


def doc_nav_keyboard(*, page: int, total: int, slug: str) -> str:
    """Inline-листание документа."""
    kb = Keyboard(inline=True)
    if page > 1:
        kb.add(
            Callback(
                BTN_DOC_PREV,
                payload={"cmd": "doc", "slug": slug, "page": page - 1},
            )
        )
    if page < total:
        kb.add(
            Callback(
                BTN_DOC_NEXT,
                payload={"cmd": "doc", "slug": slug, "page": page + 1},
            )
        )
    if page > 1 or page < total:
        kb.row()
    kb.add(Callback(BTN_DOC_LIST, payload={"cmd": "info"}))
    return kb.get_json()


def doc_keyboard(cabinet_url: str, slug: str, *, page: int, total: int) -> str:
    _ = cabinet_url
    return doc_nav_keyboard(page=page, total=total, slug=slug)


def tariffs_inline_keyboard(offers: list[Offer]) -> str:
    """Тарифы inline — не занимают reply-клавиатуру."""
    kb = Keyboard(inline=True)
    for i, offer in enumerate(offers[:10]):
        if i > 0 and i % 2 == 0:
            kb.row()
        short = f"{offer.tariff.emoji} {offer.period.price_label}"
        kb.add(
            Callback(
                short,
                payload={
                    "cmd": "tariff",
                    "tid": offer.tariff.id,
                    "days": offer.period.days,
                },
            )
        )
    kb.row()
    kb.add(Callback("🌐 В кабинет", payload={"cmd": "cabinet", "path": "/subscription/purchase"}))
    return kb.get_json()


def tariffs_keyboard(offer_labels: list[str]) -> str:
    """Fallback reply, если inline недоступен."""
    kb = Keyboard(one_time=False, inline=False)
    for label in offer_labels[:4]:
        kb.add(Text(label), color=KeyboardButtonColor.PRIMARY)
        kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def pay_methods_keyboard() -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_PAY_SBP), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_PAY_CARD), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_PAY_CRYPTO), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def pay_amounts_keyboard() -> str:
    labels = list(PAY_AMOUNT_BUTTONS.keys())
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(labels[0]), color=KeyboardButtonColor.POSITIVE)
        .add(Text(labels[1]), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text(labels[2]), color=KeyboardButtonColor.PRIMARY)
        .add(Text(labels[3]), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def payment_link_keyboard(payment_url: str) -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(OpenLink(payment_url, "✨ Оплатить"))
        .row()
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
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
    elif trial_available:
        kb.add(Text(BTN_TRIAL), color=KeyboardButtonColor.POSITIVE)
    else:
        kb.add(Text(BTN_TARIFFS), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text(BTN_PAY), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def subscription_keyboard(cabinet_url: str, has_key: bool = False) -> str:
    _ = cabinet_url
    kb = Keyboard(one_time=False, inline=False)
    if has_key:
        kb.add(Text(BTN_MY_KEY), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text(BTN_RENEW), color=KeyboardButtonColor.PRIMARY)
    else:
        kb.add(Text(BTN_TARIFFS), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text(BTN_PAY), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def referral_keyboard(cabinet_url: str = "") -> str:
    """Экран рефералки: кабинет + назад."""
    _ = cabinet_url
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_CABINET), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_BACK), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def promo_keyboard(cabinet_url: str) -> str:
    _ = cabinet_url
    return back_keyboard()


def apps_keyboard(cabinet_url: str) -> str:
    _ = cabinet_url
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_GUIDE), color=KeyboardButtonColor.POSITIVE)
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
        .row()
        .add(Callback("❓ Не работает", payload={"cmd": "guide", "os": "trouble"}))
        .get_json()
    )


def support_keyboard(support_url: str, cabinet_url: str) -> str:
    """Совместимость: перенаправляет в хаб помощи."""
    _ = cabinet_url
    return help_keyboard(support_url)


def support_wait_keyboard() -> str:
    return back_keyboard()


def admin_keyboard() -> str:
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_ADMIN_PANEL), color=KeyboardButtonColor.POSITIVE)
        .add(Text(BTN_ADMIN_USERS), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_ADMIN_TICKETS), color=KeyboardButtonColor.SECONDARY)
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
