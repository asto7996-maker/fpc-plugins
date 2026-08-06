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
        f"{header('🔐', 'Вход без регистрации')}\n\n"
        f"Аккаунт для вас уже создан и привязан к профилю ВКонтакте, "
        f"так что придумывать email и пароль не нужно. "
        f"Нажмите кнопку ниже — откроется {where}, вы сразу окажетесь внутри "
        f"как авторизованный пользователь.\n\n"
        f"{bullet('Ссылка работает 72 часа')}\n"
        f"{bullet('Действует только для вашего аккаунта')}\n\n"
        f"{soft_rule()}\n"
        f"Если кнопка почему-то не открылась, скопируйте адрес:\n{url}"
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
        f"Я — {bot}. Помогаю подключить VPN по протоколу VLESS: "
        f"он работает через приложение Happ на iOS, Android, Windows и macOS.\n\n"
        f"Новым пользователям бесплатный триал: {trial}. "
        f"Карта не нужна — нажмите «🚀 Подключиться», и ключ придёт в этот чат. "
        f"{tariff_line}"
        f"Оплата — СБП по QR, банковской картой или криптовалютой, "
        f"от {(catalog.min_topup_kopeks // 100) if catalog else 50} ₽.\n\n"
        f"{bullet('Кабинет открывается без пароля — «🌐 Кабинет»')}\n"
        f"{bullet('Пошаговая настройка по ОС — «📖 Гайд»')}\n"
        f"{bullet('Документы и FAQ — «ℹ️ Инфо»')}\n\n"
        f"{soft_rule()}\n"
        f"{footer_hint('меню внизу экрана')}"
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
        "Все тарифы — с безлимитным трафиком, отличаются числом устройств. "
        "Чем длиннее период, тем дешевле месяц. Выберите вариант кнопкой "
        "ниже — цена уже с учётом периода.",
        "",
    ]
    for offer in catalog.offers():
        t = offer.tariff
        note = f"  ·  {offer.per_month_note}" if offer.per_month_note else ""
        lines.append(
            f"{offer.tariff.emoji}  {t.name} · {offer.period.label} — "
            f"{offer.period.price_label}{note}"
        )
        lines.append(f"       {_devices_upto(t.device_limit)}, безлимит трафика")
    lines.append("")
    lines.append(soft_rule())
    lines.append(
        "После выбора тарифа предложу способ оплаты. Оплата спишется "
        "с баланса; если его не хватает — пополните на нужную сумму, "
        "и тариф активируется автоматически."
    )
    return "\n".join(lines)


def format_tariff_chosen(offer: Offer) -> str:
    t = offer.tariff
    note = f" ({offer.per_month_note})" if offer.per_month_note else ""
    return (
        f"{header('💳', f'{t.name} · {offer.period.label}')}\n\n"
        f"{kv('Цена', offer.period.price_label + note)}\n"
        f"{kv('Трафик', t.traffic_label or '♾️ Безлимит')}\n"
        f"{kv('Устройства', _devices_upto(t.device_limit))}\n\n"
        f"Выберите способ оплаты:\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_SBP_QR)} — быстрый перевод по QR')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CARD)} — карта РФ онлайн')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CRYPTO)} — USDT и другие монеты')}\n\n"
        f"Если на балансе уже есть {offer.period.price_label}, тариф "
        f"активируется сразу. Иначе выставлю счёт на недостающую сумму.\n\n"
        f"{footer_hint('способ оплаты ниже')}"
    )


def format_tariff_activated(offer: Offer, settings: Settings) -> str:
    t = offer.tariff
    return (
        f"{header('✅', 'Тариф активирован')}\n\n"
        f"{kv('Тариф', t.name)}\n"
        f"{kv('Период', offer.period.label)}\n"
        f"{kv('Устройств', f'до {t.device_limit}')}\n\n"
        f"Доступ уже работает. Нажмите «🔑 Мой ключ», чтобы получить "
        f"ссылку-подписку, или откройте «📦 Подписка» — там срок и трафик."
    )


def format_tariff_needs_topup(offer: Offer, missing_rubles: str, *, method_code: int) -> str:
    copy = method_copy(method_code)
    label = method_label(method_code)
    return (
        f"{header('💳', 'Нужно пополнить баланс')}\n\n"
        f"Тариф «{offer.tariff.name} · {offer.period.label}» стоит "
        f"{offer.period.price_label}. Не хватает {missing_rubles} — "
        f"сформировал счёт через {label}.\n\n"
        f"{copy['how']}\n\n"
        f"Оплатите по кнопке ниже. {copy['timing']} Тариф активируется "
        f"автоматически — корзина уже сохранена, повторно выбирать ничего "
        f"не нужно."
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
            "Доступ работает. Ключ — кнопка «🔑 Мой ключ»; это ссылка-подписка, "
            "она не меняется при продлении. Продлевать лучше заранее: после "
            "окончания срока сервер отключает доступ сразу, а ключ остаётся "
            "тем же и снова заработает после оплаты."
        )
    elif not user.is_trial_used:
        lines.append(
            f"Активной подписки нет, зато доступен бесплатный триал: "
            f"{settings.trial_days} дня, {settings.trial_traffic_gb} ГБ, "
            f"{_plural_devices(settings.trial_devices)}. "
            f"Нажмите «🚀 Подключиться» — выдам ключ сразу, без оплаты."
        )
    else:
        lines.append(
            "Подписка неактивна, доступ закрыт. Чтобы включить его снова: "
            "пополните баланс кнопкой «💳 Оплатить», затем выберите тариф "
            "в кабинете. Ключ сохранится — перенастраивать приложение не нужно."
        )

    lines.append("")
    lines.append(soft_rule())
    lines.append(f"🌐  Трафик по дням и устройства: {settings.cabinet_url}/subscription")
    return "\n".join(lines)


def format_profile(user: User, settings: Settings) -> str:
    return format_subscription_card(user, settings)


def format_connect_screen(
    user: User, settings: Settings, catalog: Catalog | None = None
) -> str:
    if user.is_subscription_active():
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner('Подписка активна — доступ уже работает')}\n\n"
            f"Нажмите «🔑 Мой ключ» — пришлю ссылку-подписку вида "
            f"sub.paskod.ru. Её нужно один раз добавить в Happ: клиент сам "
            f"подтянет серверы и будет обновлять их при изменениях, "
            f"заново копировать ничего не придётся.\n\n"
            f"{bullet('Остаток трафика и устройства — «📦 Подписка»')}\n"
            f"{bullet('Настройка по вашей ОС — «📖 Гайд»')}\n\n"
            f"{footer_hint()}"
        )
    if not user.is_trial_used:
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner('Бесплатный триал доступен')}\n\n"
            f"Что входит: {settings.trial_days} дня, "
            f"{settings.trial_traffic_gb} ГБ трафика, "
            f"{_plural_devices(settings.trial_devices)}. "
            f"Ни карты, ни предоплаты — нажмите «🎁 Бесплатный триал», "
            f"и ссылка придёт сюда же за несколько секунд.\n\n"
            f"Настройка занимает около минуты: установить Happ, вставить "
            f"ссылку из буфера, включить VPN. Триал выдаётся один раз "
            f"на аккаунт; когда он закончится, доступ продлевается тарифом.\n\n"
            f"{footer_hint('активируйте триал')}"
        )

    price = catalog.entry_price_label if catalog else ""
    tail = (
        f"Тарифы начинаются от {price} в месяц, трафик безлимитный. "
        if price
        else "Тарифы и цены — в кабинете. "
    )
    return (
        f"{header('🚀', 'Подключение')}\n\n"
        f"{warn_banner('Бесплатный триал уже использован')}\n\n"
        f"{tail}"
        f"Порядок такой: пополняете баланс кнопкой «💳 Оплатить» "
        f"(СБП, карта или крипта), затем выбираете тариф в кабинете — "
        f"доступ включается сразу, а ключ остаётся тем же.\n\n"
        f"Если подписка закончилась раньше срока или ключ перестал "
        f"подключаться, напишите в «💬 Помощь» — проверим по панели.\n\n"
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
        f"{subhead('📋', 'Как подключить')}\n"
        f"{step(1, 'Скопируйте ссылку целиком — от https до конца')}\n"
        f"{step(2, 'Установите Happ, если его ещё нет')}\n"
        f"{step(3, 'В Happ: «+» → «Добавить из буфера»')}\n"
        f"{step(4, 'Включите профиль и разрешите VPN в системе')}\n\n"
        f"Это ссылка-подписка: клиент сам получает список серверов и "
        f"обновляет их, когда мы что-то меняем. Добавлять её заново не нужно — "
        f"достаточно нажать «Обновить» в приложении.\n\n"
        f"Одну и ту же ссылку можно поставить на все свои устройства "
        f"в пределах лимита тарифа. Проверить, что VPN включился, проще "
        f"всего на 2ip.ru — страна должна отличаться от вашей.\n\n"
        f"{soft_rule()}\n"
        f"Инструкции по iOS, Android, Windows и macOS — в «📖 Гайд»."
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
    price = catalog.entry_price_label if catalog else ""
    hint = (
        f"Для справки: самый доступный тариф стоит {price} за месяц — "
        f"пополнения хватит сразу на подписку. "
        if price
        else ""
    )
    sbp = method_copy(PLATEGA_METHOD_SBP_QR)
    card = method_copy(PLATEGA_METHOD_CARD)
    crypto = method_copy(PLATEGA_METHOD_CRYPTO)
    return (
        f"{header('💰', 'Баланс')}\n\n"
        f"Баланс — внутренний счёт, с которого списывается оплата тарифа. "
        f"Пополнить можно прямо здесь тремя способами:\n\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_SBP_QR)} — {sbp['summary']}')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CARD)} — {card['summary']}')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CRYPTO)} — {crypto['summary']}')}\n\n"
        f"Минимум {minimum} ₽. {hint}"
        f"Зачисление автоматическое, обычно за 1–5 минут. Остаток и история — "
        f"в кабинете и в «📦 Подписка».\n\n"
        f"{footer_hint('выберите способ оплаты')}"
    )


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
        f"Пополнение баланса через Platega — те же способы, что в мини-приложении. "
        f"После зачисления деньги списываются с баланса при покупке тарифа.\n\n"
        f"{subhead('🏦', sbp['title'])}\n"
        f"{sbp['summary']} {sbp['note']}\n\n"
        f"{subhead('💳', card['title'])}\n"
        f"{card['summary']} {card['note']}\n\n"
        f"{subhead('🪙', crypto['title'])}\n"
        f"{crypto['summary']} {crypto['note']}\n\n"
        f"Выберите способ кнопкой ниже. Потом укажите сумму: готовые "
        f"({amounts}) или своё число от {minimum} ₽.\n\n"
        f"Бот не видит и не хранит данные карты, CVV и коды из SMS — "
        f"всё проходит на стороне платёжного провайдера.\n\n"
        f"{footer_hint()}"
    )


def format_pay_amount_prompt(
    method_code: int, catalog: Catalog | None = None
) -> str:
    minimum = (catalog.min_topup_kopeks // 100) if catalog else 50
    price = catalog.entry_price_label if catalog else ""
    hint = f"Месяц самого доступного тарифа — {price}. " if price else ""
    copy = method_copy(method_code)
    label = method_label(method_code)
    return (
        f"{header('💵', label)}\n\n"
        f"{copy['summary']}\n\n"
        f"{copy['how']}\n\n"
        f"Укажите сумму: нажмите готовую кнопку или отправьте число "
        f"сообщением, например 200. Минимум {minimum} ₽. {hint}"
        f"{copy['timing']} Сумма зачисляется на баланс целиком.\n\n"
        f"{footer_hint('кнопки ниже или числом')}"
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
        f"Нажмите «✨ Оплатить сейчас» — откроется страница Platega. "
        f"{copy['how']}\n\n"
        f"{copy['timing']} Если деньги списались, а баланс не изменился "
        f"за 10–15 минут, напишите в «💬 Помощь» и укажите время платежа "
        f"и способ ({copy['title']}).\n\n"
        f"{soft_rule()}\n"
        f"Если кнопка не сработала, откройте ссылку вручную:\n{payment_url}"
    )


def format_referral(settings: Settings, catalog: Catalog | None = None) -> str:
    _ = settings
    percent = catalog.referral_percent if catalog else 0
    rate = (
        f"Вы получаете {percent}% от каждой оплаты приглашённого — "
        f"не с первой покупки, а со всех последующих тоже. "
        if percent
        else "Вы получаете процент от каждой оплаты приглашённого. "
    )
    return (
        f"{header('👥', 'Партнёрская программа')}\n\n"
        f"{rate}"
        f"Начисления приходят на внутренний баланс: ими можно оплатить "
        f"свою подписку или вывести.\n\n"
        f"В кабинете вы найдёте личную ссылку с промокодом, число переходов, "
        f"сколько из них стали платящими, и общую сумму заработка. "
        f"Открою этот раздел по кнопке ниже — вход без регистрации.\n\n"
        f"{footer_hint()}"
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


def format_support(settings: Settings) -> str:
    return (
        f"{header('💬', 'Помощь')}\n\n"
        f"{settings.support_text}\n\n"
        f"Задать вопрос можно прямо здесь: нажмите «✍️ Задать вопрос» "
        f"и опишите ситуацию одним сообщением — я передам его команде. "
        f"Чем конкретнее, тем быстрее ответим: пригодятся устройство, "
        f"приложение и шаг, на котором всё остановилось. Если вопрос "
        f"про оплату, добавьте время платежа и способ.\n\n"
        f"{bullet('Отвечаем в этом же диалоге')}\n"
        f"{bullet('Готовые ответы и документы — в разделе «ℹ️ Инфо»')}"
    )


def format_support_prompt() -> str:
    return (
        f"{header('✍️', 'Слушаю вас')}\n\n"
        f"Опишите проблему или вопрос одним сообщением — я передам его "
        f"команде поддержки. Скриншот тоже можно приложить: "
        f"по картинке часто понятнее. Если передумали, нажмите «◀️ Назад»."
    )


def format_support_received(delivered: bool) -> str:
    tail = (
        "Команда уже получила уведомление."
        if delivered
        else "Обращение осталось в этом диалоге — команда его увидит."
    )
    return (
        f"{header('✅', 'Вопрос принят')}\n\n"
        f"Спасибо, я передал ваше сообщение. {tail} "
        f"Ответ придёт сюда же, в этот чат, — обычно в течение дня.\n\n"
        f"Пока ждёте, можно посмотреть «💬 FAQ» в разделе «ℹ️ Инфо»: "
        f"частые вопросы там уже разобраны."
    )


def format_admin_menu(settings: Settings) -> str:
    """Экран администратора — только для главного админа."""
    username = settings.main_admin_username or "администратор"
    pseudo = 8_000_000_000 + settings.main_admin_vk_id
    return (
        f"{header('🛠', 'Администрирование')}\n\n"
        f"Привет, @{username}! Здесь управление сервисом Paskod: "
        f"тарифы, пользователи, тикеты и настройки мини-приложения.\n\n"
        f"{bullet('Все вопросы из «Помощи» приходят вам в личку')}\n"
        f"{bullet('Панель кабинета открывается с автовходом')}\n\n"
        f"{subhead('📊', 'Кабинет')}\n"
        f"Кнопки ниже ведут в админ-панель {brand('Paskod')} — "
        f"тот же интерфейс, что в браузере. Если доступа нет, проверьте "
        f"ADMIN_IDS в Bedolaga: для VK нужен псевдо-ID {pseudo}.\n\n"
        f"{footer_hint('выберите раздел')}"
    )


def format_admin_denied() -> str:
    return (
        f"{warn_banner('Раздел только для администратора')}\n\n"
        f"Эта кнопка доступна главному админу сервиса. "
        f"Если вам нужна помощь — откройте «💬 Помощь»."
    )


def format_info_menu(settings: Settings) -> str:
    _ = settings
    lines = [
        header("ℹ️", "Информация о сервисе"),
        "",
        "Здесь собраны все документы Paskod — они открываются прямо в чате, "
        "без переходов на сайт. Длинные тексты разбиты на страницы, "
        "листать можно кнопками.",
        "",
    ]
    for doc in ALL_DOCS:
        lines.append(f"{doc.emoji}  {doc.title}")
        lines.append(f"     {doc.summary}")
        lines.append("")
    lines.append(soft_rule())
    lines.append(footer_hint("выберите документ кнопкой ниже"))
    return "\n".join(lines)


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
    return (
        f"{header('💳', 'Сначала выберите способ оплаты')}\n\n"
        f"Чтобы выставить счёт, выберите один из трёх способов:\n\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_SBP_QR)} — QR в приложении банка')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CARD)} — карта российского банка')}\n"
        f"{bullet(f'{method_label(PLATEGA_METHOD_CRYPTO)} — криптовалюта')}\n\n"
        f"После выбора я спрошу сумму пополнения."
    )


def format_amount_invalid() -> str:
    return (
        f"{header('💵', 'Не понял сумму')}\n\n"
        f"Отправьте её числом — например, 100 или 250. "
        f"Или просто нажмите одну из кнопок ниже."
    )


def format_promo_accepted(code: str) -> str:
    return (
        f"{header('🎟', 'Промокод принят')}\n\n"
        f"  {brand(code) if code.isascii() else code}\n\n"
        f"Активировать код нужно в кабинете: открою раздел подписки, "
        f"там появится поле для промокода. Вход произойдёт автоматически, "
        f"вводить логин и пароль не потребуется. Если код не примется, "
        f"проверьте срок действия или напишите в «💬 Помощь»."
    )


def format_guide_outro() -> str:
    return (
        "🏠  Выбрали не ту систему или всё уже настроено? "
        "Возвращайтесь в меню — оно снова внизу экрана."
    )


def format_loading(text: str = "✨ Секунду…") -> str:
    return text


def format_error(text: str) -> str:
    return f"{header('❌', 'Не получилось')}\n\n{error_banner(text)}"


def format_fallback() -> str:
    return (
        f"{header('🤔', 'Не совсем понял')}\n\n"
        f"Я реагирую на кнопки меню — они внизу экрана. "
        f"Если меню не видно, отправьте /start, и я покажу его заново. "
        f"А с любым вопросом всегда можно прийти в «💬 Помощь».\n\n"
        f"{footer_hint()}"
    )


def format_trial_used() -> str:
    return (
        f"{header('🎁', 'Триал уже использован')}\n\n"
        f"Бесплатный период выдаётся один раз на аккаунт, поэтому для "
        f"дальнейшего доступа нужен тариф. Купите подписку в кабинете "
        f"или пополните баланс — доступ включится автоматически, "
        f"а ключ придёт в чат.\n\n"
        f"{footer_hint()}"
    )


def days_left(user: User) -> int:
    if not user.subscription_end:
        return 0
    end = user.subscription_end
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)
    return max(0, (end - datetime.utcnow()).days)
