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

from config import load_settings
from keyboards import cbt as CBT
from services.funpay_parser import ParsedLot, fetch_funpay_lot
from services.starvell_lot_creator import create_lot_from_parsed, format_created_message
from services.yandex_translate import parse_price_hint, translate_ru_to_en
from tg_bot import keyboards as KB

logger = logging.getLogger("starvell.lot_parser")


class ParserStates(StatesGroup):
    waiting_url = State()
    waiting_smm_id = State()
    waiting_category_id = State()
    waiting_price = State()


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
            "• скопирует краткое и подробное описание\n"
            "• переведёт на английский (Яндекс.Переводчик)\n"
            "• создаст лот на Starvell (наличие 999999)\n"
            "• для SMM — сообщение после оплаты и <code>ID:</code>\n"
            "• включит автоматизированную доставку\n\n"
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

    async def _load_categories(api) -> list[dict]:
        try:
            return await asyncio.wait_for(api.fetch_seller_categories(), timeout=20.0)
        except Exception as exc:
            logger.warning("fetch_seller_categories: %s", exc)
            return []

    async def _ask_category(message: Message, state: FSMContext, *, is_smm: bool) -> None:
        s = load_settings()
        api = _api()
        categories: list[dict] = []
        if api and s.is_starvell_configured():
            categories = await _load_categories(api)

        await state.update_data(is_smm=is_smm)
        if categories:
            default_price = parse_price_hint(
                (await state.get_data()).get("price_hint", "")
            ) or s.parser_default_price
            await message.answer(
                "📁 <b>Выберите категорию Starvell</b>\n"
                "Будут скопированы фильтры из вашего лота в этой категории.\n\n"
                "Или отправьте <code>category_id цена</code>\n"
                f"Пример: <code>{categories[0]['category_id']} {default_price}</code>",
                parse_mode="HTML",
                reply_markup=KB.parser_categories_kb(categories),
            )
            await state.set_state(ParserStates.waiting_category_id)
            return

        await message.answer(
            "📁 Отправьте <b>ID категории Starvell</b> и цену через пробел:\n"
            f"Пример: <code>{s.parser_default_category_id or 1} {s.parser_default_price}</code>",
            parse_mode="HTML",
        )
        await state.set_state(ParserStates.waiting_category_id)

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

        lot = ParsedLot(
            source_url=lot_data.get("source_url", ""),
            offer_id=lot_data.get("offer_id", ""),
            title=lot_data.get("title", ""),
            brief_ru=lot_data.get("brief_ru", ""),
            full_ru=lot_data.get("full_ru", ""),
            brief_en=lot_data.get("brief_en", ""),
            full_en=lot_data.get("full_en", ""),
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
        text = format_created_message(
            title=lot.title,
            url=result.get("url", ""),
            price=price,
            category_id=category_id,
            is_smm=is_smm,
            service_id=service_id,
        )
        await wait.edit_text(text, parse_mode="HTML", reply_markup=KB.back_menu(), disable_web_page_preview=False)

    def _parse_category_price(text: str) -> tuple[int | None, str | None]:
        s = load_settings()
        parts = (text or "").strip().split()
        if not parts:
            return None, None
        cat_id: int | None = None
        price: str | None = None
        if parts[0].isdigit():
            cat_id = int(parts[0])
        if len(parts) >= 2:
            price = parse_price_hint(parts[1]) or parts[1].replace(",", ".")
        elif len(parts) == 1 and not parts[0].isdigit():
            price = parse_price_hint(parts[0])
        if cat_id and not price:
            price = s.parser_default_price
        return cat_id, price

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
            lot = await asyncio.wait_for(fetch_funpay_lot(url), timeout=35.0)
        except asyncio.TimeoutError:
            await wait.edit_text("❌ FunPay не ответил вовремя. Попробуйте позже или /cancel")
            return
        except Exception as exc:
            await wait.edit_text(f"❌ Ошибка загрузки: {exc}")
            return

        if lot.errors and not lot.title:
            await wait.edit_text("❌ " + "\n".join(lot.errors))
            return

        await wait.edit_text("⏳ Перевожу описание через Яндекс…")
        await _translate_lot(lot)

        price_hint = parse_price_hint(lot.price_hint) or load_settings().parser_default_price
        await state.update_data(parsed_lot={
            "source_url": lot.source_url,
            "offer_id": lot.offer_id,
            "title": lot.title,
            "brief_ru": lot.brief_ru,
            "full_ru": lot.full_ru,
            "brief_en": lot.brief_en,
            "full_en": lot.full_en,
        }, price_hint=price_hint)

        preview = (
            f"✅ <b>Лот распознан</b>\n\n"
            f"📌 {lot.title}\n"
            f"🆔 FunPay: <code>{lot.offer_id}</code>\n"
            f"💰 Цена FunPay: {lot.price_hint or '—'}\n\n"
            f"<i>{(lot.brief_ru or '')[:100]}…</i>\n\n"
            f"Выберите тип — бот создаст лот на Starvell:"
        )
        await wait.edit_text(
            preview,
            parse_mode="HTML",
            reply_markup=KB.parser_type_kb(lot.is_smm_guess),
        )

    @router.callback_query(F.data == CBT.PARSER_REG)
    async def cb_parser_regular(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await call.answer()
        await _ask_category(call.message, state, is_smm=False)

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
        await _ask_category(message, state, is_smm=True)

    @router.callback_query(F.data.startswith(CBT.PARSER_CAT))
    async def cb_parser_category(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        cat_raw = call.data.replace(CBT.PARSER_CAT, "", 1)
        if not cat_raw.isdigit():
            await call.answer("Неверная категория", show_alert=True)
            return
        category_id = int(cat_raw)
        data = await state.get_data()
        price = data.get("price_hint") or load_settings().parser_default_price

        api = _api()
        template_offer_id = None
        if api:
            for cat in await _load_categories(api):
                if cat.get("category_id") == category_id:
                    template_offer_id = cat.get("offer_id")
                    if cat.get("price"):
                        hint = parse_price_hint(str(cat.get("price")))
                        if hint:
                            price = hint
                    break

        await state.update_data(category_id=category_id, template_offer_id=template_offer_id)
        await call.answer()
        await call.message.edit_text(
            f"📁 Категория: <code>{category_id}</code>\n\n"
            f"Отправьте <b>цену</b> (₽ за 1 шт.) или нажмите кнопку ниже.\n"
            f"Подсказка: <code>{price}</code>",
            parse_mode="HTML",
            reply_markup=KB.parser_price_kb(price),
        )
        await state.set_state(ParserStates.waiting_price)

    @router.callback_query(F.data == CBT.PARSER_SKIP_PRICE)
    async def cb_parser_default_price(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        data = await state.get_data()
        category_id = data.get("category_id")
        if not category_id:
            await call.answer("Сначала выберите категорию", show_alert=True)
            return
        price = data.get("price_hint") or load_settings().parser_default_price
        await call.answer()
        await _create_and_reply(
            call.message,
            state,
            category_id=int(category_id),
            price=str(price),
            template_offer_id=data.get("template_offer_id"),
        )

    @router.message(ParserStates.waiting_category_id)
    async def on_category_text(message: Message, state: FSMContext) -> None:
        if not await _access(message.from_user.id):
            return
        raw = (message.text or "").strip()
        if raw.startswith("/"):
            return
        cat_id, price = _parse_category_price(raw)
        if not cat_id:
            await message.answer("❌ Укажите числовой ID категории")
            return
        if not price:
            await state.update_data(category_id=cat_id)
            await state.set_state(ParserStates.waiting_price)
            await message.answer(
                f"📁 Категория: <code>{cat_id}</code>\n\nОтправьте цену (₽):",
                parse_mode="HTML",
            )
            return
        await _create_and_reply(message, state, category_id=cat_id, price=price)

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
        category_id = data.get("category_id")
        if not category_id:
            await message.answer("❌ Категория не выбрана. Начните с /parser")
            return
        await _create_and_reply(
            message,
            state,
            category_id=int(category_id),
            price=price,
            template_offer_id=data.get("template_offer_id"),
        )

    # Legacy callbacks — сразу создаём с автодоставкой
    @router.callback_query(F.data.in_({CBT.PARSER_AD_ON, CBT.PARSER_AD_OFF}))
    async def cb_parser_legacy_autodelivery(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await call.answer("Выберите категорию для создания лота")
        await _ask_category(call.message, state, is_smm=bool((await state.get_data()).get("is_smm")))

    return router
