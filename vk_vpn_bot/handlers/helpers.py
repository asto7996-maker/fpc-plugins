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
from services.vpn_keys import mask_key


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


def format_welcome(settings: Settings, first_name: str | None = None) -> str:
    name = (first_name or "").strip()
    hello = f"Привет, {name}!" if name else "Привет!"
    bot = brand(settings.bot_name) if settings.bot_name.isascii() else settings.bot_name

    return (
        f"✨  {hello}\n"
        f"{card_rule()}\n\n"
        f"Я — {bot}, помогу подключить быстрый и стабильный VPN за пару минут.\n\n"
        f"Новым пользователям доступен бесплатный триал на "
        f"{settings.trial_days} дня: карта не нужна, достаточно нажать "
        f"«🚀 Подключиться». Дальше подписку можно продлить прямо здесь — "
        f"оплата проходит через СБП, банковскую карту или криптовалюту. "
        f"Личный кабинет открывается одним тапом, без email и пароля, "
        f"потому что аккаунт уже привязан к вашему профилю ВКонтакте.\n\n"
        f"{bullet('Инструкции по устройствам — «📖 Гайд»')}\n"
        f"{bullet('Документы сервиса — «ℹ️ Инфо»')}\n\n"
        f"{soft_rule()}\n"
        f"{footer_hint('меню внизу экрана')}"
    )


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
    if active:
        lines.append(
            "Доступ работает — ключ можно получить кнопкой «🔑 Мой ключ». "
            "Продлевать подписку лучше заранее, чтобы соединение не прерывалось."
        )
    elif not user.is_trial_used:
        lines.append(
            "Активной подписки пока нет, зато доступен бесплатный триал. "
            "Нажмите «🚀 Подключиться», и я выдам ключ сразу — оплата не потребуется."
        )
    else:
        lines.append(
            "Подписка неактивна, поэтому доступ сейчас закрыт. "
            "Выберите тариф в кабинете или пополните баланс — "
            "ключ обновится автоматически."
        )

    lines.append("")
    lines.append(soft_rule())
    lines.append(f"🌐  Подробная статистика: {settings.cabinet_url}/subscription")
    return "\n".join(lines)


def format_profile(user: User, settings: Settings) -> str:
    return format_subscription_card(user, settings)


def format_connect_screen(user: User, settings: Settings) -> str:
    if user.is_subscription_active():
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner('Подписка активна — доступ уже работает')}\n\n"
            f"Нажмите «🔑 Мой ключ», чтобы получить ссылку для импорта "
            f"в приложение Happ. Если хотите посмотреть остаток трафика "
            f"и подключённые устройства, откройте кабинет. "
            f"Настраиваете VPN впервые? В разделе «📖 Гайд» есть пошаговые "
            f"инструкции для iOS, Android, Windows и macOS.\n\n"
            f"{footer_hint()}"
        )
    if not user.is_trial_used:
        return (
            f"{header('🚀', 'Подключение')}\n\n"
            f"{success_banner(f'Доступен бесплатный триал на {settings.trial_days} дня')}\n\n"
            f"Карта и предоплата не нужны — просто нажмите "
            f"«🎁 Бесплатный триал», и я сразу пришлю ссылку подключения. "
            f"Настройка занимает около минуты: скопировать ссылку, "
            f"вставить в клиент, включить VPN. Триал даётся один раз, "
            f"а по его окончании доступ можно продлить любым тарифом.\n\n"
            f"{footer_hint('активируйте триал')}"
        )
    return (
        f"{header('🚀', 'Подключение')}\n\n"
        f"{warn_banner('Бесплатный триал уже использован')}\n\n"
        f"Чтобы подключиться снова, нужен тариф: купите подписку в кабинете "
        f"или сначала пополните баланс — принимаем СБП, банковские карты "
        f"и криптовалюту. Если доступ пропал раньше срока или что-то "
        f"не работает, напишите в «💬 Помощь» — разберёмся.\n\n"
        f"{footer_hint()}"
    )


def format_key_message(
    key: str,
    settings: Settings,
    *,
    is_trial: bool,
    source: str = "local",
) -> str:
    _ = source
    if is_trial:
        head = header("🎁", f"Триал на {settings.trial_days} дня активирован")
        intro = (
            f"Готово — доступ открыт на {settings.trial_days} дня, "
            f"ссылка ниже уже работает."
        )
    else:
        head = header("🔑", "Ваша ссылка подключения")
        intro = "Эту ссылку нужно импортировать в VPN-клиент — она ниже."

    return (
        f"{head}\n\n"
        f"{intro}\n\n"
        f"{key}\n\n"
        f"{subhead('📋', 'Как подключить')}\n"
        f"{step(1, 'Скопируйте ссылку целиком')}\n"
        f"{step(2, 'Откройте Happ → «+» → вставить из буфера')}\n"
        f"{step(3, 'Включите профиль и разрешите VPN')}\n\n"
        f"Ссылка одна для всех ваших устройств в пределах лимита тарифа. "
        f"Если соединение не поднимается, смените сервер или режим "
        f"маршрутизации в приложении.\n\n"
        f"{soft_rule()}\n"
        f"Пошаговые инструкции по системам — в разделе «📖 Гайд»."
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


def format_balance(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('💰', 'Баланс')}\n\n"
        f"Пополнить баланс можно прямо здесь, в чате: выберите способ оплаты, "
        f"укажите сумму — и я пришлю ссылку на счёт. Деньги с баланса "
        f"списываются при покупке или продлении тарифа, поэтому это удобно, "
        f"если хотите оплатить заранее. Историю операций видно в кабинете.\n\n"
        f"{footer_hint('выберите способ оплаты')}"
    )


def format_pay_intro() -> str:
    return (
        f"{header('💳', 'Оплата')}\n\n"
        f"Доступны те же способы, что и в мини-приложении — выберите удобный:\n\n"
        f"{bullet('🏦  СБП · QR — оплата по QR в банковском приложении')}\n"
        f"{bullet('💳  Банковская карта — обычная оплата онлайн')}\n"
        f"{bullet('🪙  Криптовалюта — для тех, кому так привычнее')}\n\n"
        f"После выбора я спрошу сумму и сформирую счёт. "
        f"Платёж проходит на защищённой странице провайдера — "
        f"реквизиты карты бот не видит и не хранит.\n\n"
        f"{footer_hint()}"
    )


def format_pay_amount_prompt(method_label: str) -> str:
    return (
        f"{header('💵', method_label)}\n\n"
        f"Укажите сумму пополнения: нажмите одну из кнопок ниже "
        f"или отправьте своё число сообщением, например 200. "
        f"Минимальная сумма — 50 ₽.\n\n"
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
        f"Нажмите «✨ Оплатить сейчас» — откроется защищённая страница "
        f"платёжного провайдера. Баланс обновится автоматически, "
        f"обычно в течение минуты после оплаты. Если деньги списались, "
        f"но баланс не изменился за 10–15 минут, напишите в «💬 Помощь» "
        f"и приложите время платежа.\n\n"
        f"{soft_rule()}\n"
        f"Если кнопка не сработала, откройте ссылку вручную:\n{payment_url}"
    )


def format_referral(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('👥', 'Партнёрская программа')}\n\n"
        f"Приводите друзей и получайте вознаграждение за их оплаты. "
        f"Личная ссылка, статистика переходов и вывод средств — в кабинете, "
        f"отдельная регистрация не нужна. Открою нужный раздел по кнопке ниже.\n\n"
        f"{footer_hint()}"
    )


def format_promo_prompt(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('🎟', 'Промокод')}\n\n"
        f"Отправьте код одним сообщением — например, {brand('PASKOD2026')}. "
        f"Регистр не важен, но лишние пробелы лучше убрать. "
        f"После проверки я подскажу, что делать дальше.\n\n"
        f"{footer_hint('ждём ваш код')}"
    )


def format_apps(settings: Settings) -> str:
    _ = settings
    return (
        f"{header('📱', 'Приложения')}\n\n"
        f"Рекомендую Happ — он простой, стабильный и есть на всех платформах. "
        f"Ключ из бота импортируется в него за пару касаний.\n\n"
        f"Если Happ по какой-то причине не подходит, подойдут и другие клиенты:\n"
        f"{bullet('v2rayNG — Android')}\n"
        f"{bullet('Hiddify — Android, Windows, macOS')}\n"
        f"{bullet('Streisand — iOS, macOS')}\n"
        f"{bullet('V2Box — iOS')}\n\n"
        f"Ссылка подключения одинаково работает в любом из них. "
        f"Пошаговую настройку под вашу систему найдёте в «📖 Гайд»."
    )


def format_renew_stub(settings: Settings) -> str:
    if settings.bedolaga_api_key:
        return (
            f"{header('♻️', 'Продление')}\n\n"
            f"Могу продлить доступ на {settings.renew_days} дней автоматически — "
            f"напишите «продлить сейчас», и я выдам обновлённый ключ. "
            f"Если нужен другой срок или тариф, выберите подходящий в кабинете: "
            f"после оплаты подписка продлится сама.\n\n"
            f"{footer_hint()}"
        )
    return (
        f"{header('♻️', 'Продление')}\n\n"
        f"Продление оформляется в кабинете: выберите тариф и оплатите — "
        f"ключ обновится автоматически, заново настраивать приложение "
        f"не придётся. Открою нужный раздел по кнопке ниже.\n\n"
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
        f"Чтобы выставить счёт, мне нужно знать, чем вы будете платить: "
        f"СБП, картой или криптовалютой. Выберите вариант кнопкой ниже, "
        f"и я спрошу сумму."
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
