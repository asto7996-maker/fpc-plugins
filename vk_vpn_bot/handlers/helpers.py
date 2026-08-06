"""
Форматирование ответов — тексты как в Telegram Bedolaga.
"""

from __future__ import annotations

from datetime import datetime

from config import Settings
from database.models import User
from services.vpn_keys import mask_key


def format_auto_login_message(url: str, settings: Settings) -> str:
    return (
        "🔐 Вход в кабинет без регистрации\n\n"
        "Как у пользователей Telegram: один тап — и вы уже внутри "
        "личного кабинета Paskod, без email и пароля.\n\n"
        "Нажмите кнопку ниже «Открыть кабинет».\n"
        "Ссылка действует 72 часа и привязана к вашему VK-аккаунту.\n\n"
        f"Если кнопка не открылась, скопируйте:\n{url}"
    )


def format_welcome(settings: Settings, first_name: str | None = None) -> str:
    name = first_name or "друг"
    return (
        f"👋 Привет, {name}!\n\n"
        f"Я — {settings.bot_name}, VK-версия сервиса Paskod "
        f"(тот же функционал, что в Telegram-боте Бедолага).\n\n"
        f"{settings.welcome_text}\n\n"
        f"🎁 Триал: {settings.trial_days} дн. для новых пользователей\n"
        f"🔐 Вход в кабинет — без регистрации (как в Telegram)\n"
        f"💳 Оплата СБП (QR) / картой через Platega — прямо в боте\n"
        f"🌐 {settings.cabinet_url}\n\n"
        "Выберите раздел в меню 👇"
    )


def format_subscription_card(user: User, settings: Settings, panel: dict | None = None) -> str:
    """Карточка подписки (аналог экрана Subscription в TG/Cabinet)."""
    active = user.is_subscription_active()
    if active and user.subscription_end:
        end = user.subscription_end
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        status = f"✅ Активна до {end.strftime('%d.%m.%Y %H:%M')} UTC"
    else:
        status = "❌ Нет активной подписки"

    trial = "использован" if user.is_trial_used else "доступен"
    key_preview = mask_key(user.vpn_key) if user.vpn_key else "— не выдан —"

    lines = [
        "📦 Моя подписка\n",
        f"🆔 VK ID: {user.user_id}",
        f"Статус: {status}",
        f"Триал: {trial}",
        f"Ключ: {key_preview}",
    ]

    if user.bedolaga_user_id:
        lines.append(f"ID в панели: #{user.bedolaga_user_id}")

    if panel:
        sub = panel.get("subscription") if isinstance(panel.get("subscription"), dict) else None
        if sub:
            if sub.get("traffic_limit_gb") is not None:
                used = sub.get("traffic_used_gb", 0)
                limit = sub.get("traffic_limit_gb")
                lines.append(f"Трафик: {used} / {limit} ГБ")
            if sub.get("device_limit") is not None:
                lines.append(f"Устройства: до {sub.get('device_limit')}")
            if sub.get("tariff_name"):
                lines.append(f"Тариф: {sub.get('tariff_name')}")
        bal = panel.get("balance_rubles")
        if bal is not None:
            lines.append(f"Баланс: {bal} ₽")

    lines.append(f"\n🌐 Полный кабинет: {settings.cabinet_url}/subscription")
    return "\n".join(lines)


def format_profile(user: User, settings: Settings) -> str:
    return format_subscription_card(user, settings)


def format_connect_screen(user: User, settings: Settings) -> str:
    if user.is_subscription_active():
        return (
            "🚀 Подключение\n\n"
            "Подписка уже активна.\n"
            "Скопируйте ключ или откройте раздел подписки в кабинете "
            "(там же Happ и устройства) — как в Telegram-боте."
        )
    if not user.is_trial_used:
        return (
            "🚀 Подключение\n\n"
            f"Доступен бесплатный триал на {settings.trial_days} дн.\n"
            "Нажмите «Активировать триал» — создам доступ и пришлю ссылку "
            "для Happ (как кнопка триала в Telegram)."
        )
    return (
        "🚀 Подключение\n\n"
        "Триал уже использован.\n"
        "Купите тариф в кабинете или продлите подписку — "
        "тот же сценарий, что в Telegram Paskod/Bedolaga."
    )


def format_key_message(
    key: str,
    settings: Settings,
    *,
    is_trial: bool,
    source: str = "local",
) -> str:
    header = (
        f"🎁 Триал на {settings.trial_days} дн. активирован!\n\n"
        if is_trial
        else "✅ Ссылка подключения:\n\n"
    )
    source_line = (
        "Источник: панель Paskod (Remnawave/Bedolaga)\n"
        if source == "bedolaga"
        else "Источник: локальный режим (задайте BEDOLAGA_API_KEY для боевых ключей)\n"
    )
    tip = (
        "\n\n📋 Импорт в Happ:\n"
        "1) Скопируйте ссылку целиком\n"
        "2) Happ → «+» → из буфера\n"
        "3) Подключитесь\n\n"
        f"Кабинет: {settings.cabinet_url}/subscription"
    )
    return f"{header}{source_line}\n{key}{tip}"


def format_buy(settings: Settings) -> str:
    return (
        "💳 Купить подписку\n\n"
        "Тарифы и оплата — в личном кабинете Paskod "
        "(тот же кабинет, что открывает Telegram Mini App).\n\n"
        f"👉 {settings.cabinet_url}/subscription/purchase\n\n"
        "После оплаты ключ появится в «Моя подписка» / кабинете."
    )


def format_balance(settings: Settings) -> str:
    return (
        "💰 Баланс\n\n"
        "Пополнить можно прямо здесь — «💳 Оплатить» "
        "(СБП QR / карта через Platega, как в мини-приложении).\n\n"
        f"История и детали — в кабинете:\n{settings.cabinet_url}/balance"
    )


def format_pay_intro() -> str:
    return (
        "💳 Оплата через Platega\n\n"
        "Те же способы, что в мини-приложении Paskod:\n"
        "• 🏦 СБП (QR) — оплата по QR в банковском приложении\n"
        "• 💳 Банк. карта\n"
        "• 🪙 Крипта\n\n"
        "Выберите способ 👇"
    )


def format_pay_amount_prompt(method_label: str) -> str:
    return (
        f"{method_label}\n\n"
        "Выберите сумму пополнения "
        "(минимум 50 ₽, как в кабинете):\n"
        "50 / 100 / 150 / 500 ₽\n\n"
        "Или напишите свою сумму числом, например: 200"
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
        f"✅ Счёт создан ({method_label})\n\n"
        f"Сумма: {amount}\n"
        "Нажмите «Оплатить сейчас» — откроется страница Platega "
        "(СБП QR / карта).\n\n"
        "После оплаты баланс появится в кабинете автоматически "
        "(обычно до минуты).\n\n"
        f"Если кнопка не открылась:\n{payment_url}"
    )


def format_referral(settings: Settings) -> str:
    return (
        "👥 Партнёрская программа\n\n"
        "Реферальная ссылка, статистика и вывод — в кабинете:\n"
        f"{settings.cabinet_url}/referral\n\n"
        "Условия те же, что в Telegram-боте Bedolaga."
    )


def format_promo_prompt(settings: Settings) -> str:
    return (
        "🎟 Промокод\n\n"
        "Отправьте промокод сообщением в этот чат — "
        "или активируйте его в кабинете:\n"
        f"{settings.cabinet_url}/subscription\n\n"
        "Напишите код одним сообщением (например: PASKOD2026)."
    )


def format_apps(settings: Settings) -> str:
    return (
        "📱 Приложения\n\n"
        "Рекомендуем Happ — официальный клиент Paskod.\n"
        "Скачивание и импорт конфигурации:\n"
        f"{settings.cabinet_url}/subscription\n\n"
        "Также подойдут: v2rayNG, Hiddify, Streisand, V2Box.\n"
        "Откройте «Инструкция» для пошагового гайда по ОС."
    )


def format_renew_stub(settings: Settings) -> str:
    if settings.bedolaga_api_key:
        return (
            "♻️ Продление\n\n"
            f"Автопродление через панель на {settings.renew_days} дн.: "
            "напишите «продлить сейчас».\n"
            "Или оплатите тариф в кабинете:\n"
            f"{settings.cabinet_url}/subscription/purchase"
        )
    return (
        "♻️ Продление\n\n"
        "Оплатите тариф в кабинете Paskod — как в Telegram:\n"
        f"{settings.cabinet_url}/subscription/purchase\n\n"
        "После оплаты ключ обновится автоматически в панели."
    )


def format_support(settings: Settings) -> str:
    return (
        "💬 Поддержка\n\n"
        f"{settings.support_text}\n\n"
        f"VK: {settings.support_url}\n"
        f"Кабинет: {settings.cabinet_url}"
    )


def days_left(user: User) -> int:
    if not user.subscription_end:
        return 0
    end = user.subscription_end
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)
    return max(0, (end - datetime.utcnow()).days)
