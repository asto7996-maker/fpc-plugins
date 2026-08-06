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
    mode = (
        "подключён к панели Paskod/Bedolaga"
        if settings.bedolaga_api_key
        else "локальный режим (добавьте BEDOLAGA_API_KEY для реальных ключей)"
    )
    return (
        f"👋 Привет, {name}!\n\n"
        f"Я — {settings.bot_name}.\n"
        f"{settings.welcome_text}\n\n"
        f"🎁 Новым пользователям доступен тест на {settings.trial_days} дн.\n"
        f"⚙️ Режим: {mode}\n\n"
        "Выберите действие в меню ниже 👇"
    )


def format_profile(user: User, settings: Settings) -> str:
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

    bedolaga = (
        f"#{user.bedolaga_user_id}" if user.bedolaga_user_id else "не связан"
    )

    return (
        "👤 Мой профиль\n\n"
        f"🆔 VK ID: {user.user_id}\n"
        f"📅 В боте с: {created_str}\n"
        f"📦 Подписка: {status}\n"
        f"🎁 Тестовый период: {trial}\n"
        f"🔑 Ключ: {key_preview}\n"
        f"🖥 Панель: {bedolaga}\n"
        f"🌐 Кабинет: {settings.cabinet_url}"
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
        "Нажмите «Продлить подписку» или откройте личный кабинет."
    )


def format_key_message(
    key: str,
    settings: Settings,
    *,
    is_trial: bool,
    source: str = "local",
) -> str:
    """Сообщение с выданным ключом."""
    header = (
        f"🎁 Тест на {settings.trial_days} дн. активирован!\n\n"
        if is_trial
        else "✅ Ваш VPN-ключ:\n\n"
    )
    source_line = (
        "Источник: панель Paskod/Bedolaga\n"
        if source == "bedolaga"
        else "Источник: локальный генератор (демо)\n"
    )
    tip = (
        "\n\n📋 Скопируйте ссылку целиком и импортируйте в Happ / v2rayNG.\n"
        "Если не знаете как — откройте «📖 Инструкция»."
    )
    return f"{header}{source_line}🔑 Тип: {settings.vpn_key_type.upper()}\n\n{key}{tip}"


def format_renew_stub(settings: Settings) -> str:
    """Экран продления: кабинет + подсказка по API."""
    if settings.bedolaga_api_key:
        return (
            "💳 Продление подписки\n\n"
            f"Доступно автопродление на {settings.renew_days} дн. через панель.\n"
            "Напишите «продлить сейчас» или откройте кабинет для оплаты картой.\n\n"
            f"🌐 {settings.cabinet_url}"
        )
    return (
        "💳 Продление подписки\n\n"
        "Оплатите и управляйте подпиской в личном кабинете Paskod:\n"
        f"{settings.cabinet_url}\n\n"
        "Или напишите в поддержку — активируем вручную."
    )


def format_support(settings: Settings) -> str:
    """Текст раздела поддержки."""
    return (
        "💬 Поддержка\n\n"
        f"{settings.support_text}\n\n"
        f"Ссылка: {settings.support_url}\n"
        f"Кабинет: {settings.cabinet_url}"
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
