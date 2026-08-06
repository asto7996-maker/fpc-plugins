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
        f"Аккаунт привязан к ВК — пароль не нужен. Откроется {where}.\n"
        f"Ссылка 72 ч, только для вас.\n\n"
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
        f"{settings.trial_days} дня, {settings.trial_traffic_gb} ГБ трафика, "
        f"{_plural_devices(settings.trial_devices)}"
    )
    price = catalog.entry_price_label if catalog else ""
    tariff_line = (
        f"Дальше тарифы с безлимитным трафиком — от {price} в месяц, "
        f"до {_plural_devices(catalog.max_devices)} на одной подписке. "
        if price and catalog and catalog.max_devices
        else "Дальше можно перейти на тариф с безлимитным трафиком. "
    )

    return (
        f"✨  {hello}\n"
        f"{card_rule()}\n\n"
        f"{bot} — VPN через Happ (VLESS). Триал: {trial}, без карты.\n"
        f"{tariff_line}"
        f"Оплата: СБП, карта или крипта от {(catalog.min_topup_kopeks // 100) if catalog else 50} ₽.\n\n"
        f"{bullet('🚀 Подключиться — ключ за минуту')}\n"
        f"{bullet('📦 Подписка — статус и баланс')}\n"
        f"{bullet('📖 Помощь — гайд, FAQ, поддержка')}\n\n"
        f"{footer_hint()}"
    )


def format_tariff_menu(catalog: Catalog | None, settings: Settings, *, renew: bool) -> str:
    """Экран выбора тарифа: коротко, с кнопками-офферами под сообщением."""
    if not catalog or not catalog.offers():
        return (
            f"{header('💎', 'Тарифы')}\n\n"
            f"Список тарифов и цены — в кабинете, открою уже авторизованным.\n\n"
            f"{footer_hint('кнопка ниже')}"
        )

    title = "Продление" if renew else "Тарифы"
    lines = [
        header("💎", title),
        "",
        "Безлимит трафика. Чем длиннее период — тем дешевле месяц. "
        "Нажмите тариф под сообщением.",
        "",
    ]
    for offer in catalog.offers():
        t = offer.tariff
        note = f" · {offer.per_month_note}" if offer.per_month_note else ""
        lines.append(
            f"{t.emoji}  {t.name} · {offer.period.label} — "
            f"{offer.period.price_label}{note} · {_devices_upto(t.device_limit)}"
        )
    lines.append("")
    lines.append("Дальше — способ оплаты. Не хватает баланса → счёт, тариф активируется сам.")
    return "\n".join(lines)


def format_tariff_chosen(offer: Offer) -> str:
    t = offer.tariff
    note = f" ({offer.per_month_note})" if offer.per_month_note else ""
    return (
        f"{header('💳', f'{t.name} · {offer.period.label}')}\n\n"
        f"{kv('Цена', offer.period.price_label + note)}\n"
        f"{kv('Трафик', t.traffic_label or '♾️ Безлимит')}\n"
        f"{kv('Устройства', _devices_upto(t.device_limit))}\n\n"
        f"Способ оплаты: {method_label(PLATEGA_METHOD_SBP_QR)}, "
        f"{method_label(PLATEGA_METHOD_CARD)} или {method_label(PLATEGA_METHOD_CRYPTO)}. "
        f"Хватает баланса — тариф сразу, иначе выставлю счёт.\n\n"
        f"{footer_hint()}"
    )


def format_tariff_activated(offer: Offer, settings: Settings) -> str:
    t = offer.tariff
    return (
        f"{header('✅', 'Тариф активирован')}\n\n"
        f"{kv('Тариф', t.name)}\n"
        f"{kv('Период', offer.period.label)}\n"
        f"{kv('Устройств', f'до {t.device_limit}')}\n\n"
        f"Доступ работает. «🔑 Ключ» — ссылка-подписка, «📦 Подписка» — срок и трафик."
    )


def format_tariff_needs_topup(offer: Offer, missing_rubles: str, *, method_code: int) -> str:
    copy = method_copy(method_code)
    label = method_label(method_code)
    return (
        f"{header('💳', 'Пополните баланс')}\n\n"
        f"«{offer.tariff.name} · {offer.period.label}» — {offer.period.price_label}, "
        f"не хватает {missing_rubles}. Счёт через {label}.\n\n"
        f"{copy['how']} {copy['timing']} Тариф активируется сам после оплаты."
    )


def format_tariffs(catalog: Catalog | None, settings: Settings) -> str:
    """Реальные тарифы из панели: трафик, устройства, цены по периодам."""
    if not catalog or not catalog.tariffs:
        return (
            f"{header('💎', 'Тарифы')}\n\n"
            f"Актуальный список тарифов и цены — в кабинете. "
            f"Открою его уже авторизованным: регистрация не нужна.\n\n"
            f"{footer_hint('кнопка ниже')}"
        )

    lines = [
        header("💎", "Тарифы"),
        "",
        "Все тарифы — с безлимитным трафиком, отличаются числом устройств "
        "и сроком. Чем дольше период, тем ниже цена месяца.",
        "",
    ]

    for tariff in catalog.tariffs:
        traffic = tariff.traffic_label or ("♾️ Безлимит" if tariff.unlimited_traffic else "—")
        lines.append(f"▸  {tariff.name}")
        lines.append(f"     Трафик: {traffic}")
        lines.append(f"     Устройств: до {tariff.device_limit}")
        for period in tariff.periods:
            per_month = (
                f"  ({period.per_month_label}/мес)"
                if period.per_month_label
                and period.per_month_label != period.price_label
                else ""
            )
            lines.append(f"     {period.label}: {period.price_label}{per_month}")
        if tariff.extra_device_kopeks:
            lines.append(
                f"     Доп. устройство: {tariff.extra_device_kopeks // 100} ₽"
            )
        lines.append("")

    lines.append(soft_rule())
    lines.append(
        f"Оплата тарифа списывается с баланса — пополнить можно кнопкой "
        f"«💳 Оплатить», от {catalog.min_topup_kopeks // 100} ₽. "
        f"Выбор тарифа и активация — в кабинете."
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

    trial = "уже использован" if user.is_trial_used else "🎁 доступен"
    key_preview = mask_key(user.vpn_key) if user.vpn_key else "ещё не выдан"

    lines = [
        header("📦", "Моя подписка"),
        "",
        kv("Статус", status),
        kv("Триал", trial),
        kv("Ключ", key_preview),
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
                lines.append(kv("Тариф", "триал"))
            if sub.get("traffic_limit_gb"):
                used = sub.get("traffic_used_gb", 0)
                limit = sub.get("traffic_limit_gb")
                percent = sub.get("traffic_used_percent")
                extra = f" ({percent}%)" if percent not in (None, 0) else ""
                lines.append(kv("Трафик", f"{used} / {limit} ГБ{extra}"))
            elif sub.get("traffic_limit_gb") == 0:
                lines.append(kv("Трафик", "♾️ безлимит"))
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
        lines.append(
            "Доступ работает. «🔑 Ключ» — ссылка, «♻️ Продлить» — продление. "
            "Ключ не меняется при оплате."
        )
    elif not user.is_trial_used:
        lines.append(
            f"Триал доступен: {settings.trial_days} дн., "
            f"{settings.trial_traffic_gb} ГБ, {_plural_devices(settings.trial_devices)}. "
            f"«🚀 Подключиться» → «🎁 Триал»."
        )
    else:
        lines.append(
            "Подписка неактивна. «💳 Оплатить» → «💎 Тарифы». Ключ сохранится."
        )

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
            f"{success_banner('Подписка активна')}\n\n"
            f"«🔑 Ключ» — ссылка-подписка для Happ. Добавьте один раз, "
            f"клиент сам обновляет серверы.\n\n"
            f"{footer_hint()}"
        )
    if not user.is_trial_used:
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner('Триал доступен')}\n\n"
            f"{settings.trial_days} дн., {settings.trial_traffic_gb} ГБ, "
            f"{_plural_devices(settings.trial_devices)}. Без карты — "
            f"нажмите «🎁 Триал».\n\n"
            f"{footer_hint()}"
        )

    price = catalog.entry_price_label if catalog else ""
    tail = f"Тарифы от {price}/мес. " if price else ""
    return (
        f"{header('🚀', 'Подключение')}\n\n"
        f"{warn_banner('Триал использован')}\n\n"
        f"{tail}"
        f"«💳 Оплатить» → «💎 Тарифы». Ключ не меняется.\n\n"
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
        head = header("🎁", f"Триал на {settings.trial_days} дня активирован")
        intro = f"Доступ открыт на {settings.trial_days} дня и уже работает."
    else:
        head = header("🔑", "Ваша ссылка подключения")
        intro = "Эту ссылку нужно один раз добавить в VPN-клиент."

    facts: list[str] = []
    if limits:
        if limits.get("traffic_limit_gb"):
            used = limits.get("traffic_used_gb") or 0
            facts.append(
                kv("Трафик", f"{used} / {limits['traffic_limit_gb']} ГБ")
            )
        elif limits.get("unlimited"):
            facts.append(kv("Трафик", "♾️ безлимит"))
        if limits.get("device_limit"):
            facts.append(kv("Устройств", f"до {limits['device_limit']}"))
        if limits.get("end_date"):
            facts.append(kv("Действует до", str(limits["end_date"])))
    elif is_trial:
        facts = [
            kv("Трафик", f"{settings.trial_traffic_gb} ГБ"),
            kv("Устройств", f"до {settings.trial_devices}"),
        ]

    facts_block = ("\n".join(facts) + "\n\n") if facts else ""

    return (
        f"{head}\n\n"
        f"{intro}\n\n"
        f"{facts_block}"
        f"{key}\n\n"
        f"{subhead('📋', 'Подключение')}\n"
        f"{step(1, 'Скопируйте ссылку целиком')}\n"
        f"{step(2, 'Happ → «+» → из буфера')}\n"
        f"{step(3, 'Включите VPN, разрешите в системе')}\n\n"
        f"Подписка обновляет серверы сама. Проверка — 2ip.ru. "
        f"Гайд по ОС — «📖 Помощь» → «📖 Гайд»."
    )


def format_buy(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('💎', 'Покупка подписки')}\n\n"
        f"Открою кабинет уже авторизованным — регистрация не потребуется. "
        f"Там вы увидите доступные тарифы с ценами, сроком и лимитом "
        f"устройств. После оплаты доступ активируется автоматически, "
        f"а ключ обновится в разделе «📦 Подписка».\n\n"
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
        f"Пополнение баланса (Platega). С баланса списывается тариф.\n\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_SBP_QR)} — {sbp['summary']}')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CARD)} — {card['summary']}')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CRYPTO)} — {crypto['summary']}')}\n\n"
        f"Выберите способ → сумма ({amounts} или своё, от {minimum} ₽). "
        f"Данные карты бот не видит.\n\n"
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
        f"Сумма: кнопка или число (от {minimum} ₽). {copy['timing']}\n\n"
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
        f"«✨ Оплатить» → Platega. {copy['how']} {copy['timing']}\n"
        f"Не зачислилось за 15 мин — «📖 Помощь» → «✍️ Вопрос».\n\n"
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
        f"{bullet(f'💰 {percent}% с оплат приглашённых')}\n"
        if percent
        else f"{bullet('💰 процент с оплат приглашённых')}\n"
    )

    body = (
        f"{header('👥', 'Рефералка')}\n\n"
        f"Приглашайте друзей — получайте бонусы.\n\n"
        f"{bonus_line}"
        f"{commission_line}\n"
    )

    if stats and stats.referral_code:
        body += (
            f"{subhead('🔗', 'Ваша ссылка')}\n"
            f"{stats.vk_link}\n\n"
            f"{kv('Код', stats.referral_code)}\n"
            f"{kv('Приглашено', stats.total_referrals)}\n"
            f"{kv('Активных', stats.active_referrals)}\n"
            f"{kv('Заработано', f'{stats.total_earnings_rubles:.0f} ₽')}\n\n"
            f"Скопируйте ссылку и отправьте другу — он должен открыть бот "
            f"по ней, чтобы вы получили бонус.\n\n"
        )
    else:
        body += (
            "Личная ссылка появится после входа в кабинет. "
            "Нажмите «🌐 Кабинет» ниже или подождите пару секунд.\n\n"
        )

    return f"{body}{footer_hint()}"


def format_referral_inviter_bonus(settings: Settings) -> str:
    days = settings.referral_inviter_bonus_days
    if days <= 0:
        return ""
    return (
        f"{success_banner('Реферал засчитан!')}\n\n"
        f"Спасибо, что пришли по приглашению. "
        f"Ваш друг получит +{days} дн. к подписке."
    )


def format_referral_bonus_notify(settings: Settings) -> str:
    days = settings.referral_inviter_bonus_days
    if days <= 0:
        return (
            f"{success_banner('Новый реферал!')}\n\n"
            f"По вашей ссылке зарегистрировался новый пользователь."
        )
    return (
        f"{success_banner('Новый реферал!')}\n\n"
        f"По вашей ссылке зарегистрировался друг — "
        f"мы добавили +{days} дн. к вашей подписке."
    )


def format_promo_prompt(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('🎟', 'Промокод')}\n\n"
        f"Отправьте код одним сообщением — например, {brand('PASKOD2026')}. "
        f"Регистр не важен, пробелы по краям я уберу сам.\n\n"
        f"Промокоды бывают трёх видов: скидка на покупку тарифа, "
        f"зачисление на баланс и добавочные дни к подписке. "
        f"Что именно даёт ваш код, будет видно при активации в кабинете — "
        f"я открою нужный раздел сразу после проверки.\n\n"
        f"{footer_hint('ждём ваш код')}"
    )


def format_apps(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('📱', 'Приложения')}\n\n"
        f"Ключ выдаётся по протоколу VLESS в виде ссылки-подписки, "
        f"поэтому нужен клиент, который её понимает. Рекомендуем Happ: "
        f"он есть на всех платформах и сам обновляет список серверов.\n\n"
        f"{subhead('✅', 'Где взять Happ')}\n"
        f"{bullet('iOS и macOS — App Store, поиск «Happ»')}\n"
        f"{bullet('Android — Google Play или APK с happ.su')}\n"
        f"{bullet('Windows — установщик с happ.su')}\n\n"
        f"{subhead('🔁', 'Альтернативы, если Happ не подошёл')}\n"
        f"{bullet('Android — v2rayNG, Hiddify')}\n"
        f"{bullet('iOS — Streisand, V2Box, Shadowrocket')}\n"
        f"{bullet('Windows — Hiddify, v2rayN, Nekoray')}\n"
        f"{bullet('macOS — Hiddify, Streisand, V2Box')}\n\n"
        f"Ссылка одинаково работает в любом из них: везде нужно добавить "
        f"её как подписку, а не как отдельный сервер. "
        f"Пошагово по вашей системе — в «📖 Гайд»."
    )


def format_renew_stub(settings: Settings, catalog: Catalog | None = None) -> str:
    price = catalog.entry_price_label if catalog else ""
    prices = f"Тарифы начинаются от {price} за месяц; " if price else ""
    long_hint = (
        "на трёх и шести месяцах месяц выходит дешевле."
        if catalog and catalog.tariffs
        else "длинные периоды дешевле в пересчёте на месяц."
    )

    if settings.bedolaga_api_key:
        return (
            f"{header('♻️', 'Продление')}\n\n"
            f"Быстрый вариант: напишите «продлить сейчас» — продлю доступ "
            f"на {settings.renew_days} дней и пришлю обновлённый ключ.\n\n"
            f"Если хотите сменить тариф или взять период подольше, "
            f"выберите его в кабинете. {prices}{long_hint} "
            f"Ссылка-подписка при продлении не меняется, так что "
            f"перенастраивать приложение не нужно.\n\n"
            f"{footer_hint()}"
        )
    return (
        f"{header('♻️', 'Продление')}\n\n"
        f"Продление оформляется в кабинете: пополняете баланс, выбираете "
        f"тариф и период. {prices}{long_hint}\n\n"
        f"Ключ при этом остаётся прежним — приложение подтянет доступ само, "
        f"заново добавлять подписку не придётся.\n\n"
        f"{footer_hint()}"
    )


def format_help_menu(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('📖', 'Помощь')}\n\n"
        f"{settings.support_text}\n\n"
        f"{bullet('✍️ Вопрос — напишите в чат, передам команде')}\n"
        f"{bullet('📖 Гайд — установка Happ по ОС')}\n"
        f"{bullet('ℹ️ Документы — оферта, FAQ, приватность')}\n"
        f"{bullet('👥 Рефералка — бонус за друзей')}\n"
        f"{bullet('🎟 Промо — активация кода')}\n\n"
        f"{footer_hint()}"
    )


def format_support(settings: Settings) -> str:
    return format_help_menu(settings)


def format_support_prompt() -> str:
    return (
        f"{header('✍️', 'Вопрос')}\n\n"
        f"Опишите проблему одним сообщением (можно со скриншотом). "
        f"«◀️ Назад» — отмена."
    )


def format_support_received(delivered: bool) -> str:
    tail = "Уведомление отправлено." if delivered else "Сообщение в диалоге сообщества."
    return (
        f"{header('✅', 'Принято')}\n\n"
        f"{tail} Ответ придёт сюда. Пока ждёте — FAQ в «ℹ️ Документы»."
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
        f"Нужна помощь — «📖 Помощь»."
    )


def format_info_menu(settings: Settings) -> str:
    _ = settings
    docs = " · ".join(f"{d.emoji} {d.title}" for d in ALL_DOCS)
    return (
        f"{header('ℹ️', 'Документы')}\n\n"
        f"{docs}\n\n"
        f"Нажмите документ под сообщением. Длинные тексты листаются кнопками.\n\n"
        f"{footer_hint()}"
    )


def format_cabinet_plain(settings: Settings, redirect: str = "/") -> str:
    """Кабинет без автологина — когда не настроен CABINET_JWT_SECRET."""
    return (
        f"{header('🌐', 'Личный кабинет')}\n\n"
        f"Автоматический вход сейчас недоступен, поэтому откройте кабинет "
        f"по ссылке и войдите обычным способом.\n\n"
        f"{settings.cabinet_url}{redirect}"
    )


def format_doc_not_found() -> str:
    return (
        f"{header('📄', 'Документ не найден')}\n\n"
        f"Похоже, этот раздел уже переименован или пока не заполнен. "
        f"Выберите документ из списка ниже — там актуальные версии."
    )


def format_min_amount(current: str, minimum: str = "50 ₽") -> str:
    return (
        f"{header('⚠️', 'Сумма слишком маленькая')}\n\n"
        f"Минимальная сумма пополнения — {minimum}, а вы указали {current}. "
        f"Выберите готовую сумму кнопкой ниже или отправьте другое число."
    )


def format_pay_method_first() -> str:
    return f"{header('💳', 'Сначала способ')}\n\n🏦 СБП · 💳 Карта · 🪙 Крипта — выберите кнопкой."


def format_amount_invalid() -> str:
    return f"{header('💵', 'Не понял')}\n\nЧисло сообщением или кнопка ниже."


def format_promo_accepted(code: str) -> str:
    return (
        f"{header('🎟', 'Промокод')}\n\n"
        f"{brand(code) if code.isascii() else code}\n\n"
        f"Активация в кабинете — открою по кнопке."
    )


def format_guide_outro() -> str:
    return "🏠  «◀️ Назад» — в меню."


def format_fallback() -> str:
    return (
        f"{header('🤔', 'Не понял')}\n\n"
        f"Кнопки внизу. Нет меню — /start. Вопрос — «📖 Помощь».\n\n"
        f"{footer_hint()}"
    )


def format_trial_used() -> str:
    return (
        f"{header('🎁', 'Триал использован')}\n\n"
        f"«💎 Тарифы» или «💳 Оплатить» — доступ вернётся, ключ тот же.\n\n"
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
