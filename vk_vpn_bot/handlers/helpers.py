"""
Тексты сообщений Paskod: живая связная речь с эмодзи.

Объём подстраивается под контекст — от одного предложения на служебных
экранах до четырёх-пяти там, где человеку нужно понять, что делать дальше.
"""

from __future__ import annotations

from datetime import datetime

from config import Settings
from database.models import User
from handlers.style import (
    brand,
    bullet,
    error_banner,
    footer_hint,
    header,
    kv,
    step,
    subhead,
    success_banner,
    warn_banner,
)
from services.catalog import Catalog, Offer
from services.payments import (
    PLATEGA_METHOD_CARD,
    PLATEGA_METHOD_CRYPTO,
    PLATEGA_METHOD_SBP_QR,
    method_copy,
    method_label,
)
from services.referral import ReferralStats
from services.vpn_keys import mask_key


def _plural_devices(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} устройство"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} устройства"
    return f"{n} устройств"


def _devices_upto(n: int) -> str:
    """Родительный падеж после «до»: до 1 устройства / до 5 устройств."""
    word = "устройства" if n % 10 == 1 and n % 100 != 11 else "устройств"
    return f"до {n} {word}"


def format_auto_login_message(
    url: str,
    settings: Settings,
    *,
    redirect: str | None = "/",
) -> str:
    where = {
        "/": "кабинет",
        "/subscription": "раздел подписки",
        "/subscription/purchase": "страницу тарифов",
        "/balance": "баланс",
        "/referral": "партнёрскую программу",
        "/balance/top-up": "пополнение баланса",
        "/legal/index.html": "документы",
        "/info": "информацию о сервисе",
        "/admin": "админ-панель",
        "/admin/users": "список пользователей",
        "/admin/tickets": "тикеты поддержки",
    }.get(redirect or "/", redirect or "кабинет")

    return (
        f"{header('🔐', 'Вход')}\n\n"
        f"Откроется {where}. Пароль не нужен — вы уже вошли через ВК.\n"
        f"Ссылка работает 3 дня, только для вас.\n\n"
        f"{url}"
    )


def format_welcome(
    settings: Settings,
    first_name: str | None = None,
    catalog: Catalog | None = None,
) -> str:
    name = (first_name or "").strip()
    hello = f"Привет, {name}!" if name else "Привет!"
    bot = brand(settings.bot_name) if settings.bot_name.isascii() else settings.bot_name

    trial = (
        f"{settings.trial_days} дн. бесплатно, {settings.trial_traffic_gb} ГБ, "
        f"{_plural_devices(settings.trial_devices)}"
    )
    price = catalog.entry_price_label if catalog else ""

    return (
        f"✨ {hello}\n\n"
        f"{bot} — быстрый VPN для телефона и компьютера.\n"
        f"Можно попробовать бесплатно: {trial}.\n"
        f"Платные тарифы"
        f"{f' {price}' if price else ''}. "
        f"Оплата картой, по QR или криптой от {(catalog.min_topup_kopeks // 100) if catalog else 50} ₽.\n\n"
        f"{bullet('🌐 Кабинет — ваш аккаунт на сайте')}\n"
        f"{bullet('📦 Подписка — ссылка, оплата, срок')}\n"
        f"{bullet('ℹ️ Инфо — правила и пригласить друга')}\n"
        f"{bullet('💬 Помощь · 📱 Гайд — если что-то непонятно')}\n\n"
        f"{footer_hint()}"
    )


def format_tariff_menu(catalog: Catalog | None, settings: Settings, *, renew: bool) -> str:
    """Экран выбора тарифа: коротко, с кнопками-офферами под сообщением."""
    if not catalog or not catalog.offers():
        return (
            f"{header('💎', 'Тарифы')}\n\n"
            f"Список тарифов и цены — на сайте в личном кабинете. "
            f"Открою его для вас — регистрация не нужна.\n\n"
            f"{footer_hint('кнопка ниже')}"
        )

    title = "Продление" if renew else "Тарифы"
    lines = [
        header("💎", title),
        "",
        "Цены за месяц. Другие сроки (3 и 6 мес.) дешевле в кабинете на сайте.",
        "",
    ]
    for offer in catalog.offers():
        t = offer.tariff
        whitelist = " · 📋 белые списки" if t.has_whitelist_server else ""
        lines.append(
            f"{t.emoji}  {t.name} — {t.price_from_label}/мес\n"
            f"     {t.traffic_display} · {_devices_upto(t.device_limit)}{whitelist}"
        )
    lines.append("")
    lines.append(
        "Нажмите тариф под сообщением. Если денег на счету не хватает — "
        "выставлю счёт, тариф включится сам после оплаты."
    )
    return "\n".join(lines)


def format_tariff_chosen(offer: Offer) -> str:
    t = offer.tariff
    note = f" ({offer.per_month_note})" if offer.per_month_note else ""
    whitelist = (
        f"\n{kv('Сервер', 'с белыми списками')}"
        if t.has_whitelist_server
        else ""
    )
    more_periods = (
        f"\n\nДругие сроки ({', '.join(p.label for p in t.periods[1:3])}) — "
        f"в кабинете на сайте."
        if len(t.periods) > 1
        else ""
    )
    return (
        f"{header('💳', f'{t.name} · {offer.period.label}')}\n\n"
        f"{t.short_description}\n\n"
        f"{kv('Цена', offer.period.price_label + note)}\n"
        f"{kv('Интернет', t.traffic_display)}\n"
        f"{kv('Устройства', _devices_upto(t.device_limit))}"
        f"{whitelist}{more_periods}\n\n"
        f"Как оплатить: {method_label(PLATEGA_METHOD_SBP_QR)}, "
        f"{method_label(PLATEGA_METHOD_CARD)} или {method_label(PLATEGA_METHOD_CRYPTO)}. "
        f"Если на счету хватает денег — тариф включится сразу, "
        f"если нет — пришлю ссылку на оплату.\n\n"
        f"{footer_hint()}"
    )


def format_tariff_activated(offer: Offer, settings: Settings) -> str:
    t = offer.tariff
    return (
        f"{header('✅', 'Тариф активирован')}\n\n"
        f"{kv('Тариф', t.name)}\n"
        f"{kv('Период', offer.period.label)}\n"
        f"{kv('Устройств', f'до {t.device_limit}')}\n\n"
        f"Готово — VPN работает. «🔑 Ключ» — ваша ссылка, «📦 Подписка» — срок и оплата."
    )


def format_tariff_needs_topup(offer: Offer, missing_rubles: str, *, method_code: int) -> str:
    copy = method_copy(method_code)
    label = method_label(method_code)
    return (
        f"{header('💳', 'Нужно пополнить счёт')}\n\n"
        f"Тариф «{offer.tariff.name} · {offer.period.label}» стоит "
        f"{offer.period.price_label}, не хватает {missing_rubles}. "
        f"Сейчас выставлю счёт: {label}.\n\n"
        f"{copy['how']} {copy['timing']} После оплаты тариф включится сам."
    )


def format_tariffs(catalog: Catalog | None, settings: Settings) -> str:
    """Реальные тарифы из панели: трафик, устройства, цены по периодам."""
    if not catalog or not catalog.tariffs:
        return (
            f"{header('💎', 'Тарифы')}\n\n"
            f"Актуальные тарифы и цены — в личном кабинете на сайте. "
            f"Открою его для вас, регистрация не нужна.\n\n"
            f"{footer_hint('кнопка ниже')}"
        )

    lines = [
        header("💎", "Тарифы"),
        "",
        "Три тарифа — отличаются объёмом интернета, устройствами и серверами.",
        "",
    ]

    for tariff in catalog.tariffs:
        lines.append(f"{tariff.emoji} {tariff.name}")
        lines.append(f"     {tariff.short_description}")
        lines.append(f"     Интернет: {tariff.traffic_display}")
        lines.append(f"     Устройств: до {tariff.device_limit}")
        if tariff.has_whitelist_server:
            lines.append("     Есть сервер с белыми списками")
        if tariff.price_from_label:
            lines.append(f"     Цена: {tariff.price_from_label}/мес")
        if len(tariff.periods) > 1:
            extras = ", ".join(
                f"{p.label} — {p.price_label}" for p in tariff.periods[1:3]
            )
            lines.append(f"     Длиннее: {extras}")
        if tariff.extra_device_kopeks:
            lines.append(
                f"     Доп. устройство: {tariff.extra_device_kopeks // 100} ₽"
            )
        lines.append("")

    lines.append(
        f"Оплата списывается со счёта в кабинете. Пополнить можно кнопкой "
        f"«💳 Оплатить», от {catalog.min_topup_kopeks // 100} ₽."
    )
    lines.append("")
    lines.append(footer_hint("открыть кабинет кнопкой ниже"))
    return "\n".join(lines)


def format_subscription_card(
    user: User, settings: Settings, panel: dict | None = None
) -> str:
    active = user.is_subscription_active()

    if active and user.subscription_end:
        end = user.subscription_end
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        status = f"✅ активна до {end.strftime('%d.%m.%Y')}"
    else:
        status = "⛔️ не активна"

    trial = "уже был" if user.is_trial_used else "🎁 можно попробовать"
    key_preview = mask_key(user.vpn_key) if user.vpn_key else "ещё нет"

    lines = [
        header("📦", "Подписка"),
        "",
        kv("Статус", status),
        kv("Пробный период", trial),
        kv("Ссылка", key_preview),
    ]

    time_left = ""
    if panel:
        sub = (
            panel.get("subscription")
            if isinstance(panel.get("subscription"), dict)
            else None
        )
        if sub:
            if sub.get("tariff_name"):
                lines.append(kv("Тариф", str(sub.get("tariff_name"))))
            elif sub.get("is_trial"):
                lines.append(kv("Тариф", "пробный"))
            if sub.get("traffic_limit_gb"):
                used = sub.get("traffic_used_gb", 0)
                limit = sub.get("traffic_limit_gb")
                percent = sub.get("traffic_used_percent")
                extra = f" ({percent}%)" if percent not in (None, 0) else ""
                lines.append(kv("Интернет", f"{used} / {limit} ГБ{extra}"))
            elif sub.get("traffic_limit_gb") == 0:
                lines.append(kv("Интернет", "без ограничений"))
            if sub.get("device_limit") is not None:
                lines.append(kv("Устройства", f"до {sub.get('device_limit')}"))
            if sub.get("time_left_display") and sub.get("is_active"):
                time_left = str(sub["time_left_display"])
                lines.append(kv("Осталось", time_left))
            if sub.get("autopay_enabled") is not None:
                lines.append(
                    kv(
                        "Автоплатёж",
                        "включён" if sub.get("autopay_enabled") else "выключен",
                    )
                )
        bal = panel.get("balance_rubles")
        if bal is not None:
            lines.append(kv("Баланс", f"{bal} ₽"))

    lines.append("")
    if active:
        lines.append("«🔑 Ключ» — ваша ссылка. «♻️ Продлить» — продлить доступ.")
    elif not user.is_trial_used:
        lines.append(
            f"Можно попробовать бесплатно: {settings.trial_days} дн., "
            f"{settings.trial_traffic_gb} ГБ. Нажмите «🎁 Триал»."
        )
    else:
        lines.append("Сейчас VPN выключен. Нажмите «💎 Тарифы» или «💳 Оплатить».")

    lines.append("")
    lines.append(f"🌐  {settings.cabinet_url}/subscription")
    return "\n".join(lines)


def format_profile(user: User, settings: Settings) -> str:
    return format_subscription_card(user, settings)


def format_connect_screen(
    user: User, settings: Settings, catalog: Catalog | None = None
) -> str:
    if user.is_subscription_active():
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner('VPN включён')}\n\n"
            f"Нажмите «🔑 Ключ» — там ссылка для приложения Happ.\n\n"
            f"{footer_hint()}"
        )
    if not user.is_trial_used:
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner('Можно попробовать бесплатно')}\n\n"
            f"{settings.trial_days} дн., {settings.trial_traffic_gb} ГБ, "
            f"{_plural_devices(settings.trial_devices)}. Карта не нужна — "
            f"нажмите «🎁 Триал».\n\n"
            f"{footer_hint()}"
        )

    price = catalog.entry_price_label if catalog else ""
    tail = f"Тарифы {price}. " if price else ""
    return (
        f"{header('🚀', 'Подключение')}\n\n"
        f"{warn_banner('Пробный период уже был')}\n\n"
        f"{tail}"
        f"«💎 Тарифы» или «💳 Оплатить».\n\n"
        f"{footer_hint()}"
    )


def format_key_message(
    key: str,
    settings: Settings,
    *,
    is_trial: bool,
    source: str = "local",
    limits: dict | None = None,
) -> str:
    _ = source
    if is_trial:
        head = header("🎁", f"Пробный период на {settings.trial_days} дня")
        intro = f"VPN включён на {settings.trial_days} дня."
    else:
        head = header("🔑", "Ваша ссылка")
        intro = "Скопируйте и вставьте в Happ:"

    facts: list[str] = []
    if limits:
        if limits.get("traffic_limit_gb"):
            used = limits.get("traffic_used_gb") or 0
            facts.append(kv("Интернет", f"{used} / {limits['traffic_limit_gb']} ГБ"))
        elif limits.get("unlimited"):
            facts.append(kv("Интернет", "без ограничений"))
        if limits.get("device_limit"):
            facts.append(kv("Устройств", f"до {limits['device_limit']}"))
        if limits.get("end_date"):
            facts.append(kv("Действует до", str(limits["end_date"])))
    elif is_trial:
        facts = [
            kv("Интернет", f"{settings.trial_traffic_gb} ГБ"),
            kv("Устройств", f"до {settings.trial_devices}"),
        ]

    facts_block = ("\n".join(facts) + "\n\n") if facts else ""

    return (
        f"{head}\n\n"
        f"{intro}\n"
        f"{facts_block}"
        f"{key}\n\n"
        f"{step(1, 'Скопируйте ссылку целиком')}\n"
        f"{step(2, 'Happ → «+» → вставьте ссылку')}\n"
        f"{step(3, 'Включите VPN')}\n\n"
        f"Подробнее — «📱 Гайд». Проверка: 2ip.ru"
    )


def format_buy(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('💎', 'Покупка')}\n\n"
        f"Открою личный кабинет на сайте — там все тарифы и цены. "
        f"После оплаты VPN включится сам, ссылка появится в «📦 Подписка».\n\n"
        f"{footer_hint('кнопка ниже')}"
    )


def format_balance(settings: Settings, catalog: Catalog | None = None) -> str:
    _ = settings
    minimum = (catalog.min_topup_kopeks // 100) if catalog else 50
    return format_pay_intro(catalog).replace(
        header("💳", "Оплата"),
        header("💰", "Баланс"),
        1,
    ) + f"\n\nМинимум {minimum} ₽."


def format_pay_intro(catalog: Catalog | None = None) -> str:
    minimum = (catalog.min_topup_kopeks // 100) if catalog else 50
    amounts = (
        ", ".join(f"{a // 100} ₽" for a in catalog.quick_amounts_kopeks)
        if catalog and catalog.quick_amounts_kopeks
        else "50, 100, 150, 500 ₽"
    )
    sbp = method_copy(PLATEGA_METHOD_SBP_QR)
    card = method_copy(PLATEGA_METHOD_CARD)
    crypto = method_copy(PLATEGA_METHOD_CRYPTO)
    return (
        f"{header('💳', 'Оплата')}\n\n"
        f"Сначала пополняете счёт, потом с него списывается тариф.\n\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_SBP_QR)} — {sbp['summary']}')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CARD)} — {card['summary']}')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CRYPTO)} — {crypto['summary']}')}\n\n"
        f"Выберите способ, потом сумму ({amounts}, от {minimum} ₽).\n\n"
        f"{footer_hint()}"
    )


def format_pay_amount_prompt(
    method_code: int, catalog: Catalog | None = None
) -> str:
    minimum = (catalog.min_topup_kopeks // 100) if catalog else 50
    copy = method_copy(method_code)
    label = method_label(method_code)
    return (
        f"{header('💵', label)}\n\n"
        f"{copy['summary']} {copy['how']}\n\n"
        f"Сумма: нажмите кнопку или напишите число (от {minimum} ₽). {copy['timing']}\n\n"
        f"{footer_hint()}"
    )


def format_payment_created(
    *,
    method_code: int,
    amount_rubles: float,
    payment_url: str,
) -> str:
    amount = (
        f"{int(amount_rubles)} ₽"
        if amount_rubles == int(amount_rubles)
        else f"{amount_rubles:.2f} ₽"
    )
    copy = method_copy(method_code)
    label = method_label(method_code)
    return (
        f"{header('✅', 'Счёт готов')}\n\n"
        f"{kv('Способ', label)}\n"
        f"{kv('Сумма', amount)}\n\n"
        f"Нажмите «✨ Оплатить» и следуйте подсказкам на странице. {copy['how']} {copy['timing']}\n"
        f"Деньги не пришли за 15 минут? Напишите в «💬 Помощь» → «✍️ Вопрос».\n\n"
        f"{payment_url}"
    )


def format_referral(
    settings: Settings,
    stats: ReferralStats | None = None,
    catalog: Catalog | None = None,
) -> str:
    days = settings.referral_inviter_bonus_days
    percent = (
        stats.commission_percent
        if stats
        else (catalog.referral_percent if catalog else 0)
    )

    bonus_line = (
        f"{bullet(f'🎁 +{days} дн. к подписке за каждого друга')}\n"
        if days > 0
        else ""
    )
    commission_line = (
        f"{bullet(f'💰 {percent}% с оплат друзей')}\n"
        if percent
        else ""
    )

    body = (
        f"{header('👥', 'Пригласить друга')}\n\n"
        f"Отправьте другу свою ссылку — получите бонус.\n\n"
        f"{bonus_line}"
        f"{commission_line}\n"
    )

    if stats and stats.referral_code:
        body += (
            f"{subhead('🔗', 'Ваша ссылка')}\n"
            f"{stats.vk_link}\n\n"
            f"{kv('Приглашено', stats.total_referrals)}\n"
            f"{kv('Оплатили', stats.active_referrals)}\n"
            f"{kv('Заработано', f'{stats.total_earnings_rubles:.0f} ₽')}\n\n"
            f"Друг должен открыть бот именно по этой ссылке.\n\n"
        )
    else:
        body += "Ссылка появится после входа в кабинет.\n\n"

    return f"{body}{footer_hint()}"


def format_referral_inviter_bonus(settings: Settings) -> str:
    days = settings.referral_inviter_bonus_days
    if days <= 0:
        return ""
    return (
        f"{success_banner('Спасибо!')}\n\n"
        f"Вы пришли по приглашению друга. "
        f"Ему добавим +{days} дн. к подписке."
    )


def format_referral_bonus_notify(settings: Settings) -> str:
    days = settings.referral_inviter_bonus_days
    if days <= 0:
        return (
            f"{success_banner('Новый друг!')}\n\n"
            f"Кто-то зарегистрировался по вашей ссылке."
        )
    return (
        f"{success_banner('Новый друг!')}\n\n"
        f"По вашей ссылке пришёл друг — "
        f"добавили +{days} дн. к вашей подписке."
    )


def format_promo_prompt(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('🎟', 'Промокод')}\n\n"
        f"Напишите код одним сообщением — большие или маленькие буквы не важны.\n"
        f"Что даст код — увидите при активации.\n\n"
        f"{footer_hint('ждём код')}"
    )


def format_apps(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('📱', 'Приложение')}\n\n"
        f"Нужно Happ — оно бесплатное.\n\n"
        f"{bullet('iPhone и iPad — App Store, ищите «Happ»')}\n"
        f"{bullet('Android — Google Play или happ.su')}\n"
        f"{bullet('Компьютер — happ.su')}\n\n"
        f"Ссылку из бота вставляете в Happ — и всё. "
        f"Пошагово: «📱 Гайд»."
    )


def format_renew_stub(settings: Settings, catalog: Catalog | None = None) -> str:
    price = catalog.entry_price_label if catalog else ""
    prices = f"Тарифы {price}; " if price else ""
    long_hint = (
        "на трёх и шести месяцах месяц выходит дешевле."
        if catalog and catalog.tariffs
        else "длинные периоды дешевле в пересчёте на месяц."
    )

    if settings.bedolaga_api_key:
        return (
            f"{header('♻️', 'Продление')}\n\n"
            f"Напишите «продлить сейчас» — продлю на {settings.renew_days} дней.\n\n"
            f"Или выберите другой срок в кабинете. {prices}{long_hint} "
            f"Ссылку в приложении менять не нужно.\n\n"
            f"{footer_hint()}"
        )
    return (
        f"{header('♻️', 'Продление')}\n\n"
        f"Пополните счёт в кабинете и выберите срок. {prices}{long_hint}\n\n"
        f"Ссылку в приложении менять не нужно.\n\n"
        f"{footer_hint()}"
    )


def format_help_menu(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('💬', 'Помощь')}\n\n"
        f"{settings.support_text}\n\n"
        f"{bullet('✍️ Вопрос — напишите, поможем')}\n"
        f"{bullet('🎟 Промо — введите промокод')}\n\n"
        f"{footer_hint()}"
    )


def format_support(settings: Settings) -> str:
    return format_help_menu(settings)


def format_support_prompt() -> str:
    return (
        f"{header('✍️', 'Вопрос')}\n\n"
        f"Расскажите, что не получается. «◀️ Назад» — отмена."
    )


def format_support_received(delivered: bool) -> str:
    tail = "Уведомление отправлено." if delivered else "Сообщение в диалоге."
    return (
        f"{header('✅', 'Принято')}\n\n"
        f"{tail} Ответ придёт сюда. Частые вопросы — в «ℹ️ Инфо»."
    )


def format_admin_menu(settings: Settings) -> str:
    username = settings.main_admin_username or "админ"
    return (
        f"{header('🛠', 'Админ')}\n\n"
        f"@{username}, тикеты приходят вам. Кнопки — автовход в кабинет.\n\n"
        f"{footer_hint()}"
    )


def format_admin_denied() -> str:
    return (
        f"{warn_banner('Только для админа')}\n\n"
        f"Нужна помощь — «💬 Помощь»."
    )


def format_info_menu(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('ℹ️', 'Инфо')}\n\n"
        f"Документы и «пригласить друга» — кнопки ниже. "
        f"Длинный текст листайте стрелками.\n\n"
        f"{footer_hint()}"
    )


def format_cabinet_plain(settings: Settings, redirect: str = "/") -> str:
    """Кабинет без автологина — когда не настроен CABINET_JWT_SECRET."""
    return (
        f"{header('🌐', 'Личный кабинет')}\n\n"
        f"Сейчас не могу открыть кабинет автоматически. "
        f"Зайдите на сайт по ссылке и войдите как обычно.\n\n"
        f"{settings.cabinet_url}{redirect}"
    )


def format_doc_not_found() -> str:
    return (
        f"{header('📄', 'Документ не найден')}\n\n"
        f"Такого документа нет. Выберите другой из списка ниже."
    )


def format_min_amount(current: str, minimum: str = "50 ₽") -> str:
    return (
        f"{header('⚠️', 'Сумма слишком маленькая')}\n\n"
        f"Минимум — {minimum}, а вы написали {current}. "
        f"Выберите сумму кнопкой или напишите больше."
    )


def format_pay_method_first() -> str:
    return f"{header('💳', 'Сначала способ')}\n\nВыберите: 🏦 СБП, 💳 Карта или 🪙 Крипта."


def format_amount_invalid() -> str:
    return f"{header('💵', 'Не понял')}\n\nНапишите сумму числом или нажмите кнопку ниже."


def format_promo_accepted(code: str) -> str:
    return (
        f"{header('🎟', 'Промокод')}\n\n"
        f"{brand(code) if code.isascii() else code}\n\n"
        f"Открою кабинет, чтобы активировать код.\n\n"
    )


def format_guide_outro() -> str:
    return "🏠  «◀️ Назад» — в меню."


def format_fallback() -> str:
    return (
        f"{header('🤔', 'Не понял')}\n\n"
        f"Нажмите кнопку внизу или напишите /start. "
        f"Нужна помощь — «💬 Помощь».\n\n"
        f"{footer_hint()}"
    )


def format_trial_used() -> str:
    return (
        f"{header('🎁', 'Пробный период уже был')}\n\n"
        f"Оплатите тариф — «💎 Тарифы» или «💳 Оплатить». Ссылку менять не нужно.\n\n"
        f"{footer_hint()}"
    )


def format_loading(text: str = "✨ Секунду…") -> str:
    return text


def format_error(text: str) -> str:
    return f"{header('❌', 'Ошибка')}\n\n{error_banner(text)}"


def days_left(user: User) -> int:
    if not user.subscription_end:
        return 0
    end = user.subscription_end
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)
    return max(0, (end - datetime.utcnow()).days)
