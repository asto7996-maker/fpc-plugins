"""
lobby.py — лобби компенсации.

Вход для админа: меню команд бота (/lobby, /lobby_mail, /lobby_sync).
Основной диалог с покупателями ведёт юзербот в личке поддержки:
тарифы берём из @sweetshopxxx_bot, человек выбирает кнопкой,
чек проверяется на Photoshop, возмещаем максимум один тариф.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    KeyboardButton,
    MenuButtonCommands,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import admin_bot
import config
from database import Database

logger = logging.getLogger(__name__)
router = Router()

MAIL_GAP = 1.2
MAIL_LIMIT = 400
MIN_RECEIPT_BYTES = 8_000
MAX_TARIFF_LEN = 80
MIN_TARIFF_LEN = 2
MAX_DURATION_DAYS = 365
GRANT_MULTIPLIER = 1

_db: Optional[Database] = None
_bot: Optional[Bot] = None
_bridge = None
_bot_username = ""
_mail_task: Optional[asyncio.Task] = None

_CANCEL_WORDS = frozenset(
    {"отмена", "cancel", "стоп", "/cancel", "/stop", "назад"}
)


class LobbyS(StatesGroup):
    tariff = State()
    duration = State()
    price = State()
    receipt = State()


def set_dependencies(
    db: Database,
    bot: Bot | None = None,
    bridge=None,
    bot_username: str = "",
    **_kwargs,
) -> None:
    global _db, _bot, _bridge, _bot_username
    _db = db
    _bot = bot
    _bridge = bridge
    _bot_username = (bot_username or "").lstrip("@")


def _require_db() -> Database:
    if _db is None:
        raise RuntimeError("lobby db is not bound")
    return _db


# ----- разбор ответов пользователя -----

_DURATION_UNITS: dict[str, int] = {
    "д": 1,
    "дн": 1,
    "день": 1,
    "дня": 1,
    "дней": 1,
    "сут": 1,
    "сутки": 1,
    "суток": 1,
    "day": 1,
    "days": 1,
    "d": 1,
    "нед": 7,
    "недел": 7,
    "неделя": 7,
    "недели": 7,
    "недель": 7,
    "week": 7,
    "weeks": 7,
    "w": 7,
    "мес": 30,
    "месяц": 30,
    "месяца": 30,
    "месяцев": 30,
    "month": 30,
    "months": 30,
    "mo": 30,
    "год": 365,
    "года": 365,
    "лет": 365,
    "year": 365,
    "years": 365,
    "y": 365,
}

_DURATION_TOKEN = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zА-Яа-яЁё]*)",
    re.UNICODE,
)


def parse_tariff_duration(raw: str) -> int:
    """Срок тарифа в днях. Голое число = дни (не минуты планировщика)."""
    text = (raw or "").strip().lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise ValueError("укажите срок тарифа, например: 30 дней")
    if re.search(r"(^|[^\d])-\d", text):
        raise ValueError("срок не может быть отрицательным")

    bare = re.sub(r"^(на|сроком|срок)\s+", "", text).strip()
    if re.fullmatch(r"(месяц|мес)", bare):
        return 30
    if re.fullmatch(r"(год|года)", bare):
        return 365
    if re.fullmatch(r"(неделя|неделю)", bare):
        return 7

    total = 0.0
    matched = 0
    for m in _DURATION_TOKEN.finditer(text):
        value = float(m.group("value").replace(",", "."))
        unit_raw = (m.group("unit") or "").replace("ё", "е")
        if not unit_raw:
            # «30» или «30  дней» — дни
            unit = 1
        else:
            unit = _DURATION_UNITS.get(unit_raw)
            if unit is None:
                # усечённые формы: меся..., недел...
                unit = next(
                    (
                        days
                        for key, days in _DURATION_UNITS.items()
                        if unit_raw.startswith(key) or key.startswith(unit_raw)
                    ),
                    None,
                )
            if unit is None:
                continue
        if value <= 0:
            continue
        total += value * unit
        matched += 1

    if matched == 0 or total <= 0:
        raise ValueError("не понял срок. Пример: 30 дней, 1 месяц, 3 мес")

    days = int(round(total))
    if days < 1 or days > MAX_DURATION_DAYS:
        raise ValueError(f"срок должен быть от 1 до {MAX_DURATION_DAYS} дней")
    return days


def parse_price(raw: str) -> float:
    """Цена в рублях: 1500, 1 500 ₽, 1500 руб."""
    text = (raw or "").strip().lower().replace("ё", "е")
    if not text:
        raise ValueError("укажите цену, например: 1500")
    if re.search(r"(^|[^\d])-\d", text):
        raise ValueError("цена не может быть отрицательной")
    text = text.replace("₽", " ").replace("рублей", " ").replace("рубля", " ")
    text = text.replace("руб.", " ").replace("руб", " ")
    text = text.replace("rur", " ").replace("rub", " ")
    text = text.replace("\u00a0", " ").replace(" ", "")
    text = text.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d{1,2})?)", text)
    if not m:
        raise ValueError("не понял цену. Напишите число, например 1500")
    price = float(m.group(1))
    if price <= 0:
        raise ValueError("цена должна быть больше нуля")
    if price > 10_000_000:
        raise ValueError("слишком большая сумма — проверьте число")
    return round(price, 2)


def normalize_tariff(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if name.startswith("/"):
        raise ValueError("это команда, а не название тарифа")
    if name.lower() in _CANCEL_WORDS:
        raise ValueError("отменено")
    if len(name) < MIN_TARIFF_LEN or len(name) > MAX_TARIFF_LEN:
        raise ValueError(
            f"название тарифа — от {MIN_TARIFF_LEN} до {MAX_TARIFF_LEN} символов"
        )
    return name


def granted_days_for(duration_days: int) -> int:
    """Возмещаем тот же срок одного тарифа, без удвоения."""
    return int(duration_days)


# ----- проверка чека -----

_OK_MIME = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
        "application/pdf",
    }
)


@dataclass
class ReceiptInput:
    has_photo: bool = False
    has_document: bool = False
    mime: str = ""
    file_size: int = 0
    file_name: str = ""
    caption: str = ""
    has_sticker: bool = False
    has_video: bool = False
    has_voice: bool = False
    has_animation: bool = False
    has_video_note: bool = False
    file_id: str = ""


def _extract_amounts(text: str) -> list[float]:
    if not text:
        return []
    cleaned = text.replace("\u00a0", " ").replace("₽", " ")
    found: list[float] = []
    for m in re.finditer(r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d{1,2})?", cleaned):
        raw = m.group(0).replace(" ", "").replace("\u00a0", "").replace(",", ".")
        try:
            found.append(float(raw))
        except ValueError:
            continue
    return found


def amount_matches_price(amounts: list[float], price: float) -> bool:
    if not amounts:
        return True
    for amt in amounts:
        if price <= 0:
            continue
        if abs(amt - price) <= max(1.0, price * 0.05):
            return True
    return False


def validate_receipt(item: ReceiptInput, declared_price: float) -> tuple[bool, str]:
    """Эвристика без OCR: тип, размер, сумма в подписи если она есть."""
    if item.has_sticker or item.has_video or item.has_voice:
        return False, "нужен фото- или PDF-чек, не стикер/видео/голос"
    if item.has_animation or item.has_video_note:
        return False, "нужен скриншот или PDF чека, не гифка и не кружок"
    if not item.has_photo and not item.has_document:
        return False, "пришлите фото чека или файл PDF / JPG / PNG"

    mime = (item.mime or "").strip().lower()
    name = (item.file_name or "").strip().lower()
    if item.has_document and not item.has_photo:
        ok_mime = mime in _OK_MIME or mime.startswith("image/")
        ok_name = name.endswith((".jpg", ".jpeg", ".png", ".webp", ".pdf", ".heic"))
        if mime and not ok_mime and not ok_name:
            return False, "файл должен быть изображением или PDF"
        if not mime and not ok_name:
            return False, "файл должен быть изображением или PDF"

    if item.file_size and item.file_size < MIN_RECEIPT_BYTES:
        return False, "файл слишком маленький — пришлите полный скриншот чека"

    amounts = _extract_amounts(item.caption)
    if amounts and not amount_matches_price(amounts, declared_price):
        return (
            False,
            f"сумма на чеке не совпадает с указанной ценой {declared_price:g} ₽",
        )
    return True, ""


def receipt_from_message(message: Message) -> ReceiptInput:
    photo = message.photo[-1] if message.photo else None
    doc = message.document
    file_id = ""
    mime = ""
    size = 0
    name = ""
    if photo is not None:
        file_id = photo.file_id
        size = int(photo.file_size or 0)
        mime = "image/jpeg"
    if doc is not None:
        file_id = doc.file_id
        mime = doc.mime_type or mime
        size = int(doc.file_size or size or 0)
        name = doc.file_name or ""
    return ReceiptInput(
        has_photo=photo is not None,
        has_document=doc is not None,
        mime=mime,
        file_size=size,
        file_name=name,
        caption=message.caption or message.text or "",
        has_sticker=message.sticker is not None,
        has_video=message.video is not None,
        has_voice=message.voice is not None,
        has_animation=message.animation is not None,
        has_video_note=message.video_note is not None,
        file_id=file_id,
    )


# ----- рассылка по диалогам юзербота -----

def _chat_is_private_user(chat: Any, self_id: int) -> bool:
    if chat is None:
        return False
    uid = getattr(chat, "id", None)
    if uid is None:
        return False
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return False
    if uid == int(self_id) or uid <= 0:
        return False
    if getattr(chat, "is_bot", False):
        return False
    t = getattr(chat, "type", None)
    name = str(getattr(t, "name", t) or "").lower()
    value = str(getattr(t, "value", "") or "").lower()
    blob = f"{name} {value} {t}".lower()
    return "private" in blob


def collect_private_user_ids(
    dialogs: list[Any],
    *,
    self_id: int,
    skip_ids: set[int],
) -> list[int]:
    """Кого звать в лобби: личные чаты с людьми, ещё не писали рассылкой."""
    out: list[int] = []
    seen: set[int] = set()
    for dialog in dialogs:
        chat = getattr(dialog, "chat", dialog)
        if not _chat_is_private_user(chat, self_id):
            continue
        uid = int(chat.id)
        if uid in skip_ids or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def mailing_invite_text(bot_username: str) -> str:
    return (
        "Здравствуйте. Это поддержка.\n\n"
        "Приватные каналы были заблокированы. Можем возместить "
        "максимум один купленный тариф — после проверки чека "
        "(в том числе на Photoshop).\n\n"
        "Ответьте на это сообщение, и пришлю кнопки с актуальными "
        "тарифами из @sweetshopxxx_bot. Либо откройте меню бота "
        f"@{(bot_username or '').lstrip('@')} → «Лобби компенсации»."
    )


def receipt_guide_text() -> str:
    return (
        "Пришлите <b>чек покупки</b> — фото или PDF.\n\n"
        "<b>Как получить чек</b>\n"
        "1. FunPay: откройте заказ → «Чек» или сделайте скриншот "
        "страницы заказа с суммой, датой и номером.\n"
        "2. Банк / СБП: приложение банка → операция → «Квитанция» "
        "или «Чек» → скачать / скриншот.\n"
        "3. На чеке должны быть видны сумма, дата и получатель.\n\n"
        "Не подходит: стикеры, видео, голосовые, обрезанный кусок без суммы."
    )


def welcome_text(*, already_granted: bool = False, is_admin: bool = False) -> str:
    if already_granted:
        return (
            "<b>Лобби компенсации</b>\n\n"
            "Вам уже возмещён <b>один тариф</b> — больше одного выдать нельзя. "
            "Если доступ ещё не открылся, напишите в поддержку на аккаунт, "
            "с которого приходило это сообщение."
        )
    extra = ""
    if is_admin:
        extra = (
            "\n\n<i>Админ: /lobby_mail — написать тем, кто писал аккаунту. "
            "/lobby_sync — обновить тарифы из @sweetshopxxx_bot.</i>"
        )
    return (
        "<b>Лобби компенсации</b>\n\n"
        "Приватные каналы с товаром были заблокированы. "
        "Возмещаем <b>максимум один тариф</b> из актуального списка "
        "@sweetshopxxx_bot — после проверки чека (в т.ч. Photoshop).\n\n"
        "Выберите тариф кнопкой ниже."
        + extra
    )


# ----- меню команд бота (не инлайн-клавиатура сообщений) -----

LOBBY_COMMAND = BotCommand(command="lobby", description="Лобби компенсации")
LOBBY_MAIL_COMMAND = BotCommand(
    command="lobby_mail", description="Написать тем, кто писал аккаунту"
)
LOBBY_SYNC_COMMAND = BotCommand(
    command="lobby_sync", description="Обновить тарифы из магазина"
)

_ADMIN_EXTRA = [
    BotCommand(command="start", description="Панель репостера"),
    BotCommand(command="status", description="Статус окон"),
    BotCommand(command="stop", description="Стоп текущего цикла"),
    BotCommand(command="ping", description="Проверка панели"),
]


async def setup_bot_menu(bot: Bot) -> None:
    """Кнопка меню слева от поля ввода = список команд, не reply-клавиатура."""
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logger.exception("set_chat_menu_button")
    try:
        await bot.set_my_commands([LOBBY_COMMAND], scope=BotCommandScopeDefault())
    except Exception:
        logger.exception("set_my_commands default")
    for aid in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [LOBBY_COMMAND, LOBBY_MAIL_COMMAND, LOBBY_SYNC_COMMAND, *_ADMIN_EXTRA],
                scope=BotCommandScopeChat(chat_id=aid),
            )
        except Exception:
            logger.debug("set_my_commands admin %s failed", aid, exc_info=True)


# ----- хендлеры -----

def _is_cancel(message: Message) -> bool:
    text = (message.text or "").strip().lower()
    return text in _CANCEL_WORDS


def _catalog():
    from shop_catalog import catalog_from_db_rows

    return catalog_from_db_rows(_require_db().shop_list_tariffs())


def _tariff_reply_kb():
    from shop_catalog import catalog_from_db_rows

    catalog = catalog_from_db_rows(_require_db().shop_list_tariffs())
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for t in catalog:
        row.append(KeyboardButton(text=t.short_name))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _period_reply_kb(tariff) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for _days, label, _price in tariff.period_buttons():
        row.append(KeyboardButton(text=label))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def start_lobby(message: Message, state: FSMContext) -> None:
    await state.clear()
    db = _require_db()
    uid = message.from_user.id if message.from_user else 0
    granted = db.lobby_has_granted(uid) if uid else False
    is_admin = admin_bot.is_admin(uid or None)
    catalog = _catalog()
    kb = ReplyKeyboardRemove()
    if not granted:
        kb = _tariff_reply_kb() if catalog else ReplyKeyboardRemove()
    await message.answer(
        welcome_text(already_granted=granted, is_admin=is_admin)
        + (
            ""
            if catalog or granted
            else "\n\nКаталог ещё пуст — админ: /lobby_sync, либо напишите в поддержку на аккаунт юзербота."
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )
    if not granted:
        await state.set_state(LobbyS.tariff)


@router.message(Command("lobby"))
async def cmd_lobby(message: Message, state: FSMContext) -> None:
    await start_lobby(message, state)


@router.message(Command("cancel"), LobbyS.tariff)
@router.message(Command("cancel"), LobbyS.duration)
@router.message(Command("cancel"), LobbyS.price)
@router.message(Command("cancel"), LobbyS.receipt)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Ок, отменил. Снова открыть лобби: меню бота → «Лобби компенсации».",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("lobby_sync"))
async def cmd_lobby_sync(message: Message) -> None:
    uid = message.from_user.id if message.from_user else None
    if not admin_bot.is_admin(uid):
        await message.answer("Обновление каталога только для администратора.")
        return
    if _bridge is None or getattr(_bridge, "auth", None) is None or getattr(_bridge.auth, "client", None) is None:
        await message.answer("Юзербот ещё не готов.")
        return
    await message.answer("Спрашиваю актуальные тарифы у @sweetshopxxx_bot…")

    async def _job():
        from shop_catalog import fetch_shop_catalog, tariffs_to_db_rows

        try:
            tariffs = await _bridge.call(fetch_shop_catalog(_bridge.auth.client), timeout=120)
            _require_db().shop_save_tariffs(tariffs_to_db_rows(tariffs))
            names = ", ".join(t.short_name for t in tariffs) or "—"
            await message.answer(f"Каталог обновлён ({len(tariffs)}): {names}")
        except Exception as e:
            await message.answer(f"Не удалось обновить каталог: {e}")

    asyncio.create_task(_job())


@router.message(Command("lobby_mail"))
async def cmd_lobby_mail(message: Message) -> None:
    uid = message.from_user.id if message.from_user else None
    if not admin_bot.is_admin(uid):
        await message.answer("Рассылка доступна только администратору.")
        return
    await _start_mailing(message)


@router.message(LobbyS.tariff, F.text)
async def on_tariff(message: Message, state: FSMContext) -> None:
    if _is_cancel(message):
        await cmd_cancel(message, state)
        return
    from shop_catalog import match_tariff

    catalog = _catalog()
    found = match_tariff(message.text or "", catalog) if catalog else None
    if found is None:
        if catalog:
            await message.answer(
                "Нажмите кнопку с тарифом из актуального списка магазина.",
                reply_markup=_tariff_reply_kb(),
            )
            return
        try:
            name = normalize_tariff(message.text or "")
        except ValueError as e:
            if str(e) == "отменено":
                await cmd_cancel(message, state)
                return
            await message.answer(f"{e}. Напишите название тарифа ещё раз.")
            return
        await state.update_data(tariff=name, shop_id="", price=0)
        await state.set_state(LobbyS.duration)
        await message.answer(
            f"Тариф: <b>{_esc(name)}</b>\nНа какой срок брали?",
            parse_mode="HTML",
        )
        return
    await state.update_data(
        tariff=found.short_name,
        shop_id=found.shop_id,
        price=0,
    )
    await state.set_state(LobbyS.duration)
    await message.answer(
        f"Тариф: <b>{_esc(found.short_name)}</b>\nНа какой срок покупали? Выберите кнопку.",
        parse_mode="HTML",
        reply_markup=_period_reply_kb(found),
    )


@router.message(LobbyS.tariff)
async def on_tariff_other(message: Message) -> None:
    await message.answer("Выберите тариф кнопкой.")


@router.message(LobbyS.duration, F.text)
async def on_duration(message: Message, state: FSMContext) -> None:
    if _is_cancel(message):
        await cmd_cancel(message, state)
        return
    data = await state.get_data()
    from shop_catalog import match_period

    catalog = _catalog()
    shop_id = str(data.get("shop_id") or "")
    tariff = next((t for t in catalog if t.shop_id == shop_id), None)
    if tariff is not None:
        hit = match_period(message.text or "", tariff)
        if hit:
            days, price, label = hit
            await state.update_data(duration_days=days, price=price)
            if price > 0:
                await state.set_state(LobbyS.receipt)
                await message.answer(
                    f"Срок: <b>{label}</b>\nЦена в магазине: <b>{price:g} ₽</b>\n\n"
                    + receipt_guide_text(),
                    parse_mode="HTML",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return
            await state.set_state(LobbyS.price)
            await message.answer(
                f"Срок: <b>{days} д.</b>\nНапишите цену, которую платили, числом.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
    try:
        days = parse_tariff_duration(message.text or "")
    except ValueError as e:
        await message.answer(f"{e}")
        return
    await state.update_data(duration_days=days)
    await state.set_state(LobbyS.price)
    await message.answer(
        f"Срок: <b>{days} д.</b>\nЗа какую цену покупали? Число в рублях.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(LobbyS.duration)
async def on_duration_other(message: Message) -> None:
    await message.answer("Напишите срок текстом, например: 30 дней.")


@router.message(LobbyS.price, F.text)
async def on_price(message: Message, state: FSMContext) -> None:
    if _is_cancel(message):
        await cmd_cancel(message, state)
        return
    try:
        price = parse_price(message.text or "")
    except ValueError as e:
        await message.answer(f"{e}")
        return
    await state.update_data(price=price)
    await state.set_state(LobbyS.receipt)
    await message.answer(receipt_guide_text(), parse_mode="HTML")


@router.message(LobbyS.price)
async def on_price_other(message: Message) -> None:
    await message.answer("Напишите цену числом, например: 1500.")


@router.message(LobbyS.receipt)
async def on_receipt(message: Message, state: FSMContext) -> None:
    if _is_cancel(message):
        await cmd_cancel(message, state)
        return
    if message.text and not (message.photo or message.document):
        await message.answer("Нужен файл чека (фото или PDF), не текст.")
        return
    data = await state.get_data()
    tariff = str(data.get("tariff") or "")
    days = int(data.get("duration_days") or 0)
    price = float(data.get("price") or 0)
    if not tariff or days <= 0 or price <= 0:
        await start_lobby(message, state)
        return

    item = receipt_from_message(message)
    ok, reason = validate_receipt(item, price)
    db = _require_db()
    uid = message.from_user.id if message.from_user else 0
    username = ""
    if message.from_user:
        username = message.from_user.username or message.from_user.full_name or ""
    shop_id = str(data.get("shop_id") or "")

    if db.lobby_has_granted(uid):
        await state.clear()
        await message.answer(
            welcome_text(already_granted=True),
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if not ok:
        db.lobby_save_claim(
            user_id=uid,
            username=username,
            tariff=tariff,
            duration_days=days,
            price=price,
            receipt_file_id=item.file_id,
            receipt_type=_receipt_kind(item),
            status="rejected",
            reject_reason=reason,
            shop_id=shop_id,
        )
        await message.answer(
            f"Чек не принят: {reason}.\n"
            "Пришлите другой скриншот или PDF, либо /lobby чтобы начать заново."
        )
        return

    forensic_notes = ""
    if _bot is not None and item.file_id:
        try:
            from receipt_forensics import inspect_receipt_bytes

            buf = await _bot.download(item.file_id)
            raw = buf.read() if hasattr(buf, "read") else bytes(buf)
            forensic = inspect_receipt_bytes(
                raw,
                filename=item.file_name,
                mime=item.mime,
                declared_price=price,
                caption=item.caption,
            )
            forensic_notes = "; ".join(forensic.flags)
            if not forensic.ok:
                db.lobby_save_claim(
                    user_id=uid,
                    username=username,
                    tariff=tariff,
                    duration_days=days,
                    price=price,
                    receipt_file_id=item.file_id,
                    receipt_type=_receipt_kind(item),
                    status="rejected",
                    reject_reason=forensic.reason,
                    shop_id=shop_id,
                    forensic_notes=forensic_notes,
                )
                await message.answer(
                    f"Чек отклонён: {forensic.reason}.\n"
                    "Пришлите исходный файл без Photoshop / обработки."
                )
                return
        except Exception:
            logger.exception("forensics via admin-bot")

    granted = granted_days_for(days)
    claim_id = db.lobby_save_claim(
        user_id=uid,
        username=username,
        tariff=tariff,
        duration_days=days,
        price=price,
        receipt_file_id=item.file_id,
        receipt_type=_receipt_kind(item),
        status="granted",
        granted_days=granted,
        shop_id=shop_id,
        forensic_notes=forensic_notes,
    )
    await state.clear()
    await message.answer(
        "✅ Чек принят, следов Photoshop не видно.\n\n"
        f"Возмещаем <b>один</b> тариф: <b>{_esc(tariff)}</b> "
        f"на <b>{days} д.</b> (цена {price:g} ₽).\n"
        "Больше одного тарифа выдать нельзя.\n\n"
        "Администратор откроет доступ.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _notify_admins_grant(
        claim_id=claim_id,
        user_id=uid,
        username=username,
        tariff=tariff,
        days=days,
        price=price,
        granted=granted,
        item=item,
        from_chat=message.chat.id,
        message_id=message.message_id,
    )


def _receipt_kind(item: ReceiptInput) -> str:
    if item.has_photo:
        return "photo"
    if (item.mime or "").lower() == "application/pdf" or (
        item.file_name or ""
    ).lower().endswith(".pdf"):
        return "pdf"
    if item.has_document:
        return "document"
    return "unknown"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def _notify_admins_grant(
    *,
    claim_id: int,
    user_id: int,
    username: str,
    tariff: str,
    days: int,
    price: float,
    granted: int,
    item: ReceiptInput,
    from_chat: int,
    message_id: int,
) -> None:
    bot = _bot
    if bot is None:
        return
    who = f"@{username}" if username else str(user_id)
    text = (
        f"✅ Лобби: заявка #{claim_id} принята.\n"
        f"Пользователь: {who} (<code>{user_id}</code>)\n"
        f"Тариф: {_esc(tariff)}\n"
        f"Брали: {days} д. за {price:g} ₽\n"
        f"Выдать: <b>один тариф</b> {_esc(tariff)} на {granted} д."
    )
    targets = list(config.ADMIN_IDS)
    if not targets:
        raw = _require_db().get("staging_chat_id") or ""
        if raw.isdigit():
            targets = [int(raw)]
    for aid in targets:
        if aid == user_id:
            continue
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
            try:
                await bot.copy_message(aid, from_chat, message_id)
            except Exception:
                if item.file_id and item.has_photo:
                    await bot.send_photo(aid, item.file_id)
                elif item.file_id:
                    await bot.send_document(aid, item.file_id)
        except Exception:
            logger.exception("notify admin %s about lobby grant", aid)


async def _start_mailing(message: Message) -> None:
    global _mail_task
    if _mail_task and not _mail_task.done():
        await message.answer("Рассылка уже идёт.")
        return
    if _bridge is None or getattr(_bridge, "auth", None) is None:
        await message.answer("Юзербот ещё не готов — подождите вход аккаунта.")
        return
    if getattr(_bridge.auth, "client", None) is None:
        await message.answer("Юзербот ещё не готов — подождите вход аккаунта.")
        return

    await message.answer(
        "Начинаю писать в личку тем, кто уже писал аккаунту. "
        "Повторно тем же людям не пишу."
    )

    async def _job() -> None:
        try:
            sent, skipped, failed = await _run_mailing()
            await message.answer(
                f"Рассылка лобби: отправлено {sent}, "
                f"пропущено {skipped}, ошибок {failed}."
            )
        except Exception as e:
            logger.exception("lobby mailing")
            await message.answer(f"Рассылка остановилась: {e}")

    _mail_task = asyncio.create_task(_job(), name="lobby-mail")


async def _run_mailing() -> tuple[int, int, int]:
    db = _require_db()
    bridge = _bridge
    if bridge is None:
        raise RuntimeError("нет юзербота")

    async def _collect():
        client = bridge.auth.client
        me = await client.get_me()
        self_id = int(me.id)
        dialogs = []
        count = 0
        async for d in client.get_dialogs():
            dialogs.append(d)
            count += 1
            if count >= 800:
                break
        skip = db.lobby_mailed_ids()
        skip.add(self_id)
        ids = collect_private_user_ids(dialogs, self_id=self_id, skip_ids=skip)
        return ids[:MAIL_LIMIT]

    targets = await bridge.call(_collect(), timeout=120)
    invite = mailing_invite_text(_bot_username)
    sent = skipped = failed = 0
    if not targets:
        return 0, 0, 0

    for uid in targets:
        if db.lobby_was_mailed(uid):
            skipped += 1
            continue
        if db.lobby_has_granted(uid):
            db.lobby_mark_mailed(uid)
            skipped += 1
            continue

        async def _send(peer=uid):
            await bridge.auth.client.send_message(peer, invite)

        try:
            await bridge.call(_send(), timeout=30)
            db.lobby_mark_mailed(uid)
            sent += 1
        except Exception as e:
            logger.warning("lobby mail to %s failed: %s", uid, e)
            db.lobby_mark_mailed(uid, error=str(e)[:300])
            failed += 1
        await asyncio.sleep(MAIL_GAP)
    return sent, skipped, failed
