"""
Форматирование ответов: профиль, статус подписки, выдача ключа.
"""

from __future__ import annotations

from datetime import datetime

from config import Settings
from database.models import User
from services.vpn_keys import mask_key


def format_welcome(settings: Settings, first_name: str | None = None) -> str:
    """Приветствие в стиле «Бедолага»."""
    name = first_name or "друг"
    return (
        f"👋 Привет, {name}!\n\n"
        f"Я — {settings.bot_name}.\n"
        f"{settings.welcome_text}\n\n"
        f"🎁 Новым пользователям доступен тест на {settings.trial_days} дн.\n\n"
        "Выберите действие в меню ниже 👇"
    )


def format_profile(user: User) -> str:
    """Карточка профиля пользователя."""
    active = user.is_subscription_active()
    if active and user.subscription_end:
        end = user.subscription_end
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        status = f"✅ Активна до {end.strftime('%d.%m.%Y %H:%M')} UTC"
    else:
        status = "❌ Нет активной подписки"

    trial = "уже использован" if user.is_trial_used else "доступен"
    key_preview = mask_key(user.vpn_key) if user.vpn_key else "— не выдан —"

    created = user.created_at
    if created and created.tzinfo is not None:
        created = created.replace(tzinfo=None)
    created_str = created.strftime("%d.%m.%Y") if created else "—"

    return (
        "👤 Мой профиль\n\n"
        f"🆔 VK ID: {user.user_id}\n"
        f"📅 В боте с: {created_str}\n"
        f"📦 Подписка: {status}\n"
        f"🎁 Тестовый период: {trial}\n"
        f"🔑 Ключ: {key_preview}\n"
    )


def format_connect_screen(user: User, settings: Settings) -> str:
    """Текст экрана «Подключить VPN»."""
    if user.is_subscription_active():
        return (
            "🚀 Подключение VPN\n\n"
            "У вас уже есть активная подписка.\n"
            "Можете посмотреть ключ или продлить доступ."
        )

    if not user.is_trial_used:
        return (
            "🚀 Подключение VPN\n\n"
            f"Вам доступен бесплатный тест на {settings.trial_days} дн.\n"
            "Нажмите «Получить ключ» — активирую подписку и пришлю конфиг."
        )

    return (
        "🚀 Подключение VPN\n\n"
        "Тестовый период уже использован, активной подписки нет.\n"
        "Нажмите «Продлить подписку», чтобы получить доступ."
    )


def format_key_message(key: str, settings: Settings, *, is_trial: bool) -> str:
    """Сообщение с выданным ключом."""
    header = (
        f"🎁 Тест на {settings.trial_days} дн. активирован!\n\n"
        if is_trial
        else "✅ Ваш VPN-ключ:\n\n"
    )
    tip = (
        "\n\n📋 Скопируйте ключ целиком и импортируйте в приложение.\n"
        "Если не знаете как — откройте «📖 Инструкция»."
    )
    # Ключ в отдельном блоке — удобно копировать
    return f"{header}🔑 Тип: {settings.vpn_key_type.upper()}\n\n{key}{tip}"


def format_renew_stub() -> str:
    """Заглушка оплаты (можно заменить на платёжный модуль)."""
    return (
        "💳 Продление подписки\n\n"
        "Оплата пока в режиме ручной выдачи.\n"
        "Напишите в поддержку — подберём тариф и активируем ключ.\n\n"
        "Тарифы (пример):\n"
        "• 30 дней — уточняйте у оператора\n"
        "• 90 дней — выгоднее\n"
        "• 365 дней — максимум экономии"
    )


def format_support(settings: Settings) -> str:
    """Текст раздела поддержки."""
    return (
        "💬 Поддержка\n\n"
        f"{settings.support_text}\n\n"
        f"Ссылка: {settings.support_url}"
    )


def days_left(user: User) -> int:
    """Сколько полных дней осталось по подписке."""
    if not user.subscription_end:
        return 0
    end = user.subscription_end
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)
    delta = end - datetime.utcnow()
    return max(0, delta.days)
