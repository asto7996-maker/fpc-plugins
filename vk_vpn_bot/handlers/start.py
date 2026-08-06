"""
Обработчики VK-бота — функциональный паритет с Telegram Bedolaga/Paskod.

Разделы:
  Подключиться / триал / ключ
  Моя подписка / профиль
  Купить / Баланс / Партнёрка → кабинет
  Промокод (ввод в чат)
  Приложения / Гайд / Поддержка
  Инфо / документы (соглашение, приватность, оферта, правила, FAQ)
"""

from __future__ import annotations

import json
import logging
import re

from vkbottle import GroupEventType, GroupTypes, ShowSnackbarEvent
from vkbottle.bot import BotLabeler, Message

from config import get_settings
from database import get_or_create_user
from handlers.helpers import (
    format_amount_invalid,
    format_apps,
    format_auto_login_message,
    format_balance,
    format_cabinet_plain,
    format_connect_screen,
    format_doc_not_found,
    format_error,
    format_fallback,
    format_guide_outro,
    format_info_menu,
    format_key_message,
    format_min_amount,
    format_pay_amount_prompt,
    format_pay_intro,
    format_pay_method_first,
    format_payment_created,
    format_promo_accepted,
    format_promo_prompt,
    format_renew_stub,
    format_subscription_card,
    format_support,
    format_trial_used,
    format_welcome,
)
from handlers.texts import GUIDE_INTRO, GUIDES, OS_TITLES
from keyboards import (
    BTN_APPS,
    BTN_BACK,
    BTN_BALANCE,
    BTN_BUY,
    BTN_CABINET,
    BTN_CABINET_LOGIN,
    BTN_CONNECT,
    BTN_CONNECT_OLD,
    BTN_DOC_LIST,
    BTN_DOC_NEXT,
    BTN_DOC_PREV,
    BTN_FAQ,
    BTN_GET_KEY,
    BTN_GUIDE,
    BTN_INFO,
    BTN_MY_KEY,
    BTN_OFFER,
    BTN_PAY,
    BTN_PAY_CARD,
    BTN_PAY_CRYPTO,
    BTN_PAY_SBP,
    BTN_PRIVACY,
    BTN_PROFILE,
    BTN_PROMO,
    BTN_REFERRAL,
    BTN_RENEW,
    BTN_RULES,
    BTN_SUBSCRIPTION,
    BTN_SUPPORT,
    BTN_TERMS,
    BTN_TRIAL,
    PAY_AMOUNT_BUTTONS,
    apps_keyboard,
    auto_login_keyboard,
    connect_keyboard,
    doc_keyboard,
    doc_nav_keyboard,
    guide_os_keyboard,
    info_keyboard,
    main_menu_keyboard,
    pay_amounts_keyboard,
    pay_methods_keyboard,
    payment_link_keyboard,
    promo_keyboard,
    subscription_keyboard,
    support_keyboard,
)
from legal.documents import DOCS_BY_SLUG, format_doc_for_bot
from services.bedolaga import get_bedolaga_client
from services.cabinet_auth import ensure_auto_login_url, ensure_panel_user
from services.keys_service import issue_renewal, issue_trial_or_key
from services.payments import (
    METHOD_LABELS,
    PLATEGA_METHOD_CARD,
    PLATEGA_METHOD_CRYPTO,
    PLATEGA_METHOD_SBP_QR,
    CabinetPaymentClient,
    CabinetPaymentError,
    ensure_bedolaga_id_for_payments,
    format_rubles,
)

logger = logging.getLogger(__name__)

labeler = BotLabeler()
labeler.vbml_ignore_case = True

# Ожидание промокода от пользователя (vk_id → True)
_waiting_promo: set[int] = set()
# Ожидание суммы оплаты: vk_id → код метода Platega (2/11/13)
_pending_pay_method: dict[int, int] = {}
# Чтение документа в боте: vk_id → {"slug": str, "page": int, "total": int}
_reading_doc: dict[int, dict[str, int | str]] = {}

# Кнопка текста → slug документа
_DOC_BUTTONS: dict[str, str] = {
    BTN_PRIVACY: "privacy",
    BTN_TERMS: "terms",
    BTN_OFFER: "offer",
    BTN_RULES: "rules",
    BTN_FAQ: "faq",
    "приватность": "privacy",
    "политика": "privacy",
    "политика конфиденциальности": "privacy",
    "соглашение": "terms",
    "пользовательское соглашение": "terms",
    "оферта": "offer",
    "публичная оферта": "offer",
    "правила": "rules",
    "правила сервиса": "rules",
    "faq": "faq",
    "частые вопросы": "faq",
}


async def _ensure_user(message: Message):
    first_name = None
    try:
        users = await message.ctx_api.users.get(user_ids=[message.from_id])
        if users:
            first_name = users[0].first_name
    except Exception as exc:  # noqa: BLE001
        logger.warning("users.get failed for %s: %s", message.from_id, exc)
    return await get_or_create_user(message.from_id, first_name=first_name)


def _cab() -> str:
    return get_settings().cabinet_url


async def _send_doc(message: Message, slug: str, page: int = 1) -> None:
    """Показывает документ целиком в VK-чате с кнопками листания."""
    doc = DOCS_BY_SLUG.get(slug)
    if not doc:
        _reading_doc.pop(message.from_id, None)
        await message.answer(
            format_doc_not_found(),
            keyboard=info_keyboard(_cab()),
        )
        return
    text, total = format_doc_for_bot(doc, page=page)
    page = max(1, min(page, total))
    _reading_doc[message.from_id] = {"slug": slug, "page": page, "total": total}
    await message.answer(
        text,
        keyboard=doc_nav_keyboard(page=page, total=total),
    )


async def _open_cabinet(
    message: Message,
    *,
    redirect: str = "/",
    title: str = "✨ Секунду…",
    button_text: str = "✨ Открыть",
) -> None:
    """Авторегистрация + автологин в кабинет без email/пароля."""
    settings = get_settings()
    user = await _ensure_user(message)

    if not settings.cabinet_jwt_secret:
        await message.answer(
            format_cabinet_plain(settings, redirect),
            keyboard=main_menu_keyboard(_cab()),
        )
        return

    try:
        await message.answer(title)
        url, _bid = await ensure_auto_login_url(
            user.user_id,
            user.first_name,
            settings,
            redirect=redirect,
        )
        await message.answer(
            format_auto_login_message(url, settings, redirect=redirect),
            keyboard=auto_login_keyboard(url, button_text=button_text),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("cabinet open failed: %s", exc)
        await message.answer(
            format_error(
                "Кабинет не отвечает — скорее всего, это временный сбой. "
                "Попробуйте ещё раз через минуту, а если не поможет, "
                "напишите в «💬 Помощь», и мы посмотрим со своей стороны."
            ),
            keyboard=main_menu_keyboard(_cab()),
        )



# ---------------------------------------------------------------------------
# Старт / меню
# ---------------------------------------------------------------------------


@labeler.private_message(
    text=[
        "/start",
        "начать",
        "старт",
        "меню",
        BTN_BACK,
        "◀️ Назад в меню",
        "Назад",
    ]
)
async def cmd_start(message: Message):
    settings = get_settings()
    _waiting_promo.discard(message.from_id)
    _pending_pay_method.pop(message.from_id, None)
    _reading_doc.pop(message.from_id, None)
    user = await _ensure_user(message)

    if settings.cabinet_jwt_secret and settings.bedolaga_api_key:
        try:
            await ensure_panel_user(user.user_id, user.first_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("silent panel register failed: %s", exc)

    await message.answer(
        format_welcome(settings, user.first_name),
        keyboard=main_menu_keyboard(_cab()),
    )



# ---------------------------------------------------------------------------
# Подключиться / триал / ключ
# ---------------------------------------------------------------------------


@labeler.private_message(
    text=[
        BTN_CONNECT,
        BTN_CONNECT_OLD,
        "🚀 Подключиться",
        "Подключиться",
        "подключить vpn",
        "подключить",
        "подключиться",
    ]
)
async def cmd_connect(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)
    await message.answer(
        format_connect_screen(user, settings),
        keyboard=connect_keyboard(
            has_active=user.is_subscription_active(),
            trial_available=not user.is_trial_used,
            cabinet_url=_cab(),
        ),
    )


@labeler.private_message(
    text=[
        BTN_TRIAL,
        BTN_GET_KEY,
        BTN_MY_KEY,
        "🎁 Активировать триал",
        "🎁 Бесплатный триал",
        "Бесплатный триал",
        "🔑 Мой ключ",
        "🔑 Получить ключ",
        "Мой ключ",
        "получить ключ",
        "мой ключ",
        "активировать триал",
        "триал",
    ]
)
async def cmd_get_key(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)
    text = (message.text or "").lower()
    want_trial = any(x in text for x in ("триал", "trial", "активировать", "бесплатный"))
    want_key_only = "ключ" in text and not want_trial

    if user.is_subscription_active() and user.vpn_key and not want_trial:
        await message.answer(
            format_key_message(
                user.vpn_key,
                settings,
                is_trial=False,
                source="bedolaga" if user.bedolaga_user_id else "local",
            ),
            keyboard=connect_keyboard(
                has_active=True,
                trial_available=False,
                cabinet_url=_cab(),
            ),
        )
        return

    if not user.is_trial_used:
        await message.answer("✨ Секунду — готовлю триал…")
        result = await issue_trial_or_key(user, settings)
        await message.answer(
            format_key_message(
                result.key, settings, is_trial=True, source=result.source
            ),
            keyboard=connect_keyboard(
                has_active=True,
                trial_available=False,
                cabinet_url=_cab(),
            ),
        )
        return

    if want_key_only and user.vpn_key:
        await message.answer(
            format_key_message(
                user.vpn_key,
                settings,
                is_trial=False,
                source="bedolaga" if user.bedolaga_user_id else "local",
            ),
            keyboard=connect_keyboard(
                has_active=user.is_subscription_active(),
                trial_available=False,
                cabinet_url=_cab(),
            ),
        )
        return

    await message.answer(
        format_trial_used(),
        keyboard=connect_keyboard(
            has_active=False,
            trial_available=False,
            cabinet_url=_cab(),
        ),
    )



# ---------------------------------------------------------------------------
# Подписка / профиль
# ---------------------------------------------------------------------------


@labeler.private_message(
    text=[
        BTN_SUBSCRIPTION,
        BTN_PROFILE,
        "📦 Моя подписка",
        "📦 Подписка",
        "Подписка",
        "👤 Профиль",
        "Профиль",
        "профиль",
        "подписка",
        "моя подписка",
    ]
)
async def cmd_subscription(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)
    panel = None
    client = get_bedolaga_client()
    if client and client.enabled:
        try:
            panel = await client.get_panel_user_for_vk(
                user.user_id, first_name=user.first_name
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("panel user fetch failed: %s", exc)

    await message.answer(
        format_subscription_card(user, settings, panel),
        keyboard=subscription_keyboard(_cab(), has_key=bool(user.vpn_key)),
    )


@labeler.private_message(
    text=[
        BTN_CABINET,
        BTN_CABINET_LOGIN,
        "🌐 Кабинет",
        "Кабинет",
        "🔐 Войти без регистрации",
        "🔐 Войти",
        "Войти",
        "войти",
        "кабинет",
        "войти в кабинет",
        "автологин",
        "без регистрации",
        "открыть кабинет",
    ]
)
async def cmd_cabinet_login(message: Message):
    """Автологин в cabinet.paskod.ru — аккаунт создаётся сам."""
    await _open_cabinet(
        message,
        redirect="/",
        title="✨ Секунду…",
        button_text="🌐 Открыть кабинет",
    )



# ---------------------------------------------------------------------------
# Инфо / юридические документы
# ---------------------------------------------------------------------------


@labeler.private_message(
    text=[
        BTN_INFO,
        "ℹ️ Инфо",
        "Инфо",
        "инфо",
        "информация",
        "документы",
        "документ",
    ]
)
async def cmd_info(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_info_menu(settings),
        keyboard=info_keyboard(_cab()),
    )


@labeler.private_message(text=list(_DOC_BUTTONS.keys()))
async def cmd_document(message: Message):
    await _ensure_user(message)
    raw = (message.text or "").strip()
    slug = _DOC_BUTTONS.get(raw) or _DOC_BUTTONS.get(raw.lower())
    if not slug:
        await message.answer(
            format_info_menu(get_settings()), keyboard=info_keyboard(_cab())
        )
        return
    await _send_doc(message, slug, page=1)


@labeler.private_message(text=[BTN_DOC_NEXT, "➡️ Далее", "далее"])
async def cmd_doc_next(message: Message):
    state = _reading_doc.get(message.from_id)
    if not state:
        await message.answer(
            format_info_menu(get_settings()), keyboard=info_keyboard(_cab())
        )
        return
    page = int(state["page"]) + 1
    await _send_doc(message, str(state["slug"]), page=page)


@labeler.private_message(text=[BTN_DOC_PREV, "⬅️ Назад"])
async def cmd_doc_prev(message: Message):
    """«⬅️ Назад» при чтении документа = предыдущая страница; иначе в меню."""
    state = _reading_doc.get(message.from_id)
    if state and int(state["page"]) > 1:
        await _send_doc(message, str(state["slug"]), page=int(state["page"]) - 1)
        return
    # Если страница 1 или документа нет — ведём себя как обычный «Назад»
    _reading_doc.pop(message.from_id, None)
    settings = get_settings()
    user = await _ensure_user(message)
    await message.answer(
        format_welcome(settings, user.first_name),
        keyboard=main_menu_keyboard(_cab()),
    )


@labeler.private_message(text=[BTN_DOC_LIST, "📚 К документам", "к документам"])
async def cmd_doc_list(message: Message):
    _reading_doc.pop(message.from_id, None)
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_info_menu(settings),
        keyboard=info_keyboard(_cab()),
    )



# ---------------------------------------------------------------------------
# Оплата Platega: СБП (QR) / карта / крипта — как в мини-приложении
# ---------------------------------------------------------------------------


def _clear_pay_state(vk_id: int) -> None:
    _pending_pay_method.pop(vk_id, None)


async def _start_pay_amount(message: Message, method_code: int) -> None:
    _waiting_promo.discard(message.from_id)
    _pending_pay_method[message.from_id] = method_code
    label = METHOD_LABELS.get(method_code, f"Platega {method_code}")
    await message.answer(
        format_pay_amount_prompt(label),
        keyboard=pay_amounts_keyboard(),
    )


async def _create_and_send_payment(
    message: Message,
    *,
    method_code: int,
    amount_kopeks: int,
) -> None:
    settings = get_settings()
    user = await _ensure_user(message)

    if amount_kopeks < 5000:
        await message.answer(
            format_min_amount(format_rubles(amount_kopeks)),
            keyboard=pay_amounts_keyboard(),
        )
        return

    try:
        await message.answer(
            f"✨ Секунду — готовлю счёт на {format_rubles(amount_kopeks)}…"
        )
        bedolaga_id = await ensure_bedolaga_id_for_payments(
            user.user_id, user.first_name
        )
        client = CabinetPaymentClient(settings)
        result = await client.create_platega_topup(
            bedolaga_id,
            amount_kopeks=amount_kopeks,
            payment_option=method_code,
        )
        _clear_pay_state(message.from_id)
        await message.answer(
            format_payment_created(
                method_label=result.method_label,
                amount_rubles=result.amount_rubles,
                payment_url=result.payment_url,
            ),
            keyboard=payment_link_keyboard(result.payment_url),
        )
    except CabinetPaymentError as exc:
        logger.exception("platega topup failed: %s", exc)
        await message.answer(
            format_error(
                "Счёт создать не удалось — платёжный сервис вернул ошибку. "
                "Попробуйте выбрать другой способ оплаты или повторите "
                "попытку чуть позже. Пополнить баланс также можно "
                "напрямую в кабинете."
            ),
            keyboard=pay_methods_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("platega topup unexpected: %s", exc)
        await message.answer(
            format_error(
                "Что-то сломалось на нашей стороне, и счёт не сформировался. "
                "Деньги при этом не списывались. Попробуйте ещё раз через "
                "пару минут или напишите в «💬 Помощь»."
            ),
            keyboard=main_menu_keyboard(_cab()),
        )


@labeler.private_message(
    text=[BTN_PAY, "💳 Оплатить", "Оплатить", "оплатить", "оплата", "сбп", "пополнить баланс"]
)
async def cmd_pay(message: Message):
    await _ensure_user(message)
    _waiting_promo.discard(message.from_id)
    await message.answer(format_pay_intro(), keyboard=pay_methods_keyboard())


@labeler.private_message(
    text=[BTN_PAY_SBP, "🏦 СБП (QR)", "🏦 СБП · QR", "СБП · QR", "сбп qr", "сбп (qr)", "qr"]
)
async def cmd_pay_sbp(message: Message):
    await _ensure_user(message)
    await _start_pay_amount(message, PLATEGA_METHOD_SBP_QR)


@labeler.private_message(
    text=[BTN_PAY_CARD, "💳 Банк. карта", "💳 Карта", "Карта", "карта", "банк. карта", "банковская карта"]
)
async def cmd_pay_card(message: Message):
    await _ensure_user(message)
    await _start_pay_amount(message, PLATEGA_METHOD_CARD)


@labeler.private_message(
    text=[BTN_PAY_CRYPTO, "🪙 Крипта", "Крипта", "крипта", "криптовалюта"]
)
async def cmd_pay_crypto(message: Message):
    await _ensure_user(message)
    await _start_pay_amount(message, PLATEGA_METHOD_CRYPTO)


@labeler.private_message(text=list(PAY_AMOUNT_BUTTONS.keys()) + ["50 ₽", "100 ₽", "150 ₽", "500 ₽"])
async def cmd_pay_quick_amount(message: Message):
    method_code = _pending_pay_method.get(message.from_id)
    if method_code is None:
        await message.answer(
            format_pay_method_first(),
            keyboard=pay_methods_keyboard(),
        )
        return
    raw = (message.text or "").strip()
    amount = PAY_AMOUNT_BUTTONS.get(raw)
    if amount is None:
        # legacy labels without emoji
        legacy = {
            "50 ₽": 5000,
            "100 ₽": 10000,
            "150 ₽": 15000,
            "500 ₽": 50000,
        }
        amount = legacy.get(raw)
    if not amount:
        return
    await _create_and_send_payment(
        message, method_code=method_code, amount_kopeks=amount
    )


@labeler.private_message(
    text=[BTN_BUY, "💳 Купить", "💎 Купить", "Купить", "купить", "тарифы", "тариф"]
)
async def cmd_buy(message: Message):
    await _open_cabinet(
        message,
        redirect="/subscription/purchase",
        title="✨ Секунду…",
        button_text="💎 К тарифам",
    )


@labeler.private_message(text=[BTN_BALANCE, "💰 Баланс", "Баланс", "баланс"])
async def cmd_balance(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_balance(settings),
        keyboard=pay_methods_keyboard(),
    )
    await _open_cabinet(
        message,
        redirect="/balance",
        title="✨ Или откройте кабинет",
        button_text="💰 Баланс в кабинете",
    )


@labeler.private_message(
    text=[
        BTN_REFERRAL,
        "👥 Партнёрка",
        "👥 Партнёрам",
        "Партнёрам",
        "партнёрка",
        "реферал",
        "рефералка",
        "партнёрам",
    ]
)
async def cmd_referral(message: Message):
    await _open_cabinet(
        message,
        redirect="/referral",
        title="✨ Секунду…",
        button_text="👥 Партнёрка",
    )


@labeler.private_message(
    text=[BTN_PROMO, "🎟 Промокод", "Промокод", "промокод", "промо"]
)
async def cmd_promo(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    _waiting_promo.add(message.from_id)
    await message.answer(
        format_promo_prompt(settings),
        keyboard=promo_keyboard(_cab()),
    )


@labeler.private_message(
    text=[BTN_APPS, "📱 Приложения", "Приложения", "приложения", "happ", "скачать"]
)
async def cmd_apps(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(format_apps(settings), keyboard=apps_keyboard(_cab()))


@labeler.private_message(
    text=[BTN_GUIDE, "📖 Инструкция", "📖 Гайд", "Гайд", "инструкция", "гайд"]
)
async def cmd_guide(message: Message):
    await message.answer(GUIDE_INTRO, keyboard=guide_os_keyboard())
    await message.answer(
        format_guide_outro(),
        keyboard=main_menu_keyboard(_cab()),
    )


@labeler.private_message(
    text=[
        BTN_SUPPORT,
        "💬 Поддержка",
        "💬 Помощь",
        "Помощь",
        "поддержка",
        "саппорт",
        "помощь",
    ]
)
async def cmd_support(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_support(settings),
        keyboard=support_keyboard(settings.support_url, _cab()),
    )


@labeler.private_message(
    text=[BTN_RENEW, "♻️ Продлить", "Продлить", "продлить", "продлить сейчас"]
)
async def cmd_renew(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)
    text = (message.text or "").lower()

    if settings.bedolaga_api_key and "сейчас" in text:
        try:
            await message.answer("✨ Секунду — продлеваю…")
            result = await issue_renewal(user, settings)
            await message.answer(
                format_key_message(
                    result.key, settings, is_trial=False, source=result.source
                ),
                keyboard=connect_keyboard(
                    has_active=True,
                    trial_available=False,
                    cabinet_url=_cab(),
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("renew failed: %s", exc)
            await message.answer(
                format_error(
                    "Автоматически продлить не получилось — панель не ответила. "
                    "Оформите продление через покупку тарифа в кабинете: "
                    "там доступ включится сразу после оплаты."
                ),
                keyboard=subscription_keyboard(_cab()),
            )

    await message.answer(
        format_renew_stub(settings),
        keyboard=support_keyboard(settings.support_url, _cab()),
    )


# ---------------------------------------------------------------------------
# Callback: инструкция по ОС + документы
# ---------------------------------------------------------------------------


@labeler.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=GroupTypes.MessageEvent)
async def on_message_event(event: GroupTypes.MessageEvent):
    settings = get_settings()
    payload_raw = event.object.payload
    if isinstance(payload_raw, str):
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {}
    elif isinstance(payload_raw, dict):
        payload = payload_raw
    else:
        payload = {}

    cmd = payload.get("cmd")
    snack = "Готово"

    if cmd == "guide":
        os_name = payload.get("os")
        if os_name in GUIDES:
            snack = OS_TITLES.get(os_name, os_name)
            await event.ctx_api.messages.send(
                peer_id=event.object.peer_id,
                message=GUIDES[os_name],
                random_id=0,
                keyboard=main_menu_keyboard(settings.cabinet_url),
            )
    elif cmd == "info":
        snack = "ℹ️ Инфо"
        _reading_doc.pop(event.object.user_id, None)
        await event.ctx_api.messages.send(
            peer_id=event.object.peer_id,
            message=format_info_menu(settings),
            random_id=0,
            keyboard=info_keyboard(settings.cabinet_url),
        )
    elif cmd == "doc":
        slug = str(payload.get("slug") or "")
        page = int(payload.get("page") or 1)
        doc = DOCS_BY_SLUG.get(slug)
        if doc:
            text, total = format_doc_for_bot(doc, page=page)
            page = max(1, min(page, total))
            _reading_doc[event.object.user_id] = {
                "slug": slug,
                "page": page,
                "total": total,
            }
            snack = f"{doc.emoji} {page}/{total}"
            await event.ctx_api.messages.send(
                peer_id=event.object.peer_id,
                message=text,
                random_id=0,
                keyboard=doc_nav_keyboard(page=page, total=total),
            )
        else:
            snack = "Не найдено"

    await event.ctx_api.messages.send_message_event_answer(
        event_id=event.object.event_id,
        user_id=event.object.user_id,
        peer_id=event.object.peer_id,
        event_data=ShowSnackbarEvent(text=snack[:90]).model_dump_json(),
    )


# ---------------------------------------------------------------------------
# Фоллбек: промокод или подсказка
# ---------------------------------------------------------------------------

_PROMO_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")
_AMOUNT_RE = re.compile(
    r"^(?:сумма\s*)?(\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?|rur|rub)?$",
    re.IGNORECASE,
)


@labeler.private_message()
async def fallback(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    text = (message.text or "").strip()

    method_code = _pending_pay_method.get(message.from_id)
    if method_code is not None:
        m = _AMOUNT_RE.match(text.replace(" ", ""))
        if m:
            raw = m.group(1).replace(",", ".")
            rubles = float(raw)
            amount_kopeks = int(round(rubles * 100))
            await _create_and_send_payment(
                message, method_code=method_code, amount_kopeks=amount_kopeks
            )
            return
        await message.answer(
            format_amount_invalid(),
            keyboard=pay_amounts_keyboard(),
        )
        return

    if message.from_id in _waiting_promo or _PROMO_RE.match(text or ""):
        _waiting_promo.discard(message.from_id)
        code = text.upper()
        await message.answer(
            format_promo_accepted(code),
            keyboard=promo_keyboard(_cab()),
        )
        await _open_cabinet(
            message,
            redirect="/subscription",
            title="✨ Секунду…",
            button_text="🌐 Открыть кабинет",
        )
        return

    await message.answer(
        format_fallback(),
        keyboard=main_menu_keyboard(_cab()),
    )
