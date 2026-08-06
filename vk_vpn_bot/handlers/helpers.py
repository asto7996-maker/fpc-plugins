"""
Тексты сообщений — спокойный минималистичный тон Paskod.
"""

from __future__ import annotations

from datetime import datetime

from config import Settings
from database.models import User
from services.vpn_keys import mask_key


def _rule() -> str:
    return "· · ·"


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
    }.get(redirect or "/", redirect or "кабинет")

    return (
        f"Вход без регистрации\n"
        f"{_rule()}\n\n"
        f"Аккаунт уже готов. Откройте {where} — "
        f"email и пароль не нужны.\n\n"
        f"Ссылка действует 72 часа.\n\n"
        f"Если кнопка не сработала:\n{url}"
    )


def format_welcome(settings: Settings, first_name: str | None = None) -> str:
    name = (first_name or "").strip()
    hello = f"Привет, {name}" if name else "Привет"

    return (
        f"{hello}\n"
        f"{_rule()}\n\n"
        f"{settings.bot_name} — быстрый VPN без лишних шагов.\n\n"
        f"Триал {settings.trial_days} дня · оплата СБП · "
        f"кабинет без регистрации\n\n"
        f"Выберите действие ниже"
    )


def format_subscription_card(
    user: User, settings: Settings, panel: dict | None = None
) -> str:
    if user.is_subscription_active() and user.subscription_end:
        end = user.subscription_end
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        status = f"активна до {end.strftime('%d.%m.%Y')}"
    else:
        status = "не активна"

    trial = "уже использован" if user.is_trial_used else "доступен"
    key_preview = mask_key(user.vpn_key) if user.vpn_key else "ещё не выдан"

    lines = [
        "Подписка",
        _rule(),
        "",
        f"Статус · {status}",
        f"Триал · {trial}",
        f"Ключ · {key_preview}",
    ]

    if panel:
        sub = (
            panel.get("subscription")
            if isinstance(panel.get("subscription"), dict)
            else None
        )
        if sub:
            if sub.get("tariff_name"):
                lines.append(f"Тариф · {sub.get('tariff_name')}")
            if sub.get("traffic_limit_gb") is not None:
                used = sub.get("traffic_used_gb", 0)
                limit = sub.get("traffic_limit_gb")
                lines.append(f"Трафик · {used} / {limit} ГБ")
            if sub.get("device_limit") is not None:
                lines.append(f"Устройства · до {sub.get('device_limit')}")
        bal = panel.get("balance_rubles")
        if bal is not None:
            lines.append(f"Баланс · {bal} ₽")

    return "\n".join(lines)


def format_profile(user: User, settings: Settings) -> str:
    return format_subscription_card(user, settings)


def format_connect_screen(user: User, settings: Settings) -> str:
    if user.is_subscription_active():
        return (
            f"Подключение\n"
            f"{_rule()}\n\n"
            f"Подписка уже активна.\n"
            f"Откройте ключ или кабинет — и подключайтесь в Happ."
        )
    if not user.is_trial_used:
        return (
            f"Подключение\n"
            f"{_rule()}\n\n"
            f"Вам доступен бесплатный триал на {settings.trial_days} дня.\n"
            f"Один тап — и пришлём ссылку для Happ."
        )
    return (
        f"Подключение\n"
        f"{_rule()}\n\n"
        f"Триал уже использован.\n"
        f"Можно купить тариф или пополнить баланс."
    )


def format_key_message(
    key: str,
    settings: Settings,
    *,
    is_trial: bool,
    source: str = "local",
) -> str:
    header = (
        f"Триал на {settings.trial_days} дня готов"
        if is_trial
        else "Ссылка подключения"
    )
    return (
        f"{header}\n"
        f"{_rule()}\n\n"
        f"{key}\n\n"
        f"Как подключить\n"
        f"1. Скопируйте ссылку целиком\n"
        f"2. Happ → «+» → из буфера\n"
        f"3. Включите VPN"
    )


def format_buy(settings: Settings) -> str:
    _ = settings
    return (
        f"Покупка\n"
        f"{_rule()}\n\n"
        f"Откроем кабинет уже авторизованным.\n"
        f"Выберите тариф — и готово."
    )


def format_balance(settings: Settings) -> str:
    _ = settings
    return (
        f"Баланс\n"
        f"{_rule()}\n\n"
        f"Пополните прямо здесь через СБП или карту.\n"
        f"Либо откройте баланс в кабинете."
    )


def format_pay_intro() -> str:
    return (
        f"Оплата\n"
        f"{_rule()}\n\n"
        f"Выберите удобный способ — "
        f"те же, что в мини-приложении."
    )


def format_pay_amount_prompt(method_label: str) -> str:
    return (
        f"{method_label}\n"
        f"{_rule()}\n\n"
        f"Сумма пополнения\n"
        f"от 50 ₽ · или введите свою, например 200"
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
        f"Счёт готов\n"
        f"{_rule()}\n\n"
        f"{method_label} · {amount}\n\n"
        f"Нажмите «Оплатить» — откроется безопасная страница.\n"
        f"Баланс обновится обычно в течение минуты.\n\n"
        f"Если кнопка не сработала:\n{payment_url}"
    )


def format_referral(settings: Settings) -> str:
    _ = settings
    return (
        f"Партнёрам\n"
        f"{_rule()}\n\n"
        f"Реферальная ссылка, статистика и вывод — "
        f"в кабинете, без отдельной регистрации."
    )


def format_promo_prompt(settings: Settings) -> str:
    _ = settings
    return (
        f"Промокод\n"
        f"{_rule()}\n\n"
        f"Отправьте код одним сообщением.\n"
        f"Например: PASKOD2026"
    )


def format_apps(settings: Settings) -> str:
    _ = settings
    return (
        f"Приложения\n"
        f"{_rule()}\n\n"
        f"Рекомендуем Happ — спокойный и стабильный клиент.\n\n"
        f"Также подойдут v2rayNG, Hiddify, Streisand и V2Box.\n"
        f"Откройте «Гайд», если нужна пошаговая настройка."
    )


def format_renew_stub(settings: Settings) -> str:
    if settings.bedolaga_api_key:
        return (
            f"Продление\n"
            f"{_rule()}\n\n"
            f"Автопродление на {settings.renew_days} дн. — "
            f"напишите «продлить сейчас».\n\n"
            f"Или купите тариф в кабинете."
        )
    return (
        f"Продление\n"
        f"{_rule()}\n\n"
        f"Откройте покупку в кабинете — "
        f"ключ обновится автоматически."
    )


def format_support(settings: Settings) -> str:
    return (
        f"Помощь\n"
        f"{_rule()}\n\n"
        f"{settings.support_text}\n\n"
        f"Мы рядом — напишите в любое время."
    )


def format_loading(text: str = "Секунду…") -> str:
    return text


def format_error(text: str) -> str:
    return f"Не получилось\n{_rule()}\n\n{text}"


def format_fallback() -> str:
    return (
        f"Не совсем понял\n"
        f"{_rule()}\n\n"
        f"Откройте меню или отправьте /start — "
        f"там всё по полочкам."
    )


def days_left(user: User) -> int:
    if not user.subscription_end:
        return 0
    end = user.subscription_end
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)
    return max(0, (end - datetime.utcnow()).days)
