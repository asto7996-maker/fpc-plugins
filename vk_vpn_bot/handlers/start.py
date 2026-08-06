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
    format_balance,
    format_buy,
    format_connect_screen,
    format_key_message,
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
    BTN_CONNECT,
    BTN_CONNECT_OLD,
    BTN_GET_KEY,
    BTN_GUIDE,
    BTN_MY_KEY,
    BTN_PROFILE,
    BTN_PROMO,
    BTN_REFERRAL,
    BTN_RENEW,
    BTN_SUBSCRIPTION,
    BTN_SUPPORT,
    BTN_TRIAL,
    apps_keyboard,
    connect_keyboard,
    guide_os_keyboard,
    main_menu_keyboard,
    promo_keyboard,
    subscription_keyboard,
    support_keyboard,
)
from services.bedolaga import get_bedolaga_client
from services.keys_service import issue_renewal, issue_trial_or_key

logger = logging.getLogger(__name__)

labeler = BotLabeler()
labeler.vbml_ignore_case = True

# Ожидание промокода от пользователя (vk_id → True)
_waiting_promo: set[int] = set()


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
        keyboard=main_menu_keyboard(_cab()),
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
    text=[BTN_RENEW, "продлить", "оплатить", "продлить сейчас"]
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


@labeler.private_message()
async def fallback(message: Message):
    settings = get_settings()
    await _ensure_user(message)
    text = (message.text or "").strip()

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
