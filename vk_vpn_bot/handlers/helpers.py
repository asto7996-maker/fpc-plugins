"""
Тексты сообщений — выразительный тон Paskod с эмодзи и чёткой структурой.
"""

from __future__ import annotations

from datetime import datetime

from config import Settings
from database.models import User
from handlers.style import (
    brand,
    bullet,
    card_rule,
    error_banner,
    footer_hint,
    header,
    kv,
    soft_rule,
    step,
    subhead,
    success_banner,
    warn_banner,
)
from legal.documents import ALL_DOCS
from services.vpn_keys import mask_key


def format_auto_login_message(
    url: str,
    settings: Settings,
    *,
    redirect: str | None = "/",
) -> str:
    where = {
        "/": "кабинет",
        "/subscription": "подписку",
        "/subscription/purchase": "покупку",
        "/balance": "баланс",
        "/referral": "партнёрку",
        "/balance/top-up": "пополнение",
        "/legal/index.html": "документы",
        "/info": "инфо",
    }.get(redirect or "/", redirect or "кабинет")

    return (
        f"{header('🔐', 'Вход без регистрации')}\n\n"
        f"Аккаунт уже готов — как в Telegram Mini App.\n"
        f"Откройте {where}: email и пароль не нужны.\n\n"
        f"{bullet('Ссылка действует 72 часа')}\n"
        f"{bullet('Привязана к вашему VK')}\n\n"
        f"{soft_rule()}\n"
        f"Если кнопка не сработала:\n{url}"
    )


def format_welcome(settings: Settings, first_name: str | None = None) -> str:
    name = (first_name or "").strip()
    hello = f"Привет, {name}!" if name else "Привет!"
    bot = brand(settings.bot_name) if settings.bot_name.isascii() else settings.bot_name

    return (
        f"✨  {hello}\n"
        f"{card_rule()}\n\n"
        f"Я — {bot}\n"
        f"быстрый VPN без лишней суеты.\n\n"
        f"{bullet(f'Триал {settings.trial_days} дня для новых')}\n"
        f"{bullet('Оплата СБП · карта · крипта')}\n"
        f"{bullet('Кабинет без регистрации')}\n"
        f"{bullet('Документы — кнопка «ℹ️ Инфо»')}\n\n"
        f"{soft_rule()}\n"
        f"{footer_hint('меню внизу экрана')}"
    )


def format_subscription_card(
    user: User, settings: Settings, panel: dict | None = None
) -> str:
    if user.is_subscription_active() and user.subscription_end:
        end = user.subscription_end
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        status = f"✅ активна до {end.strftime('%d.%m.%Y')}"
    else:
        status = "⛔️ не активна"

    trial = "уже использован" if user.is_trial_used else "🎁 доступен"
    key_preview = mask_key(user.vpn_key) if user.vpn_key else "ещё не выдан"

    lines = [
        header("📦", "Моя подписка"),
        "",
        kv("Статус", status),
        kv("Триал", trial),
        kv("Ключ", key_preview),
    ]

    if panel:
        sub = (
            panel.get("subscription")
            if isinstance(panel.get("subscription"), dict)
            else None
        )
        if sub:
            if sub.get("tariff_name"):
                lines.append(kv("Тариф", str(sub.get("tariff_name"))))
            if sub.get("traffic_limit_gb") is not None:
                used = sub.get("traffic_used_gb", 0)
                limit = sub.get("traffic_limit_gb")
                lines.append(kv("Трафик", f"{used} / {limit} ГБ"))
            if sub.get("device_limit") is not None:
                lines.append(kv("Устройства", f"до {sub.get('device_limit')}"))
        bal = panel.get("balance_rubles")
        if bal is not None:
            lines.append(kv("Баланс", f"{bal} ₽"))

    lines.append("")
    lines.append(soft_rule())
    lines.append(f"🌐  Полный кабинет: {settings.cabinet_url}/subscription")
    return "\n".join(lines)


def format_profile(user: User, settings: Settings) -> str:
    return format_subscription_card(user, settings)


def format_connect_screen(user: User, settings: Settings) -> str:
    if user.is_subscription_active():
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner('Подписка уже активна')}\n\n"
            f"Откройте ключ или кабинет — и подключайтесь в Happ.\n\n"
            f"{footer_hint()}"
        )
    if not user.is_trial_used:
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner(f'Вам доступен триал на {settings.trial_days} дня')}\n\n"
            f"Один тап — и пришлём ссылку для Happ.\n\n"
            f"{footer_hint('активируйте триал')}"
        )
    return (
        f"{header('🚀', 'Подключение')}\n\n"
        f"{warn_banner('Триал уже использован')}\n\n"
        f"Можно купить тариф или пополнить баланс.\n\n"
        f"{footer_hint()}"
    )


def format_key_message(
    key: str,
    settings: Settings,
    *,
    is_trial: bool,
    source: str = "local",
) -> str:
    if is_trial:
        head = header("🎁", f"Триал на {settings.trial_days} дня готов")
    else:
        head = header("🔑", "Ссылка подключения")

    return (
        f"{head}\n\n"
        f"{key}\n\n"
        f"{subhead('📋', 'Как подключить')}\n"
        f"{step(1, 'Скопируйте ссылку целиком')}\n"
        f"{step(2, 'Happ → «+» → из буфера')}\n"
        f"{step(3, 'Включите VPN')}\n\n"
        f"{soft_rule()}\n"
        f"Нужна помощь по ОС? Откройте «📖 Гайд»"
    )


def format_buy(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('💎', 'Покупка')}\n\n"
        f"Откроем кабинет уже авторизованным.\n"
        f"Выберите тариф — и готово.\n\n"
        f"{footer_hint('кнопка ниже')}"
    )


def format_balance(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('💰', 'Баланс')}\n\n"
        f"Пополните прямо здесь через СБП или карту.\n"
        f"Либо откройте баланс в кабинете — без регистрации.\n\n"
        f"{footer_hint('выберите способ оплаты')}"
    )


def format_pay_intro() -> str:
    return (
        f"{header('💳', 'Оплата')}\n\n"
        f"Те же способы, что в мини-приложении:\n\n"
        f"{bullet('🏦  СБП · QR — через банк')}\n"
        f"{bullet('💳  Банковская карта')}\n"
        f"{bullet('🪙  Криптовалюта')}\n\n"
        f"{footer_hint()}"
    )


def format_pay_amount_prompt(method_label: str) -> str:
    return (
        f"{header('💵', method_label)}\n\n"
        f"{subhead('✨', 'Сумма пополнения')}\n"
        f"{bullet('от 50 ₽')}\n"
        f"{bullet('или введите свою, например 200')}\n\n"
        f"{footer_hint('кнопки ниже или числом')}"
    )


def format_payment_created(
    *,
    method_label: str,
    amount_rubles: float,
    payment_url: str,
) -> str:
    amount = (
        f"{int(amount_rubles)} ₽"
        if amount_rubles == int(amount_rubles)
        else f"{amount_rubles:.2f} ₽"
    )
    return (
        f"{header('✅', 'Счёт готов')}\n\n"
        f"{kv('Способ', method_label)}\n"
        f"{kv('Сумма', amount)}\n\n"
        f"Нажмите «Оплатить сейчас» — откроется безопасная страница.\n"
        f"Баланс обновится обычно в течение минуты.\n\n"
        f"{soft_rule()}\n"
        f"Если кнопка не сработала:\n{payment_url}"
    )


def format_referral(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('👥', 'Партнёрам')}\n\n"
        f"Реферальная ссылка, статистика и вывод —\n"
        f"в кабинете, без отдельной регистрации.\n\n"
        f"{footer_hint()}"
    )


def format_promo_prompt(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('🎟', 'Промокод')}\n\n"
        f"Отправьте код одним сообщением.\n\n"
        f"  Например:  {brand('PASKOD2026')}\n\n"
        f"{footer_hint('ждём ваш код')}"
    )


def format_apps(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('📱', 'Приложения')}\n\n"
        f"Рекомендуем Happ — спокойный и стабильный клиент.\n\n"
        f"{bullet('v2rayNG')}\n"
        f"{bullet('Hiddify')}\n"
        f"{bullet('Streisand')}\n"
        f"{bullet('V2Box')}\n\n"
        f"Откройте «📖 Гайд», если нужна пошаговая настройка."
    )


def format_renew_stub(settings: Settings) -> str:
    if settings.bedolaga_api_key:
        return (
            f"{header('♻️', 'Продление')}\n\n"
            f"Автопродление на {settings.renew_days} дн. —\n"
            f"напишите «продлить сейчас».\n\n"
            f"Или купите тариф в кабинете."
        )
    return (
        f"{header('♻️', 'Продление')}\n\n"
        f"Откройте покупку в кабинете —\n"
        f"ключ обновится автоматически."
    )


def format_support(settings: Settings) -> str:
    return (
        f"{header('💬', 'Помощь')}\n\n"
        f"{settings.support_text}\n\n"
        f"{bullet('Мы рядом — напишите в любое время')}\n"
        f"{bullet('Документы и FAQ — в разделе «Инфо»')}"
    )


def format_info_menu(settings: Settings) -> str:
    _ = settings
    lines = [
        header("ℹ️", "Инфо и документы"),
        "",
        "Читайте прямо здесь, в боте ВКонтакте.",
        "Выберите документ кнопкой ниже 👇",
        "",
    ]
    for doc in ALL_DOCS:
        lines.append(f"{doc.emoji}  {doc.title}")
        lines.append(f"     {doc.summary}")
        lines.append("")
    lines.append(soft_rule())
    lines.append(footer_hint("откройте нужный документ"))
    return "\n".join(lines)


def format_loading(text: str = "✨ Секунду…") -> str:
    return text


def format_error(text: str) -> str:
    return f"{header('❌', 'Не получилось')}\n\n{text}"


def format_fallback() -> str:
    return (
        f"{header('🤔', 'Не совсем понял')}\n\n"
        f"Откройте меню или отправьте /start —\n"
        f"там всё по полочкам.\n\n"
        f"{footer_hint()}"
    )


def format_trial_used() -> str:
    return (
        f"{header('🎁', 'Триал уже использован')}\n\n"
        f"Можно купить тариф или пополнить баланс.\n\n"
        f"{footer_hint()}"
    )


def days_left(user: User) -> int:
    if not user.subscription_end:
        return 0
    end = user.subscription_end
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)
    return max(0, (end - datetime.utcnow()).days)
