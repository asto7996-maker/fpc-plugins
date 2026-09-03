"""
support_inbox.py — юзербот не ведёт компенсацию в личке.

1. Сканирует непрочитанные лички → база своих людей → зовёт в @бот-панель.
2. Кто уже в базе и пишет снова — короткое напоминание про бота.
3. Левые (новые) пользователи блокируются.
Тарифы магазина по-прежнему подтягиваются здесь же (для кнопок в боте).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pyrogram.enums import ChatType, ParseMode

import config
from access import decide_inbox_action, is_privileged
from database import Database
from shop_catalog import catalog_from_db_rows, catalog_prices_summary, fetch_shop_catalog, tariffs_to_db_rows

logger = logging.getLogger(__name__)


def bot_username() -> str:
    return (getattr(config, "BOT_USERNAME", "") or "").lstrip("@")


def invite_to_bot_text(uname: str = "") -> str:
    handle = (uname or bot_username() or "ZzzLV_bot").lstrip("@")
    return (
        "Это поддержка. Компенсацию оформляем только в боте, не здесь.\n\n"
        f"Откройте @{handle} → команда /lobby → выберите тариф и срок "
        "(цены из магазина) → пришлите чек. Нужна настоящая квитанция, "
        "не случайное фото и не Photoshop.\n\n"
        "Сообщения в этот чат от новых людей не принимаются."
    )


def remind_text(uname: str = "") -> str:
    handle = (uname or bot_username() or "ZzzLV_bot").lstrip("@")
    return (
        f"Напишите боту @{handle} команду /lobby и следуйте кнопкам. "
        "Здесь компенсацию не оформляем."
    )


def stranger_text() -> str:
    return "Сообщения от новых пользователей не принимаются."


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
    """Сохранён для тестов: старые чаты при первом проходе не спамим."""
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


def load_catalog(db: Database):
    return catalog_from_db_rows(db.shop_list_tariffs())


async def refresh_catalog(client: Any, db: Database):
    tariffs = await fetch_shop_catalog(client)
    db.shop_save_tariffs(tariffs_to_db_rows(tariffs))
    return tariffs


def _import_legacy_known(db: Database) -> int:
    n = 0
    for pid in db.lobby_mailed_ids():
        if db.known_get(pid) is None:
            db.known_upsert(pid, source="mailing")
            n += 1
    for pid in db.lobby_claim_user_ids():
        if db.known_get(pid) is None:
            db.known_upsert(pid, source="claim")
            n += 1
    return n


async def _send(client: Any, chat_id: int, text: str) -> None:
    await client.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def _block_stranger(client: Any, db: Database, peer_id: int) -> None:
    try:
        await _send(client, peer_id, stranger_text())
    except Exception:
        logger.debug("notice before block %s failed", peer_id, exc_info=True)
    try:
        await client.block_user(peer_id)
    except Exception:
        logger.exception("block_user %s", peer_id)
    db.known_mark_blocked(peer_id)
    logger.info("заблокирован левый пользователь %s", peer_id)


async def poll_once(client: Any, db: Database, panel_username: str = "") -> int:
    me = await client.get_me()
    self_id = int(me.id)
    shop_id = await resolve_shop_user_id(client)
    handle = panel_username or bot_username()
    seed_done = db.seed_done()
    if not seed_done:
        _import_legacy_known(db)
    handled = 0
    async for dialog in client.get_dialogs(limit=80):
        chat = dialog.chat
        if not _chat_ok(chat, self_id, shop_id):
            continue
        top = getattr(dialog, "top_message", None)
        peer_id = int(chat.id)
        username = (getattr(chat, "username", None) or "") or ""
        unread = int(getattr(dialog, "unread_messages_count", 0) or 0)
        outgoing = bool(getattr(top, "outgoing", False)) if top is not None else True
        incoming = bool(top is not None and not outgoing)
        top_id = int(getattr(top, "id", 0) or 0) if top is not None else 0
        row = db.known_get(peer_id)
        privileged = is_privileged(peer_id, username)
        action = decide_inbox_action(
            privileged=privileged,
            known=row is not None and int((row or {}).get("blocked") or 0) == 0,
            blocked=bool(row and int(row.get("blocked") or 0)),
            incoming=incoming,
            seed_done=seed_done,
            unread=unread > 0,
        )
        if action == "ignore":
            continue
        if action == "seed":
            db.known_upsert(
                peer_id,
                username=username,
                source="unread",
                unread_at_scan=unread,
                last_msg_id=top_id,
            )
            try:
                await _send(client, peer_id, invite_to_bot_text(handle))
                db.known_mark_invited(peer_id, top_id)
                handled += 1
            except Exception:
                logger.exception("invite known %s", peer_id)
            await asyncio.sleep(0.4)
            continue
        if action == "remind":
            if privileged:
                db.known_upsert(peer_id, username=username, source="test", last_msg_id=top_id)
            last = int((row or {}).get("last_msg_id") or 0)
            if top_id and top_id <= last:
                continue
            try:
                await _send(client, peer_id, remind_text(handle))
                db.known_upsert(peer_id, username=username, last_msg_id=top_id)
                if row is None or not int((row or {}).get("invited") or 0):
                    db.known_mark_invited(peer_id, top_id)
                handled += 1
            except Exception:
                logger.exception("remind %s", peer_id)
            await asyncio.sleep(0.35)
            continue
        if action == "block":
            await _block_stranger(client, db, peer_id)
            handled += 1
            await asyncio.sleep(0.35)
    if not seed_done:
        db.mark_seed_done()
        logger.info("сиды непрочитанных чатов сохранены")
    return handled


async def run_forever(bridge, db: Database) -> None:
    logger.info("Инбокс поддержки запущен (только свои + блок левых)")
    last_sync = 0.0
    catalog = load_catalog(db)
    interval = float(getattr(config, "SUPPORT_INBOX_SECONDS", 12.0))
    panel = ""
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
            if not panel:
                panel = (getattr(bridge, "bot_username", None) or "") or bot_username()
            now = time.time()
            if now - last_sync > 6 * 3600 or not catalog:
                try:
                    catalog = await refresh_catalog(client, db)
                    last_sync = now
                    logger.info(
                        "Тарифы магазина обновлены:\n%s",
                        catalog_prices_summary(catalog),
                    )
                except Exception:
                    logger.exception("не удалось обновить тарифы магазина")
                    catalog = load_catalog(db) or catalog
                    last_sync = now + 15 * 60
            await poll_once(client, db, panel)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("support inbox loop")
        await asyncio.sleep(interval)
