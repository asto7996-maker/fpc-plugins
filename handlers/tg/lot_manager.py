"""Управление лотами Starvell: список, отключить, включить, удалить."""

from __future__ import annotations

import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from keyboards import cbt as CBT
from starvell_api import StarvellAPIError
from tg_bot import keyboards as KB

logger = logging.getLogger("starvell.lot_manager")

_LOT_ID_RE = re.compile(r"^[0-9a-f-]{8,}$", re.IGNORECASE)


def lot_actions_kb(offer_ref: str) -> InlineKeyboardMarkup:
    ref = str(offer_ref).strip()
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏸ Отключить", callback_data=f"{CBT.LOT_OFF}{ref}"),
            InlineKeyboardButton(text="▶️ Включить", callback_data=f"{CBT.LOT_ON}{ref}"),
        ],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{CBT.LOT_DEL_ASK}{ref}")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data=CBT.MAIN)],
    ])


def lot_delete_confirm_kb(offer_ref: str) -> InlineKeyboardMarkup:
    ref = str(offer_ref).strip()
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"{CBT.LOT_DEL_OK}{ref}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CBT.MAIN),
        ],
    ])


def _offer_ref(offer: dict[str, Any]) -> str:
    return str(offer.get("public_id") or offer.get("publicId") or offer.get("id") or "")


async def _resolve_offer(api, token: str) -> tuple[str, dict[str, Any] | None]:
    """Находит лот по numeric id или publicId."""
    token = (token or "").strip()
    if not token:
        return token, None
    offers = await api.fetch_my_offers(limit=300)
    for offer in offers:
        oid = str(offer.get("id") or "")
        pid = str(offer.get("public_id") or "")
        if token == oid or token == pid:
            return _offer_ref(offer) or token, offer
    fetched = await api.fetch_offer(token)
    if fetched:
        return str(fetched.get("publicId") or fetched.get("id") or token), fetched
    return token, None


def create_lot_manager_router(ctx: Any) -> Router:
    router = Router(name="lot_manager")

    async def _access(user_id: int) -> bool:
        return await ctx._has_access(user_id)

    def _api():
        return ctx.cardinal.get_api()

    async def _format_lots_list() -> str:
        api = _api()
        if not api:
            return "❌ Starvell не настроен."
        offers = await api.fetch_my_offers(limit=30)
        if not offers:
            return "📭 У вас нет лотов на Starvell."
        lines = ["📦 <b>Ваши лоты</b> (последние 30)", "━━━━━━━━━━━━━━━━━━"]
        for i, offer in enumerate(offers[:30], 1):
            ref = _offer_ref(offer)
            title = (offer.get("title") or "—")[:60]
            price = offer.get("price") or "?"
            active = offer.get("is_active", True)
            status = "🟢" if active else "⏸"
            lines.append(
                f"{i}. {status} <code>{ref}</code> · {price} ₽\n   {title}"
            )
        lines += [
            "",
            "Команды:",
            "• <code>/lot_off ID</code> — отключить",
            "• <code>/lot_on ID</code> — включить",
            "• <code>/lot_del ID</code> — удалить",
        ]
        return "\n".join(lines)

    @router.message(Command("lots"))
    async def cmd_lots(message: Message) -> None:
        if not await _access(message.from_user.id):
            return
        try:
            text = await _format_lots_list()
        except Exception as exc:
            await message.answer(f"❌ Ошибка: <code>{exc}</code>", parse_mode="HTML")
            return
        await message.answer(text, parse_mode="HTML", reply_markup=KB.back_menu())

    async def _lot_action(message: Message, token: str, action: str) -> None:
        api = _api()
        if not api:
            await message.answer("❌ Starvell не настроен.", reply_markup=KB.back_menu())
            return
        ref, offer = await _resolve_offer(api, token)
        if not offer and not _LOT_ID_RE.match(token) and not token.isdigit():
            await message.answer(f"❌ Лот <code>{token}</code> не найден.", parse_mode="HTML")
            return
        try:
            if action == "off":
                oid = offer.get("id") if isinstance(offer, dict) else token
                pid = None
                if isinstance(offer, dict):
                    pid = offer.get("public_id") or offer.get("publicId")
                await api.deactivate_offer(oid or token, public_id=pid)
                await message.answer(f"⏸ Лот <code>{ref}</code> отключён.", parse_mode="HTML")
            elif action == "on":
                await api.activate_offer(ref)
                await message.answer(f"▶️ Лот <code>{ref}</code> включён.", parse_mode="HTML")
            elif action == "del":
                await api.delete_offer(ref)
                await message.answer(f"🗑 Лот <code>{ref}</code> удалён.", parse_mode="HTML")
        except StarvellAPIError as exc:
            await message.answer(f"❌ Starvell: <code>{exc}</code>", parse_mode="HTML")
        except Exception as exc:
            logger.exception("lot action %s", action)
            await message.answer(f"❌ Ошибка: <code>{exc}</code>", parse_mode="HTML")

    @router.message(Command("lot_off"))
    async def cmd_lot_off(message: Message) -> None:
        if not await _access(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: <code>/lot_off ID</code>", parse_mode="HTML")
            return
        await _lot_action(message, parts[1].strip(), "off")

    @router.message(Command("lot_on"))
    async def cmd_lot_on(message: Message) -> None:
        if not await _access(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: <code>/lot_on ID</code>", parse_mode="HTML")
            return
        await _lot_action(message, parts[1].strip(), "on")

    @router.message(Command("lot_del"))
    async def cmd_lot_del(message: Message) -> None:
        if not await _access(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: <code>/lot_del ID</code>", parse_mode="HTML")
            return
        token = parts[1].strip()
        await message.answer(
            f"🗑 Удалить лот <code>{token}</code>?",
            parse_mode="HTML",
            reply_markup=lot_delete_confirm_kb(token),
        )

    @router.callback_query(F.data.startswith(CBT.LOT_OFF))
    async def cb_lot_off(call: CallbackQuery) -> None:
        if not await _access(call.from_user.id):
            return
        token = call.data[len(CBT.LOT_OFF):]
        await call.answer()
        await _lot_action(call.message, token, "off")

    @router.callback_query(F.data.startswith(CBT.LOT_ON))
    async def cb_lot_on(call: CallbackQuery) -> None:
        if not await _access(call.from_user.id):
            return
        token = call.data[len(CBT.LOT_ON):]
        await call.answer()
        await _lot_action(call.message, token, "on")

    @router.callback_query(F.data.startswith(CBT.LOT_DEL_ASK))
    async def cb_lot_del_ask(call: CallbackQuery) -> None:
        if not await _access(call.from_user.id):
            return
        token = call.data[len(CBT.LOT_DEL_ASK):]
        await call.answer()
        await call.message.edit_text(
            f"🗑 Удалить лот <code>{token}</code>?",
            parse_mode="HTML",
            reply_markup=lot_delete_confirm_kb(token),
        )

    @router.callback_query(F.data.startswith(CBT.LOT_DEL_OK))
    async def cb_lot_del_ok(call: CallbackQuery) -> None:
        if not await _access(call.from_user.id):
            return
        token = call.data[len(CBT.LOT_DEL_OK):]
        await call.answer()
        await _lot_action(call.message, token, "del")

    return router
