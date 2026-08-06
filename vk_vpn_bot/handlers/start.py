"""
Основные обработчики команд и кнопок меню.

Логика триала:
1. Пользователь жмёт «Получить ключ» / «Подключить VPN»
2. Если is_trial_used == False → генерируем ключ, активируем подписку на TRIAL_DAYS
3. Иначе, если подписка активна → показываем текущий ключ
4. Иначе → предлагаем продление
"""

from __future__ import annotations

import json
import logging

from vkbottle import GroupEventType, GroupTypes, ShowSnackbarEvent
from vkbottle.bot import BotLabeler, Message

from config import get_settings
from database import activate_trial, get_or_create_user
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
from services.vpn_keys import generate_vpn_key

logger = logging.getLogger(__name__)

# Labeler подключается в main.py к bot.labeler
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


# ---------------------------------------------------------------------------
# /start и любое первое сообщение / «Назад в меню»
# ---------------------------------------------------------------------------


@labeler.private_message(text=["/start", "начать", "старт", "меню", BTN_BACK])
async def cmd_start(message: Message):
    """Главное меню бота."""
    settings = get_settings()
    user = await _ensure_user(message)
    text = format_welcome(settings, user.first_name)
    await message.answer(text, keyboard=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Подключить VPN
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_CONNECT, "подключить vpn", "подключить"])
async def cmd_connect(message: Message):
    """Экран подключения."""
    settings = get_settings()
    user = await _ensure_user(message)
    text = format_connect_screen(user, settings)
    kb = connect_keyboard(
        has_active=user.is_subscription_active(),
        trial_available=not user.is_trial_used,
    )
    await message.answer(text, keyboard=kb)


# ---------------------------------------------------------------------------
# Получить ключ / показать ключ
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_GET_KEY, BTN_MY_KEY, "получить ключ", "мой ключ"])
async def cmd_get_key(message: Message):
    """
    Выдача ключа:
    - триал не использован → активируем и выдаём;
    - подписка активна → показываем сохранённый ключ;
    - иначе → предлагаем продление.
    """
    settings = get_settings()
    user = await _ensure_user(message)

    # 1) Активная подписка — просто отдаём ключ
    if user.is_subscription_active() and user.vpn_key:
        await message.answer(
            format_key_message(user.vpn_key, settings, is_trial=False),
            keyboard=connect_keyboard(has_active=True, trial_available=False),
        )
        return

    # 2) Триал ещё не использован — активируем
    if not user.is_trial_used:
        key = generate_vpn_key(user.user_id, settings)
        user = await activate_trial(user.user_id, key, settings.trial_days)
        logger.info(
            "Активирован триал для user_id=%s на %s дн.",
            user.user_id,
            settings.trial_days,
        )
        await message.answer(
            format_key_message(key, settings, is_trial=True),
            keyboard=connect_keyboard(has_active=True, trial_available=False),
        )
        return

    # 3) Триал был, подписки нет
    await message.answer(
        "⏳ Тестовый период уже использован, активной подписки нет.\n\n"
        "Нажмите «Продлить подписку» или напишите в поддержку.",
        keyboard=connect_keyboard(has_active=False, trial_available=False),
    )


# ---------------------------------------------------------------------------
# Профиль
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_PROFILE, "профиль", "мой профиль"])
async def cmd_profile(message: Message):
    """Карточка профиля."""
    user = await _ensure_user(message)
    await message.answer(format_profile(user), keyboard=profile_keyboard())


# ---------------------------------------------------------------------------
# Инструкция
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_GUIDE, "инструкция", "гайд", "помощь"])
async def cmd_guide(message: Message):
    """Выбор ОС для инструкции (inline-кнопки)."""
    await message.answer(GUIDE_INTRO, keyboard=guide_os_keyboard())
    # Дублируем обычную клавиатуру «назад», чтобы не потерять меню
    await message.answer(
        "Или вернитесь в главное меню кнопкой ниже.",
        keyboard=main_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Поддержка
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_SUPPORT, "поддержка", "саппорт"])
async def cmd_support(message: Message):
    """Раздел поддержки."""
    settings = get_settings()
    await message.answer(
        format_support(settings),
        keyboard=support_keyboard(settings.support_url),
    )


# ---------------------------------------------------------------------------
# Продление
# ---------------------------------------------------------------------------


@labeler.private_message(text=[BTN_RENEW, "продлить", "оплатить", "купить"])
async def cmd_renew(message: Message):
    """Экран продления (заглушка оплаты + поддержка)."""
    settings = get_settings()
    await _ensure_user(message)
    await message.answer(
        format_renew_stub(),
        keyboard=support_keyboard(settings.support_url),
    )


# ---------------------------------------------------------------------------
# Callback: выбор ОС в инструкции (MESSAGE_EVENT)
# ---------------------------------------------------------------------------


@labeler.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=GroupTypes.MessageEvent)
async def on_message_event(event: GroupTypes.MessageEvent):
    """
    Обработка нажатий Callback (inline) кнопок.
    Отвечаем snackbar'ом и присылаем текст инструкции в ЛС.
    """
    payload_raw = event.object.payload
    # payload может прийти dict или JSON-строкой
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
        guide_text = GUIDES[os_name]
        # Отправляем инструкцию пользователю
        await event.ctx_api.messages.send(
            peer_id=event.object.peer_id,
            message=guide_text,
            random_id=0,
            keyboard=main_menu_keyboard(),
        )
    else:
        snack_text = "Неизвестная кнопка"

    # Обязательный ответ на MESSAGE_EVENT (иначе кнопка «висит»)
    await event.ctx_api.messages.send_message_event_answer(
        event_id=event.object.event_id,
        user_id=event.object.user_id,
        peer_id=event.object.peer_id,
        event_data=ShowSnackbarEvent(text=snack_text).model_dump_json(),
    )


# ---------------------------------------------------------------------------
# Фоллбек: любое другое личное сообщение → подсказка меню
# ---------------------------------------------------------------------------


@labeler.private_message()
async def fallback(message: Message):
    """
    Фоллбек: ловит личные сообщения, не попавшие в хендлеры выше.
    В vkbottle хендлеры блокирующие по умолчанию — срабатывает первый матч,
    поэтому этот обработчик должен регистрироваться последним.
    """
    await _ensure_user(message)
    await message.answer(
        "Не совсем понял запрос 🤔\n"
        "Нажмите кнопку меню или отправьте /start",
        keyboard=main_menu_keyboard(),
    )
