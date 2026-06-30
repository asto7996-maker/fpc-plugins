"""Парсер FunPay → автосоздание лота на Starvell."""

from __future__ import annotations

import asyncio
import logging
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
from services.price_utils import (
    format_price_display,
    normalize_price_input,
    parse_parser_message,
    parse_price_hint,
    parse_smm_reply,
)
from services.starvell_lot_creator import create_lot_from_parsed, format_created_message
from services.yandex_translate import translate_ru_to_en
from starvell_api import StarvellAPIError
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
            "Отправьте ссылку на лот FunPay — бот сам:\n"
            "• определит категорию Starvell (как на FunPay)\n"
            "• скопирует описание + перевод (Яндекс)\n"
            "• подставит цену с FunPay (в т.ч. <code>0.001</code>)\n"
            "• создаст лот (999999 шт., автодоставка)\n\n"
            "<b>Быстрый ввод (SMM):</b>\n"
            "<code>https://funpay.com/lots/offer?id=…</code>\n"
            "<code>1634 0.001</code>\n\n"
            "Или одной строкой:\n"
            "<code>https://funpay.com/… 1634 0.001</code>\n\n"
            "<i>/cancel — выход</i>"
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
            lot.brief_en, lot.full_en = lot.brief_ru, lot.full_ru
        except Exception as exc:
            logger.warning("Yandex translate: %s", exc)
            lot.brief_en, lot.full_en = lot.brief_ru, lot.full_ru

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

    def _resolve_price(lot: ParsedLot, override: str | None = None) -> str:
        s = load_settings()
        for candidate in (override, parse_price_hint(lot.price_hint), s.parser_default_price):
            if not candidate:
                continue
            normalized = normalize_price_input(str(candidate))
            if normalized:
                return normalized
        return normalize_price_input(s.parser_default_price) or "0.01"

    async def _build_lot_payload(
        lot: ParsedLot,
        match,
        *,
        price_override: str | None = None,
    ) -> dict:
        price_hint = _resolve_price(lot, price_override)
        payload = {
            "source_url": lot.source_url,
            "offer_id": lot.offer_id,
            "title": lot.title,
            "brief_ru": lot.brief_ru,
            "full_ru": lot.full_ru,
            "brief_en": lot.brief_en,
            "full_en": lot.full_en,
            "funpay_node_id": lot.funpay_node_id,
            "funpay_category_title": lot.funpay_category_title,
            "funpay_service_type": lot.funpay_service_type,
            "price_hint": price_hint,
        }
        if match:
            payload.update({
                "category_id": match.category_id,
                "game_id": match.game_id,
                "game_slug": match.game_slug,
                "category_slug": match.category_slug,
                "starvell_category_name": f"{match.game_name} → {match.category_name}".strip(" →"),
                "template_offer_id": await _find_template_offer(match.category_id),
            })
        return payload

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

        normalized = normalize_price_input(price)
        if not normalized:
            await message.answer(
                "❌ Некорректная цена. Пример: <code>0.001</code>, <code>0.05</code>, <code>10</code>",
                parse_mode="HTML",
            )
            return

        if not api or not s.is_starvell_configured():
            await message.answer(
                "❌ Starvell не настроен. Укажите session cookie в профиле.",
                reply_markup=KB.back_menu(),
            )
            await state.clear()
            return

        if template_offer_id is None:
            template_offer_id = lot_data.get("template_offer_id")
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
            funpay_service_type=lot_data.get("funpay_service_type", ""),
        )

        wait = await message.answer("⏳ Создаю лот на Starvell…")
        try:
            result = await asyncio.wait_for(
                create_lot_from_parsed(
                    api,
                    lot,
                    is_smm=is_smm,
                    service_id=service_id if is_smm else None,
                    price=normalized,
                    category_id=category_id,
                    game_id=int(lot_data.get("game_id") or 0),
                    game_slug=str(lot_data.get("game_slug") or ""),
                    category_slug=str(lot_data.get("category_slug") or ""),
                    template_offer_id=template_offer_id,
                    auto_delivery=s.parser_auto_delivery,
                ),
                timeout=60.0,
            )
        except StarvellAPIError as exc:
            logger.warning("create_offer api error: %s", exc)
            detail = str(exc)
            if "build=attrs-v6" not in detail and "build=attrs-v5" not in detail and "build=attrs-v4" not in detail:
                detail = (
                    f"{detail}\n\n⚠️ На сервере старый код парсера. "
                    "Обновите: curl -fsSL …/update_starvell_cardinal.sh | sudo bash -s -- cursor/parser-auto-create-6ec3"
                )
            if exc.body:
                extra = exc.body.get("message") or exc.body.get("errors")
                if extra and str(extra) not in detail:
                    detail = f"{detail}\n{extra}"
            await wait.edit_text(f"❌ Starvell отклонил лот:\n<code>{detail}</code>", parse_mode="HTML")
            return
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
            price=normalized,
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
        display = format_price_display(price)
        await message.answer(
            f"💰 Отправьте <b>цену</b> за 1 шт. (от <code>0.000001</code> ₽)\n"
            f"Примеры: <code>0.001</code> · <code>0.039</code> · <code>10</code>\n\n"
            f"Цена FunPay: <code>{display}</code> ₽",
            parse_mode="HTML",
            reply_markup=KB.parser_price_kb(display),
        )
        await state.set_state(ParserStates.waiting_price)

    async def _ask_smm_id(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        price = format_price_display(data.get("price_hint") or "0.01")
        await message.answer(
            "🚀 <b>SMM-товар</b>\n\n"
            "Отправьте <b>ID VexBoost</b> или сразу с ценой:\n"
            f"• <code>1634</code>\n"
            f"• <code>1634 {price}</code>\n"
            f"• <code>1634 0.001</code>",
            parse_mode="HTML",
            reply_markup=KB.parser_smm_kb(price),
        )
        await state.set_state(ParserStates.waiting_smm_id)

    async def _ask_category_fallback(message: Message, state: FSMContext, lot: ParsedLot) -> None:
        await message.answer(
            "⚠️ <b>Категория Starvell не найдена автоматически</b>\n\n"
            f"FunPay: <b>{lot.funpay_category_title or '—'}</b>\n"
            f"Node: <code>{lot.funpay_node_id or '—'}</code>\n\n"
            "Отправьте <b>ID категории Starvell</b> — бот запомнит.",
            parse_mode="HTML",
        )
        await state.set_state(ParserStates.waiting_category_fallback)

    async def _try_fast_create(
        message: Message,
        state: FSMContext,
        *,
        service_id: int,
        price: str | None,
        is_smm: bool = True,
    ) -> bool:
        data = await state.get_data()
        lot_data = data.get("parsed_lot") or {}
        category_id = lot_data.get("category_id")
        if not category_id:
            await state.update_data(service_id=service_id, is_smm=is_smm, pending_category_fallback=True)
            return False
        final_price = price or lot_data.get("price_hint") or load_settings().parser_default_price
        await state.update_data(service_id=service_id, is_smm=is_smm)
        await _create_and_reply(
            message,
            state,
            category_id=int(category_id),
            price=str(final_price),
            template_offer_id=lot_data.get("template_offer_id"),
        )
        return True

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
        raw = (message.text or "").strip()
        if not raw or raw.startswith("/"):
            return

        url, preset_service_id, preset_price = parse_parser_message(raw)
        if not url:
            return

        wait = await message.answer("⏳ Загружаю лот FunPay…")
        try:
            lot = await asyncio.wait_for(fetch_funpay_lot(url), timeout=45.0)
        except asyncio.TimeoutError:
            await wait.edit_text("❌ FunPay не ответил вовремя. /cancel")
            return
        except Exception as exc:
            await wait.edit_text(f"❌ Ошибка загрузки: {exc}")
            return

        if lot.errors and not lot.title:
            await wait.edit_text("❌ " + "\n".join(lot.errors))
            return
        if not lot.funpay_node_id:
            await wait.edit_text("❌ Не удалось определить категорию FunPay.")
            return

        await wait.edit_text("⏳ Категория + перевод…")
        match, _ = await asyncio.gather(
            _resolve_category(lot),
            _translate_lot(lot),
        )

        price_hint = _resolve_price(lot, preset_price)
        lot_payload = await _build_lot_payload(lot, match, price_override=preset_price)
        await state.update_data(
            parsed_lot=lot_payload,
            price_hint=price_hint,
            is_smm=lot.is_smm_guess,
            funpay_category_title=lot.funpay_category_title,
            starvell_category_name=lot_payload.get("starvell_category_name"),
            pending_category_fallback=not bool(match),
        )

        if preset_service_id and match:
            if await _try_fast_create(
                wait,
                state,
                service_id=preset_service_id,
                price=preset_price or price_hint,
                is_smm=True,
            ):
                return

        fp_cat = lot.funpay_category_title or f"node {lot.funpay_node_id}"
        sv_cat = lot_payload.get("starvell_category_name") or "— укажете вручную"
        price_display = format_price_display(price_hint)
        preview = (
            f"✅ <b>Лот готов</b>\n\n"
            f"📌 {(lot.title or '')[:90]}\n"
            f"📁 FunPay: <b>{fp_cat}</b>\n"
            f"🎯 Starvell: <b>{sv_cat}</b>\n"
            f"💰 Цена FunPay: <code>{price_display}</code> ₽\n\n"
            f"<i>{(lot.brief_ru or '')[:90]}…</i>"
        )
        if lot.is_smm_guess:
            preview += "\n\nВыберите SMM или отправьте <code>ID цена</code>:"
        else:
            preview += "\n\nВыберите тип:"

        await wait.edit_text(
            preview,
            parse_mode="HTML",
            reply_markup=KB.parser_preview_kb(
                lot.is_smm_guess,
                has_category=bool(match),
                price=price_display,
            ),
        )

    @router.callback_query(F.data == CBT.PARSER_QUICK_CREATE)
    async def cb_quick_smm(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        data = await state.get_data()
        if not data.get("parsed_lot", {}).get("category_id"):
            await call.answer("Сначала укажите категорию Starvell", show_alert=True)
            return
        await state.update_data(is_smm=True)
        await call.answer()
        await _ask_smm_id(call.message, state)

    @router.callback_query(F.data == CBT.PARSER_REG)
    async def cb_parser_regular(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await call.answer()
        data = await state.get_data()
        await state.update_data(is_smm=False, service_id=None)
        if data.get("pending_category_fallback"):
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
        await call.answer()
        data = await state.get_data()
        if data.get("pending_category_fallback"):
            lot = ParsedLot(
                funpay_node_id=int(data.get("parsed_lot", {}).get("funpay_node_id") or 0),
                funpay_category_title=data.get("parsed_lot", {}).get("funpay_category_title", ""),
                source_url="", offer_id="",
            )
            await _ask_category_fallback(call.message, state, lot)
            return
        await _ask_smm_id(call.message, state)

    @router.message(ParserStates.waiting_smm_id)
    async def on_smm_id(message: Message, state: FSMContext) -> None:
        if not await _access(message.from_user.id):
            return
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            return
        service_id, price = parse_smm_reply(raw)
        if not service_id:
            await message.answer(
                "❌ Укажите ID VexBoost, например <code>1634</code> или <code>1634 0.001</code>",
                parse_mode="HTML",
            )
            return

        data = await state.get_data()
        if data.get("pending_category_fallback"):
            await state.update_data(service_id=service_id, is_smm=True)
            lot = ParsedLot(
                funpay_node_id=int(data.get("parsed_lot", {}).get("funpay_node_id") or 0),
                funpay_category_title=data.get("parsed_lot", {}).get("funpay_category_title", ""),
                source_url="", offer_id="",
            )
            await _ask_category_fallback(message, state, lot)
            return

        if price:
            await _try_fast_create(message, state, service_id=service_id, price=price, is_smm=True)
            return

        await state.update_data(service_id=service_id, is_smm=True)
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
        lot_data["template_offer_id"] = await _find_template_offer(category_id)
        await state.update_data(
            parsed_lot=lot_data,
            pending_category_fallback=False,
            starvell_category_name=str(category_id),
        )

        if node_id:
            from services.category_mapper import StarvellCategoryMatch
            save_category_mapping(node_id, StarvellCategoryMatch(
                category_id=category_id,
                game_id=0,
                game_name="",
                category_name=str(category_id),
                funpay_node_id=node_id,
                funpay_title=lot_data.get("funpay_category_title", ""),
                confidence=1.0,
                source="manual",
            ))
            s = load_settings()
            s.parser_funpay_category_map[str(node_id)] = {"category_id": category_id}
            save_settings(s)

        service_id = int(data.get("service_id") or 0)
        if service_id and data.get("is_smm"):
            await message.answer(f"✅ Категория <code>{category_id}</code> сохранена")
            await _try_fast_create(message, state, service_id=service_id, price=None, is_smm=True)
            return

        await message.answer(f"✅ Категория <code>{category_id}</code> сохранена")
        if data.get("is_smm"):
            await _ask_smm_id(message, state)
        else:
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
        if data.get("is_smm") and not data.get("service_id"):
            await call.answer("Сначала укажите ID VexBoost", show_alert=True)
            await _ask_smm_id(call.message, state)
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
        price = normalize_price_input(raw)
        if not price:
            await message.answer(
                "❌ Некорректная цена.\nПримеры: <code>0.001</code> · <code>0.039</code> · <code>15.5</code>",
                parse_mode="HTML",
            )
            return
        data = await state.get_data()
        lot_data = data.get("parsed_lot") or {}
        category_id = lot_data.get("category_id")
        if not category_id:
            await message.answer("❌ Категория не определена. /parser")
            return
        await _create_and_reply(
            message,
            state,
            category_id=int(category_id),
            price=price,
            template_offer_id=lot_data.get("template_offer_id"),
        )

    return router
