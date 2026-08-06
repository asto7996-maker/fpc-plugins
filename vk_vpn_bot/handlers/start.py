"""
Основные обработчики команд и кнопок меню (стиль Бедолага / Paskod).
"""

from __future__ import annotations

import json
import logging

from vkbottle import GroupEventType, GroupTypes, ShowSnackbarEvent
from vkbottle.bot import BotLabeler, Message

from config import get_settings
from database import get_or_create_user
from handlers.helpers import (
    format_connect_screen,
    format_key_message,
    format_profile,
    format_renew_stub,
    format_support,
    format_welcome,
)
from handlers.texts import GUIDE_INTRO, GUIDES, OS_TITLES
from keyboards import (
    BTN_BACK,
    BTN_CONNECT,
    BTN_GET_KEY,
    BTN_GUIDE,
    BTN_MY_KEY,
    BTN_PROFILE,
    BTN_RENEW,
    BTN_SUPPORT,
    connect_keyboard,
    guide_os_keyboard,
    main_menu_keyboard,
    profile_keyboard,
    support_keyboard,
)
from services.keys_service import issue_renewal, issue_trial_or_key

logger = logging.getLogger(__name__)

labeler = BotLabeler()
labeler.vbml_ignore_case = True


async def _ensure_user(message: Message):
    """Регистрирует пользователя в БД при первом обращении."""
    first_name = None
    try:
        users = await message.ctx_api.users.get(user_ids=[message.from_id])
        if users:
            first_name = users[0].first_name
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось получить имя пользователя %s: %s", message.from_id, exc)

    return await get_or_create_user(message.from_id, first_name=first_name)


def _cabinet(settings) -> str:
    return settings.cabinet_url


@labeler.private_message(text=["/start", "начать", "старт", "меню", BTN_BACK])
async def cmd_start(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)
    await message.answer(
        format_welcome(settings, user.first_name),
        keyboard=main_menu_keyboard(_cabinet(settings)),
    )


@labeler.private_message(text=[BTN_CONNECT, "подключить vpn", "подключить"])
async def cmd_connect(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)
    await message.answer(
        format_connect_screen(user, settings),
        keyboard=connect_keyboard(
            has_active=user.is_subscription_active(),
            trial_available=not user.is_trial_used,
            cabinet_url=_cabinet(settings),
        ),
    )


@labeler.private_message(text=[BTN_GET_KEY, BTN_MY_KEY, "получить ключ", "мой ключ"])
async def cmd_get_key(message: Message):
    """
    Выдача ключа:
    - триал не использован → Bedolaga/локально активируем;
    - подписка активна → показываем сохранённый ключ;
    - иначе → предлагаем продление.
    """
    settings = get_settings()
    user = await _ensure_user(message)

    if user.is_subscription_active() and user.vpn_key:
        await message.answer(
            format_key_message(user.vpn_key, settings, is_trial=False, source="bedolaga" if user.bedolaga_user_id else "local"),
            keyboard=connect_keyboard(
                has_active=True,
                trial_available=False,
                cabinet_url=_cabinet(settings),
            ),
        )
        return

    if not user.is_trial_used:
        await message.answer("⏳ Активирую тестовый период…")
        result = await issue_trial_or_key(user, settings)
        await message.answer(
            format_key_message(
                result.key,
                settings,
                is_trial=True,
                source=result.source,
            ),
            keyboard=connect_keyboard(
                has_active=True,
                trial_available=False,
                cabinet_url=_cabinet(settings),
            ),
        )
        return

    await message.answer(
        "⏳ Тестовый период уже использован, активной подписки нет.\n\n"
        "Нажмите «Продлить подписку» или откройте личный кабинет.",
        keyboard=connect_keyboard(
            has_active=False,
            trial_available=False,
            cabinet_url=_cabinet(settings),
        ),
    )


@labeler.private_message(text=[BTN_PROFILE, "профиль", "мой профиль"])
async def cmd_profile(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)
    await message.answer(
        format_profile(user, settings),
        keyboard=profile_keyboard(_cabinet(settings)),
    )


@labeler.private_message(text=[BTN_GUIDE, "инструкция", "гайд", "помощь"])
async def cmd_guide(message: Message):
    settings = get_settings()
    await message.answer(GUIDE_INTRO, keyboard=guide_os_keyboard())
    await message.answer(
        "Или вернитесь в главное меню кнопкой ниже.",
        keyboard=main_menu_keyboard(_cabinet(settings)),
    )


@labeler.private_message(text=[BTN_SUPPORT, "поддержка", "саппорт"])
async def cmd_support(message: Message):
    settings = get_settings()
    await message.answer(
        format_support(settings),
        keyboard=support_keyboard(settings.support_url, _cabinet(settings)),
    )


@labeler.private_message(
    text=[BTN_RENEW, "продлить", "оплатить", "купить", "продлить сейчас"]
)
async def cmd_renew(message: Message):
    settings = get_settings()
    user = await _ensure_user(message)

    # Если есть API-ключ Bedolaga и пользователь явно просит «продлить сейчас» —
    # пытаемся выдать через панель (без онлайн-оплаты).
    text = (message.text or "").lower()
    if settings.bedolaga_api_key and "сейчас" in text:
        try:
            await message.answer("⏳ Продлеваю подписку через панель…")
            result = await issue_renewal(user, settings)
            await message.answer(
                format_key_message(
                    result.key, settings, is_trial=False, source=result.source
                ),
                keyboard=connect_keyboard(
                    has_active=True,
                    trial_available=False,
                    cabinet_url=_cabinet(settings),
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Renew failed: %s", exc)
            await message.answer(
                f"Не удалось продлить автоматически: {exc}\n"
                f"Откройте кабинет: {settings.cabinet_url}"
            )

    await message.answer(
        format_renew_stub(settings),
        keyboard=support_keyboard(settings.support_url, _cabinet(settings)),
    )


@labeler.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=GroupTypes.MessageEvent)
async def on_message_event(event: GroupTypes.MessageEvent):
    """Callback (inline) кнопки инструкции по ОС."""
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
    snack_text = "Готово"

    if cmd == "guide" and os_name in GUIDES:
        snack_text = f"Инструкция: {OS_TITLES.get(os_name, os_name)}"
        await event.ctx_api.messages.send(
            peer_id=event.object.peer_id,
            message=GUIDES[os_name],
            random_id=0,
            keyboard=main_menu_keyboard(_cabinet(settings)),
        )
    else:
        snack_text = "Неизвестная кнопка"

    await event.ctx_api.messages.send_message_event_answer(
        event_id=event.object.event_id,
        user_id=event.object.user_id,
        peer_id=event.object.peer_id,
        event_data=ShowSnackbarEvent(text=snack_text).model_dump_json(),
    )


@labeler.private_message()
async def fallback(message: Message):
    """Фоллбек для неизвестных сообщений (регистрируется последним)."""
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        "Не совсем понял запрос 🤔\n"
        "Нажмите кнопку меню или отправьте /start",
        keyboard=main_menu_keyboard(_cabinet(settings)),
    )
