"""
Обработчики VK-бота — функциональный паритет с Telegram Bedolaga/Paskod.

Разделы:
  Подключиться / триал / ключ
  Моя подписка / профиль
  Купить / Баланс / Партнёрка → кабинет
  Промокод (ввод в чат)
  Приложения / Инструкция / Поддержка
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
    format_apps,
    format_auto_login_message,
    format_balance,
    format_buy,
    format_connect_screen,
    format_key_message,
    format_pay_amount_prompt,
    format_pay_intro,
    format_payment_created,
    format_promo_prompt,
    format_referral,
    format_renew_stub,
    format_subscription_card,
    format_support,
    format_welcome,
)
from handlers.texts import GUIDE_INTRO, GUIDES, OS_TITLES
from keyboards import (
    BTN_APPS,
    BTN_BACK,
    BTN_BALANCE,
    BTN_BUY,
    BTN_CABINET_LOGIN,
    BTN_CONNECT,
    BTN_CONNECT_OLD,
    BTN_GET_KEY,
    BTN_GUIDE,
    BTN_MY_KEY,
    BTN_PAY,
    BTN_PAY_CARD,
    BTN_PAY_CRYPTO,
    BTN_PAY_SBP,
    BTN_PROFILE,
    BTN_PROMO,
    BTN_REFERRAL,
    BTN_RENEW,
    BTN_SUBSCRIPTION,
    BTN_SUPPORT,
    BTN_TRIAL,
    PAY_AMOUNT_BUTTONS,
    apps_keyboard,
    auto_login_keyboard,
    connect_keyboard,
    guide_os_keyboard,
    main_menu_keyboard,
    pay_amounts_keyboard,
    pay_methods_keyboard,
    payment_link_keyboard,
    promo_keyboard,
    subscription_keyboard,
    support_keyboard,
)
from services.bedolaga import get_bedolaga_client
from services.cabinet_auth import ensure_auto_login_url
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


# ---------------------------------------------------------------------------
# Старт / меню
# ---------------------------------------------------------------------------


@labeler.private_message(text=["/start", "начать", "старт", "меню", BTN_BACK])
async def cmd_start(message: Message):
    settings = get_settings()
    _waiting_promo.discard(message.from_id)
    _pending_pay_method.pop(message.from_id, None)
    user = await _ensure_user(message)
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
    want_trial = any(x in text for x in ("триал", "trial", "активировать"))
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
        await message.answer("⏳ Активирую триал, как в Telegram-боте…")
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
        "⏳ Триал уже использован.\n"
        "Купите тариф в кабинете или нажмите «Продлить» — "
        "как в Telegram Paskod.",
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
    text=[BTN_SUBSCRIPTION, BTN_PROFILE, "профиль", "подписка", "моя подписка"]
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
        BTN_CABINET_LOGIN,
        "войти",
        "кабинет",
        "войти в кабинет",
        "автологин",
        "без регистрации",
    ]
)
async def cmd_cabinet_login(message: Message):
    """
    Автологин в cabinet.paskod.ru без email/пароля —
    аналог входа Telegram-пользователей через Mini App.
    """
    settings = get_settings()
    user = await _ensure_user(message)

    if not settings.cabinet_jwt_secret:
        await message.answer(
            "Автологин пока не настроен (нет CABINET_JWT_SECRET).\n"
            f"Откройте кабинет вручную: {settings.cabinet_url}/login",
            keyboard=main_menu_keyboard(_cab()),
        )
        return

    try:
        await message.answer("⏳ Готовлю вход в кабинет…")
        url, _bedolaga_id = await ensure_auto_login_url(
            user.user_id, user.first_name, settings
        )
        await message.answer(
            format_auto_login_message(url, settings),
            keyboard=auto_login_keyboard(url),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto-login failed: %s", exc)
        await message.answer(
            f"Не удалось создать вход: {exc}\n"
            f"Попробуйте открыть {settings.cabinet_url}/login",
            keyboard=main_menu_keyboard(_cab()),
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
    label = METHOD_LABELS.get(method_code, f"Platega {method_code}")

    if amount_kopeks < 5000:
        await message.answer(
            f"Минимальная сумма — 50 ₽ (сейчас {format_rubles(amount_kopeks)}).",
            keyboard=pay_amounts_keyboard(),
        )
        return

    try:
        await message.answer(
            f"⏳ Создаю счёт {label} на {format_rubles(amount_kopeks)}…"
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
            f"Не удалось создать оплату: {exc}\n"
            f"Попробуйте через кабинет: {settings.cabinet_url}/balance/top-up",
            keyboard=pay_methods_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("platega topup unexpected: %s", exc)
        await message.answer(
            f"Ошибка оплаты: {exc}",
            keyboard=main_menu_keyboard(_cab()),
        )


@labeler.private_message(
    text=[BTN_PAY, "оплатить", "оплата", "сбп", "пополнить баланс"]
)
async def cmd_pay(message: Message):
    await _ensure_user(message)
    _waiting_promo.discard(message.from_id)
    await message.answer(format_pay_intro(), keyboard=pay_methods_keyboard())


@labeler.private_message(text=[BTN_PAY_SBP, "сбп qr", "сбп (qr)", "qr"])
async def cmd_pay_sbp(message: Message):
    await _ensure_user(message)
    await _start_pay_amount(message, PLATEGA_METHOD_SBP_QR)


@labeler.private_message(
    text=[BTN_PAY_CARD, "карта", "банк. карта", "банковская карта"]
)
async def cmd_pay_card(message: Message):
    await _ensure_user(message)
    await _start_pay_amount(message, PLATEGA_METHOD_CARD)


@labeler.private_message(text=[BTN_PAY_CRYPTO, "крипта", "криптовалюта"])
async def cmd_pay_crypto(message: Message):
    await _ensure_user(message)
    await _start_pay_amount(message, PLATEGA_METHOD_CRYPTO)


@labeler.private_message(text=list(PAY_AMOUNT_BUTTONS.keys()))
async def cmd_pay_quick_amount(message: Message):
    method_code = _pending_pay_method.get(message.from_id)
    if method_code is None:
        await message.answer(
            "Сначала выберите способ оплаты.",
            keyboard=pay_methods_keyboard(),
        )
        return
    amount = PAY_AMOUNT_BUTTONS.get((message.text or "").strip())
    if not amount:
        return
    await _create_and_send_payment(
        message, method_code=method_code, amount_kopeks=amount
    )


# ---------------------------------------------------------------------------
# Купить / Баланс / Партнёрка (кабинет = Telegram Mini App)
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_BUY, "купить", "тарифы", "тариф"])
async def cmd_buy(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_buy(settings),
        keyboard=subscription_keyboard(_cab()),
    )


@labeler.private_message(text=[BTN_BALANCE, "баланс", "пополнить"])
async def cmd_balance(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_balance(settings),
        keyboard=pay_methods_keyboard(),
    )


@labeler.private_message(text=[BTN_REFERRAL, "партнёрка", "реферал", "рефералка"])
async def cmd_referral(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_referral(settings),
        keyboard=main_menu_keyboard(_cab()),
    )


# ---------------------------------------------------------------------------
# Промокод
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_PROMO, "промокод", "промо"])
async def cmd_promo(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    _waiting_promo.add(message.from_id)
    await message.answer(
        format_promo_prompt(settings),
        keyboard=promo_keyboard(_cab()),
    )


# ---------------------------------------------------------------------------
# Приложения / инструкция / поддержка / продление
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_APPS, "приложения", "happ", "скачать"])
async def cmd_apps(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(format_apps(settings), keyboard=apps_keyboard(_cab()))


@labeler.private_message(text=[BTN_GUIDE, "инструкция", "гайд", "помощь"])
async def cmd_guide(message: Message):
    await message.answer(GUIDE_INTRO, keyboard=guide_os_keyboard())
    await message.answer(
        "Или вернитесь в меню.",
        keyboard=main_menu_keyboard(_cab()),
    )


@labeler.private_message(text=[BTN_SUPPORT, "поддержка", "саппорт"])
async def cmd_support(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_support(settings),
        keyboard=support_keyboard(settings.support_url, _cab()),
    )


@labeler.private_message(
    text=[BTN_RENEW, "продлить", "продлить сейчас"]
)
async def cmd_renew(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)
    text = (message.text or "").lower()

    if settings.bedolaga_api_key and "сейчас" in text:
        try:
            await message.answer("⏳ Продлеваю через панель Bedolaga…")
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
                f"Не удалось продлить автоматически: {exc}\n"
                f"Откройте покупку: {settings.cabinet_url}/subscription/purchase"
            )

    await message.answer(
        format_renew_stub(settings),
        keyboard=support_keyboard(settings.support_url, _cab()),
    )


# ---------------------------------------------------------------------------
# Callback: инструкция по ОС
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
    os_name = payload.get("os")
    snack = "Готово"

    if cmd == "guide" and os_name in GUIDES:
        snack = f"Инструкция: {OS_TITLES.get(os_name, os_name)}"
        await event.ctx_api.messages.send(
            peer_id=event.object.peer_id,
            message=GUIDES[os_name],
            random_id=0,
            keyboard=main_menu_keyboard(settings.cabinet_url),
        )
    else:
        snack = "Неизвестная кнопка"

    await event.ctx_api.messages.send_message_event_answer(
        event_id=event.object.event_id,
        user_id=event.object.user_id,
        peer_id=event.object.peer_id,
        event_data=ShowSnackbarEvent(text=snack).model_dump_json(),
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
            "Введите сумму числом (например 100) или выберите кнопку.",
            keyboard=pay_amounts_keyboard(),
        )
        return

    if message.from_id in _waiting_promo or _PROMO_RE.match(text or ""):
        _waiting_promo.discard(message.from_id)
        code = text.upper()
        await message.answer(
            "🎟 Промокод принят: "
            f"{code}\n\n"
            "Активация промокодов выполняется в кабинете Paskod "
            "(как в Telegram Mini App), потому что привязана к аккаунту панели.\n\n"
            f"1) Откройте {settings.cabinet_url}\n"
            f"2) Войдите / зарегистрируйтесь\n"
            f"3) Введите промокод {code} в разделе подписки\n\n"
            "Если нужен триал прямо здесь — нажмите «Подключиться».",
            keyboard=promo_keyboard(_cab()),
        )
        return

    await message.answer(
        "Не совсем понял 🤔\n"
        "Откройте меню или отправьте /start — "
        "разделы такие же, как в Telegram-боте Paskod.",
        keyboard=main_menu_keyboard(_cab()),
    )
