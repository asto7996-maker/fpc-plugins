"""
support_inbox.py — юзербот сам фильтрует лички поддержки и ведёт компенсацию.

Люди пишут на аккаунт техподдержки (юзербот). Бот отвечает вариантами
тарифов из @sweetshopxxx_bot, принимает чек, проверяет его и выдаёт
максимум один тариф.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

import config
from database import Database
from lobby import ReceiptInput, parse_price, validate_receipt
from receipt_forensics import inspect_receipt_bytes
from shop_catalog import (
    Tariff,
    catalog_from_db_rows,
    fetch_shop_catalog,
    match_period,
    match_tariff,
    tariffs_to_db_rows,
)

logger = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_TARIFF = "tariff"
STATE_PERIOD = "period"
STATE_PRICE = "price"
STATE_RECEIPT = "receipt"
STATE_DONE = "done"

CANCEL = frozenset({"отмена", "cancel", "/cancel", "стоп"})


def welcome_text() -> str:
    return (
        "Приватные каналы с товаром были заблокированы — это поддержка.\n\n"
        "Можем <b>возместить максимум один тариф</b>: тот, который вы покупали. "
        "Нужен чек оплаты (его проверим на подделку и Photoshop).\n\n"
        "Выберите тариф из актуального списка <b>@sweetshopxxx_bot</b>:"
    )


def already_granted_text() -> str:
    return (
        "По этому аккаунту уже возмещён один тариф — больше одного выдать нельзя.\n"
        "Если доступ ещё не открыли, подождите или напишите сюда «доступ»."
    )


def receipt_guide() -> str:
    return (
        "Пришлите чек покупки <b>файлом или фото</b> (лучше «как документ», "
        "без сжатия Telegram).\n\n"
        "Как получить чек:\n"
        "• банк / СБП → операция → квитанция / чек → скачать или полный скриншот;\n"
        "• на чеке должны быть сумма, дата и получатель.\n\n"
        "Нельзя: стикеры, видео, обрезок, картинка из Photoshop / редактора."
    )


def tariff_keyboard(catalog: list[Tariff]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for t in catalog:
        row.append(KeyboardButton(t.short_name))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton("Отмена")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def period_keyboard(tariff: Tariff) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for _days, label, _price in tariff.period_buttons():
        row.append(KeyboardButton(label))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton("Отмена")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _esc(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _chat_ok(chat: Any, self_id: int, shop_id: int) -> bool:
    if chat is None:
        return False
    uid = getattr(chat, "id", None)
    if uid is None:
        return False
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return False
    if uid in {int(self_id), int(shop_id)} or uid <= 0:
        return False
    if getattr(chat, "is_bot", False):
        return False
    t = getattr(chat, "type", None)
    blob = f"{getattr(t, 'name', t)} {getattr(t, 'value', '')} {t}".lower()
    return "private" in blob or t == ChatType.PRIVATE


def should_handle_top(
    *,
    has_session: bool,
    last_handled_id: int,
    top_id: int,
    top_outgoing: bool,
    unread: int,
    top_age_hours: float,
    catchup_hours: float,
) -> tuple[bool, str]:
    """
    Фильтр личек.
    Возвращает (обрабатывать, зачем).
    Старые чаты при первом проходе только запоминаем, не пишем всем подряд.
    """
    if top_id <= 0:
        return False, "empty"
    if top_outgoing:
        return False, "ours"
    if has_session and top_id <= last_handled_id:
        return False, "already"
    if not has_session:
        if unread > 0 or top_age_hours <= catchup_hours:
            return True, "new-recent"
        return False, "seed-old"
    return True, "incoming"


async def resolve_shop_user_id(client: Any) -> int:
    try:
        chat = await client.get_chat((config.SHOP_BOT_USERNAME or "sweetshopxxx_bot").lstrip("@"))
        return int(chat.id)
    except Exception:
        return 0


def load_catalog(db: Database) -> list[Tariff]:
    return catalog_from_db_rows(db.shop_list_tariffs())


async def refresh_catalog(client: Any, db: Database) -> list[Tariff]:
    tariffs = await fetch_shop_catalog(client)
    db.shop_save_tariffs(tariffs_to_db_rows(tariffs))
    return tariffs


def _msg_text(message: Any) -> str:
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def _receipt_input_from_pyrogram(message: Any) -> ReceiptInput:
    photo = getattr(message, "photo", None)
    doc = getattr(message, "document", None)
    file_id = ""
    mime = ""
    size = 0
    name = ""
    if photo is not None:
        file_id = getattr(photo, "file_id", "") or ""
        size = int(getattr(photo, "file_size", 0) or 0)
        mime = "image/jpeg"
    if doc is not None:
        file_id = getattr(doc, "file_id", "") or file_id
        mime = getattr(doc, "mime_type", "") or mime
        size = int(getattr(doc, "file_size", 0) or size or 0)
        name = getattr(doc, "file_name", "") or ""
    return ReceiptInput(
        has_photo=photo is not None,
        has_document=doc is not None,
        mime=mime,
        file_size=size,
        file_name=name,
        caption=_msg_text(message),
        has_sticker=getattr(message, "sticker", None) is not None,
        has_video=getattr(message, "video", None) is not None,
        has_voice=getattr(message, "voice", None) is not None,
        has_animation=getattr(message, "animation", None) is not None,
        has_video_note=getattr(message, "video_note", None) is not None,
        file_id=file_id,
    )


async def _download_bytes(client: Any, message: Any) -> tuple[bytes, str]:
    suffix = ".bin"
    doc = getattr(message, "document", None)
    if getattr(message, "photo", None) is not None:
        suffix = ".jpg"
    if doc is not None:
        name = (getattr(doc, "file_name", "") or "").lower()
        mime = (getattr(doc, "mime_type", "") or "").lower()
        if name.endswith(".png") or "png" in mime:
            suffix = ".png"
        elif name.endswith(".pdf") or "pdf" in mime:
            suffix = ".pdf"
        elif name.endswith(".webp"):
            suffix = ".webp"
        else:
            suffix = ".jpg"
    tmp = tempfile.NamedTemporaryFile(prefix="receipt_", suffix=suffix, delete=False)
    tmp.close()
    path = tmp.name
    try:
        await client.download_media(message, file_name=path)
        data = Path(path).read_bytes()
        return data, path
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


async def _reply(client: Any, chat_id: int, text: str, kb=None) -> None:
    await client.send_message(
        chat_id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _start_flow(client: Any, db: Database, peer_id: int, catalog: list[Tariff]) -> None:
    if db.lobby_has_granted(peer_id):
        db.support_upsert(peer_id, state=STATE_DONE)
        await _reply(client, peer_id, already_granted_text(), ReplyKeyboardRemove())
        return
    if not catalog:
        await _reply(
            client,
            peer_id,
            "Сейчас подтягиваю тарифы из магазина, напишите ещё раз через минуту.",
        )
        return
    db.support_upsert(peer_id, state=STATE_TARIFF, shop_id="", tariff="", duration_days=0, price=0)
    await _reply(client, peer_id, welcome_text(), tariff_keyboard(catalog))


async def handle_private_message(
    client: Any,
    db: Database,
    message: Any,
    catalog: list[Tariff],
) -> None:
    peer_id = int(message.chat.id)
    msg_id = int(message.id)
    text = _msg_text(message)
    sess = db.support_get(peer_id) or {}
    state = sess.get("state") or STATE_IDLE

    db.support_upsert(peer_id, last_msg_id=msg_id)

    if db.lobby_has_granted(peer_id) and state != STATE_RECEIPT:
        db.support_upsert(peer_id, state=STATE_DONE)
        await _reply(client, peer_id, already_granted_text(), ReplyKeyboardRemove())
        return

    if text.lower() in CANCEL:
        db.support_upsert(peer_id, state=STATE_IDLE, shop_id="", tariff="", duration_days=0, price=0)
        await _reply(client, peer_id, "Ок, отменил. Напишите ещё раз, когда будете готовы выбрать тариф.", ReplyKeyboardRemove())
        return

    if state in {STATE_IDLE, STATE_DONE} or not state:
        found = match_tariff(text, catalog) if text else None
        if found:
            await _choose_tariff(client, db, peer_id, found)
            return
        await _start_flow(client, db, peer_id, catalog)
        return

    if state == STATE_TARIFF:
        found = match_tariff(text, catalog)
        if not found:
            await _reply(
                client,
                peer_id,
                "Нажмите кнопку с тарифом из списка — возмещаем только актуальные подписки магазина.",
                tariff_keyboard(catalog),
            )
            return
        await _choose_tariff(client, db, peer_id, found)
        return

    if state == STATE_PERIOD:
        shop_id = str(sess.get("shop_id") or "")
        tariff = next((t for t in catalog if t.shop_id == shop_id), None)
        if tariff is None:
            await _start_flow(client, db, peer_id, catalog)
            return
        hit = match_period(text, tariff)
        if not hit:
            await _reply(
                client,
                peer_id,
                "Выберите срок кнопкой.",
                period_keyboard(tariff),
            )
            return
        days, price, label = hit
        db.support_upsert(peer_id, duration_days=days, price=price)
        if price > 0:
            db.support_upsert(peer_id, state=STATE_RECEIPT)
            await _reply(
                client,
                peer_id,
                f"Тариф: <b>{_esc(tariff.short_name)}</b>\n"
                f"Срок: <b>{label}</b>\n"
                f"Цена в магазине: <b>{price:g} ₽</b>\n\n" + receipt_guide(),
                ReplyKeyboardRemove(),
            )
        else:
            db.support_upsert(peer_id, state=STATE_PRICE)
            await _reply(
                client,
                peer_id,
                f"Срок: <b>{days} д.</b>\nНапишите цену, которую платили, числом в рублях.",
                ReplyKeyboardRemove(),
            )
        return

    if state == STATE_PRICE:
        try:
            price = parse_price(text)
        except ValueError as e:
            await _reply(client, peer_id, str(e))
            return
        db.support_upsert(peer_id, price=price, state=STATE_RECEIPT)
        await _reply(client, peer_id, receipt_guide())
        return

    if state == STATE_RECEIPT:
        await _handle_receipt(client, db, message, catalog)
        return


async def _choose_tariff(client: Any, db: Database, peer_id: int, tariff: Tariff) -> None:
    db.support_upsert(
        peer_id,
        state=STATE_PERIOD,
        shop_id=tariff.shop_id,
        tariff=tariff.short_name,
        duration_days=0,
        price=0,
    )
    await _reply(
        client,
        peer_id,
        f"Тариф: <b>{_esc(tariff.short_name)}</b>\n"
        "На какой срок покупали? Выберите вариант.",
        period_keyboard(tariff),
    )


async def _handle_receipt(
    client: Any, db: Database, message: Any, catalog: list[Tariff]
) -> None:
    peer_id = int(message.chat.id)
    sess = db.support_get(peer_id) or {}
    tariff_name = str(sess.get("tariff") or "")
    days = int(sess.get("duration_days") or 0)
    price = float(sess.get("price") or 0)
    shop_id = str(sess.get("shop_id") or "")
    if not tariff_name or days <= 0:
        await _start_flow(client, db, peer_id, catalog)
        return

    item = _receipt_input_from_pyrogram(message)
    ok, reason = validate_receipt(item, price)
    if not ok:
        db.lobby_save_claim(
            user_id=peer_id,
            username="",
            tariff=tariff_name,
            duration_days=days,
            price=price,
            receipt_file_id=item.file_id,
            receipt_type="photo" if item.has_photo else "document",
            status="rejected",
            reject_reason=reason,
            shop_id=shop_id,
        )
        await _reply(client, peer_id, f"Чек не принят: {reason}. Пришлите другой файл.")
        return

    try:
        data, _path = await _download_bytes(client, message)
    except Exception as e:
        logger.exception("download receipt")
        await _reply(client, peer_id, f"Не удалось скачать чек: {e}")
        return

    forensic = inspect_receipt_bytes(
        data,
        filename=item.file_name,
        mime=item.mime,
        declared_price=price,
        caption=item.caption,
    )
    notes = "; ".join(forensic.flags)
    if not forensic.ok:
        db.lobby_save_claim(
            user_id=peer_id,
            username="",
            tariff=tariff_name,
            duration_days=days,
            price=price,
            receipt_file_id=item.file_id,
            receipt_type="photo" if item.has_photo else "document",
            status="rejected",
            reject_reason=forensic.reason,
            shop_id=shop_id,
            forensic_notes=notes,
        )
        await _reply(
            client,
            peer_id,
            f"Чек отклонён: {forensic.reason}.\n"
            "Пришлите исходный файл без обработки.",
        )
        return

    if db.lobby_has_granted(peer_id):
        db.support_upsert(peer_id, state=STATE_DONE)
        await _reply(client, peer_id, already_granted_text(), ReplyKeyboardRemove())
        return

    claim_id = db.lobby_save_claim(
        user_id=peer_id,
        username="",
        tariff=tariff_name,
        duration_days=days,
        price=price,
        receipt_file_id=item.file_id,
        receipt_type="photo" if item.has_photo else "document",
        status="granted",
        granted_days=days,
        shop_id=shop_id,
        forensic_notes=notes,
    )
    db.support_upsert(peer_id, state=STATE_DONE)
    await _reply(
        client,
        peer_id,
        "✅ Чек принят, следов Photoshop не видно.\n\n"
        f"Возмещаем <b>один</b> тариф: <b>{_esc(tariff_name)}</b> "
        f"на <b>{days} д.</b> (цена {price:g} ₽).\n\n"
        "Больше одного тарифа выдать нельзя. Администратор откроет доступ.",
        ReplyKeyboardRemove(),
    )
    await _notify_admins_via_userbot(
        client,
        claim_id=claim_id,
        user_id=peer_id,
        tariff=tariff_name,
        days=days,
        price=price,
        from_chat=peer_id,
        message_id=int(message.id),
        notes=notes,
    )


async def _notify_admins_via_userbot(
    client: Any,
    *,
    claim_id: int,
    user_id: int,
    tariff: str,
    days: int,
    price: float,
    from_chat: int,
    message_id: int,
    notes: str,
) -> None:
    text = (
        f"✅ Компенсация #{claim_id}: один тариф.\n"
        f"Пользователь id <code>{user_id}</code>\n"
        f"Тариф: {_esc(tariff)}\n"
        f"{days} д. / {price:g} ₽\n"
        f"Проверка чека: ок ({_esc(notes) or 'без флагов'})"
    )
    for aid in config.ADMIN_IDS:
        if aid == user_id:
            continue
        try:
            await client.send_message(aid, text, parse_mode=ParseMode.HTML)
            try:
                await client.forward_messages(aid, from_chat, message_id)
            except Exception:
                logger.debug("forward receipt to admin failed", exc_info=True)
        except Exception:
            logger.exception("notify admin %s via userbot", aid)


async def poll_once(client: Any, db: Database, catalog: list[Tariff]) -> int:
    """Один проход по свежим личкам. Возвращает число обработанных входящих."""
    me = await client.get_me()
    self_id = int(me.id)
    shop_id = await resolve_shop_user_id(client)
    catchup = float(getattr(config, "SUPPORT_CATCHUP_HOURS", 36.0))
    handled = 0
    n = 0
    async for dialog in client.get_dialogs(limit=80):
        n += 1
        chat = dialog.chat
        if not _chat_ok(chat, self_id, shop_id):
            continue
        top = getattr(dialog, "top_message", None)
        if top is None:
            continue
        peer_id = int(chat.id)
        sess = db.support_get(peer_id)
        top_id = int(getattr(top, "id", 0) or 0)
        outgoing = bool(getattr(top, "outgoing", False))
        unread = int(getattr(dialog, "unread_messages_count", 0) or 0)
        ts = getattr(getattr(top, "date", None), "timestamp", lambda: 0)()
        try:
            age_h = max(0.0, (time.time() - float(ts)) / 3600.0) if ts else 999.0
        except Exception:
            age_h = 999.0
        last_id = int((sess or {}).get("last_msg_id") or 0)
        go, why = should_handle_top(
            has_session=sess is not None,
            last_handled_id=last_id,
            top_id=top_id,
            top_outgoing=outgoing,
            unread=unread,
            top_age_hours=age_h,
            catchup_hours=catchup,
        )
        if why == "seed-old":
            db.support_upsert(peer_id, last_msg_id=top_id, state=STATE_IDLE)
            continue
        if not go:
            continue
        try:
            await handle_private_message(client, db, top, catalog)
            handled += 1
        except Exception:
            logger.exception("support handle %s", peer_id)
            db.support_upsert(peer_id, last_msg_id=top_id)
        await asyncio.sleep(0.35)
    return handled


async def run_forever(bridge, db: Database) -> None:
    logger.info("Инбокс поддержки запущен")
    last_sync = 0.0
    catalog: list[Tariff] = load_catalog(db)
    interval = float(getattr(config, "SUPPORT_INBOX_SECONDS", 12.0))
    await asyncio.sleep(10)
    while True:
        try:
            auth = getattr(bridge, "auth", None)
            client = getattr(auth, "client", None) if auth else None
            poster = getattr(bridge, "poster", None)
            if client is None:
                await asyncio.sleep(interval)
                continue
            if poster is not None and getattr(poster, "is_busy", False):
                await asyncio.sleep(interval)
                continue
            now = time.time()
            if now - last_sync > 6 * 3600 or not catalog:
                try:
                    catalog = await refresh_catalog(client, db)
                    last_sync = now
                    logger.info("Тарифы магазина обновлены: %s", [t.short_name for t in catalog])
                except Exception:
                    logger.exception("не удалось обновить тарифы магазина")
                    catalog = load_catalog(db) or catalog
                    last_sync = now + 15 * 60  # не долбить магазин
            await poll_once(client, db, catalog)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("support inbox loop")
        await asyncio.sleep(interval)
