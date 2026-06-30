"""Парсер FunPay → автосоздание лота на Starvell."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import load_settings, save_settings
from keyboards import cbt as CBT
from services.category_mapper import resolve_starvell_category, save_category_mapping
from services.funpay_parser import ParsedLot, fetch_funpay_lot
from services.starvell_lot_creator import create_lot_from_parsed, format_created_message
from services.yandex_translate import parse_price_hint, translate_ru_to_en
from tg_bot import keyboards as KB

logger = logging.getLogger("starvell.lot_parser")


class ParserStates(StatesGroup):
    waiting_url = State()
    waiting_smm_id = State()
    waiting_price = State()
    waiting_category_fallback = State()


def create_lot_parser_router(ctx: Any) -> Router:
    router = Router(name="lot_parser")

    async def _access(user_id: int) -> bool:
        return await ctx._has_access(user_id)

    def _api():
        return ctx.cardinal.get_api()

    async def _parser_intro() -> str:
        return (
            "🔍 <b>Парсер лотов FunPay</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Отправьте ссылку на лот FunPay — бот:\n"
            "• определит категорию FunPay и подберёт такую же на Starvell\n"
            "• скопирует краткое и подробное описание\n"
            "• переведёт на английский (Яндекс.Переводчик)\n"
            "• создаст лот (наличие 999999, автодоставка)\n"
            "• для SMM — сообщение после оплаты и <code>ID:</code>\n\n"
            "Пример:\n"
            "<code>https://funpay.com/lots/offer?id=12345678</code>\n\n"
            "<i>/cancel или ◀️ Меню — выход</i>"
        )

    async def _translate_lot(lot: ParsedLot) -> None:
        try:
            brief_src = lot.brief_ru or lot.title
            full_src = lot.full_ru or brief_src
            lot.brief_en, lot.full_en = await asyncio.wait_for(
                asyncio.gather(
                    translate_ru_to_en(brief_src),
                    translate_ru_to_en(full_src),
                ),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            lot.brief_en = lot.brief_ru
            lot.full_en = lot.full_ru
        except Exception as exc:
            logger.warning("Yandex translate: %s", exc)
            lot.brief_en = lot.brief_ru
            lot.full_en = lot.full_ru

    async def _resolve_category(lot: ParsedLot):
        s = load_settings()
        return await resolve_starvell_category(
            lot.funpay_node_id,
            lot.funpay_category_title,
            settings_overrides=s.parser_funpay_category_map,
        )

    async def _find_template_offer(category_id: int) -> str | int | None:
        api = _api()
        if not api:
            return None
        try:
            for cat in await api.fetch_seller_categories():
                if int(cat.get("category_id") or 0) == category_id:
                    return cat.get("offer_id")
        except Exception as exc:
            logger.debug("template offer: %s", exc)
        return None

    async def _create_and_reply(
        message: Message,
        state: FSMContext,
        *,
        category_id: int,
        price: str,
        template_offer_id: str | int | None = None,
    ) -> None:
        data = await state.get_data()
        lot_data = data.get("parsed_lot") or {}
        is_smm = bool(data.get("is_smm"))
        service_id = int(data.get("service_id") or 0) or None
        s = load_settings()
        api = _api()

        if not api or not s.is_starvell_configured():
            await message.answer(
                "❌ Starvell не настроен. Укажите session cookie в профиле.",
                reply_markup=KB.back_menu(),
            )
            await state.clear()
            return

        if template_offer_id is None:
            template_offer_id = await _find_template_offer(category_id)

        lot = ParsedLot(
            source_url=lot_data.get("source_url", ""),
            offer_id=lot_data.get("offer_id", ""),
            title=lot_data.get("title", ""),
            brief_ru=lot_data.get("brief_ru", ""),
            full_ru=lot_data.get("full_ru", ""),
            brief_en=lot_data.get("brief_en", ""),
            full_en=lot_data.get("full_en", ""),
            funpay_node_id=int(lot_data.get("funpay_node_id") or 0),
            funpay_category_title=lot_data.get("funpay_category_title", ""),
        )

        wait = await message.answer("⏳ Создаю лот на Starvell…")
        try:
            result = await asyncio.wait_for(
                create_lot_from_parsed(
                    api,
                    lot,
                    is_smm=is_smm,
                    service_id=service_id if is_smm else None,
                    price=price,
                    category_id=category_id,
                    template_offer_id=template_offer_id,
                    auto_delivery=s.parser_auto_delivery,
                ),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            await wait.edit_text("❌ Starvell не ответил вовремя. Попробуйте позже.")
            return
        except Exception as exc:
            logger.exception("create lot")
            await wait.edit_text(f"❌ Не удалось создать лот:\n<code>{exc}</code>", parse_mode="HTML")
            return

        await state.clear()
        cat_title = lot_data.get("starvell_category_name") or lot_data.get("funpay_category_title") or ""
        text = format_created_message(
            title=lot.title,
            url=result.get("url", ""),
            price=price,
            category_id=category_id,
            is_smm=is_smm,
            service_id=service_id,
        )
        if cat_title:
            text += f"\n📁 Категория: <b>{cat_title}</b>"
        await wait.edit_text(text, parse_mode="HTML", reply_markup=KB.back_menu(), disable_web_page_preview=False)

    async def _ask_price(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        price = data.get("price_hint") or load_settings().parser_default_price
        cat_line = data.get("funpay_category_title") or "—"
        sv_line = data.get("starvell_category_name") or "—"
        await message.answer(
            f"📁 FunPay: <b>{cat_line}</b>\n"
            f"🎯 Starvell: <b>{sv_line}</b>\n\n"
            f"Отправьте <b>цену</b> (₽ за 1 шт.) или нажмите кнопку:\n"
            f"Подсказка: <code>{price}</code>",
            parse_mode="HTML",
            reply_markup=KB.parser_price_kb(price),
        )
        await state.set_state(ParserStates.waiting_price)

    async def _ask_category_fallback(message: Message, state: FSMContext, lot: ParsedLot) -> None:
        await message.answer(
            "⚠️ <b>Не удалось автоматически определить категорию Starvell</b>\n\n"
            f"FunPay: <b>{lot.funpay_category_title or '—'}</b>\n"
            f"Node ID: <code>{lot.funpay_node_id or '—'}</code>\n\n"
            "Отправьте <b>ID категории Starvell</b> (число).\n"
            "Бот запомнит соответствие для этой категории FunPay.",
            parse_mode="HTML",
        )
        await state.set_state(ParserStates.waiting_category_fallback)

    @router.message(Command("parser"))
    async def cmd_parser(message: Message, state: FSMContext) -> None:
        if not await _access(message.from_user.id):
            return
        await state.clear()
        await message.answer(await _parser_intro(), parse_mode="HTML", reply_markup=KB.back_menu())
        await state.set_state(ParserStates.waiting_url)

    @router.callback_query(F.data == CBT.PARSER)
    async def cb_parser_menu(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await state.clear()
        await call.message.edit_text(await _parser_intro(), parse_mode="HTML", reply_markup=KB.back_menu())
        await state.set_state(ParserStates.waiting_url)
        await call.answer()

    @router.message(ParserStates.waiting_url)
    async def on_funpay_url(message: Message, state: FSMContext) -> None:
        if not await _access(message.from_user.id):
            return
        url = (message.text or "").strip()
        if not url or url.startswith("/"):
            return

        wait = await message.answer("⏳ Загружаю лот FunPay…")
        try:
            lot = await asyncio.wait_for(fetch_funpay_lot(url), timeout=45.0)
        except asyncio.TimeoutError:
            await wait.edit_text("❌ FunPay не ответил вовремя. Попробуйте позже или /cancel")
            return
        except Exception as exc:
            await wait.edit_text(f"❌ Ошибка загрузки: {exc}")
            return

        if lot.errors and not lot.title:
            await wait.edit_text("❌ " + "\n".join(lot.errors))
            return

        if not lot.funpay_node_id:
            await wait.edit_text(
                "❌ Не удалось определить категорию FunPay.\n"
                "Проверьте, что ссылка ведёт на лот, а не на список."
            )
            return

        await wait.edit_text("⏳ Определяю категорию Starvell…")
        match = await _resolve_category(lot)

        await wait.edit_text("⏳ Перевожу описание через Яндекс…")
        await _translate_lot(lot)

        price_hint = parse_price_hint(lot.price_hint) or load_settings().parser_default_price
        lot_payload = {
            "source_url": lot.source_url,
            "offer_id": lot.offer_id,
            "title": lot.title,
            "brief_ru": lot.brief_ru,
            "full_ru": lot.full_ru,
            "brief_en": lot.brief_en,
            "full_en": lot.full_en,
            "funpay_node_id": lot.funpay_node_id,
            "funpay_category_title": lot.funpay_category_title,
            "price_hint": price_hint,
        }

        if match:
            lot_payload.update({
                "category_id": match.category_id,
                "starvell_category_name": f"{match.game_name} → {match.category_name}".strip(" →"),
                "template_offer_id": await _find_template_offer(match.category_id),
            })
        await state.update_data(parsed_lot=lot_payload, price_hint=price_hint, is_smm=lot.is_smm_guess)

        fp_cat = lot.funpay_category_title or f"node {lot.funpay_node_id}"
        sv_cat = lot_payload.get("starvell_category_name") or "не найдена — укажете вручную"
        preview = (
            f"✅ <b>Лот распознан</b>\n\n"
            f"📌 {lot.title}\n"
            f"🆔 FunPay: <code>{lot.offer_id}</code>\n"
            f"📁 FunPay: <b>{fp_cat}</b>\n"
            f"🎯 Starvell: <b>{sv_cat}</b>\n"
            f"💰 Цена FunPay: {lot.price_hint or '—'}\n\n"
            f"<i>{(lot.brief_ru or '')[:100]}…</i>\n\n"
            f"Выберите тип товара:"
        )
        await wait.edit_text(
            preview,
            parse_mode="HTML",
            reply_markup=KB.parser_type_kb(lot.is_smm_guess),
        )

        if not match:
            await state.update_data(pending_category_fallback=True)

    @router.callback_query(F.data == CBT.PARSER_REG)
    async def cb_parser_regular(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await call.answer()
        data = await state.get_data()
        await state.update_data(is_smm=False, service_id=None)
        if data.get("pending_category_fallback") or not data.get("parsed_lot", {}).get("category_id"):
            lot = ParsedLot(
                funpay_node_id=int(data.get("parsed_lot", {}).get("funpay_node_id") or 0),
                funpay_category_title=data.get("parsed_lot", {}).get("funpay_category_title", ""),
                source_url="", offer_id="",
            )
            await _ask_category_fallback(call.message, state, lot)
            return
        await _ask_price(call.message, state)

    @router.callback_query(F.data == CBT.PARSER_SMM)
    async def cb_parser_smm(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await state.update_data(is_smm=True)
        await state.set_state(ParserStates.waiting_smm_id)
        await call.message.edit_text(
            "🚀 <b>SMM-товар</b>\n\n"
            "Старый <code>ID:</code> из FunPay будет удалён.\n"
            "Отправьте <b>ID услуги VexBoost</b> (число):\n\n"
            "Пример: <code>1634</code>",
            parse_mode="HTML",
        )
        await call.answer()

    @router.message(ParserStates.waiting_smm_id)
    async def on_smm_id(message: Message, state: FSMContext) -> None:
        if not await _access(message.from_user.id):
            return
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            return
        m = re.search(r"\d+", raw)
        if not m:
            await message.answer("❌ Отправьте числовой ID услуги")
            return
        await state.update_data(service_id=int(m.group()), is_smm=True)
        data = await state.get_data()
        if data.get("pending_category_fallback") or not data.get("parsed_lot", {}).get("category_id"):
            lot = ParsedLot(
                funpay_node_id=int(data.get("parsed_lot", {}).get("funpay_node_id") or 0),
                funpay_category_title=data.get("parsed_lot", {}).get("funpay_category_title", ""),
                source_url="", offer_id="",
            )
            await _ask_category_fallback(message, state, lot)
            return
        await _ask_price(message, state)

    @router.message(ParserStates.waiting_category_fallback)
    async def on_category_fallback(message: Message, state: FSMContext) -> None:
        if not await _access(message.from_user.id):
            return
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            return
        if not raw.isdigit():
            await message.answer("❌ Отправьте числовой ID категории Starvell")
            return
        category_id = int(raw)
        data = await state.get_data()
        lot_data = dict(data.get("parsed_lot") or {})
        node_id = int(lot_data.get("funpay_node_id") or 0)
        lot_data["category_id"] = category_id
        await state.update_data(parsed_lot=lot_data, pending_category_fallback=False)

        if node_id:
            from services.category_mapper import StarvellCategoryMatch
            match = StarvellCategoryMatch(
                category_id=category_id,
                game_id=0,
                game_name="",
                category_name=str(category_id),
                funpay_node_id=node_id,
                funpay_title=lot_data.get("funpay_category_title", ""),
                confidence=1.0,
                source="manual",
            )
            save_category_mapping(node_id, match)
            s = load_settings()
            s.parser_funpay_category_map[str(node_id)] = {"category_id": category_id}
            save_settings(s)

        await message.answer(f"✅ Категория <code>{category_id}</code> сохранена для FunPay node {node_id}")
        await _ask_price(message, state)

    @router.callback_query(F.data == CBT.PARSER_SKIP_PRICE)
    async def cb_parser_default_price(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        data = await state.get_data()
        lot_data = data.get("parsed_lot") or {}
        category_id = lot_data.get("category_id")
        if not category_id:
            await call.answer("Сначала укажите категорию", show_alert=True)
            return
        price = data.get("price_hint") or load_settings().parser_default_price
        await call.answer()
        await _create_and_reply(
            call.message,
            state,
            category_id=int(category_id),
            price=str(price),
            template_offer_id=lot_data.get("template_offer_id"),
        )

    @router.message(ParserStates.waiting_price)
    async def on_price(message: Message, state: FSMContext) -> None:
        if not await _access(message.from_user.id):
            return
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            return
        price = parse_price_hint(raw) or raw.replace(",", ".")
        if not re.match(r"^\d+(?:\.\d{1,2})?$", price):
            await message.answer("❌ Отправьте цену числом, например <code>15.00</code>", parse_mode="HTML")
            return
        data = await state.get_data()
        lot_data = data.get("parsed_lot") or {}
        category_id = lot_data.get("category_id")
        if not category_id:
            await message.answer("❌ Категория не определена. Начните с /parser")
            return
        await _create_and_reply(
            message,
            state,
            category_id=int(category_id),
            price=price,
            template_offer_id=lot_data.get("template_offer_id"),
        )

    return router
