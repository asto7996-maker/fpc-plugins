"""Парсер и копирование лотов FunPay → Starvell."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ai_service import AIService
from config import load_settings
from keyboards import cbt as CBT
from services.funpay_parser import (
    build_starvell_package,
    fetch_funpay_lot,
    send_copy_sections,
)
from tg_bot import keyboards as KB


class ParserStates(StatesGroup):
    waiting_url = State()
    waiting_smm_id = State()


def create_lot_parser_router(ctx: Any) -> Router:
    router = Router(name="lot_parser")

    async def _access(user_id: int) -> bool:
        return await ctx._has_access(user_id)

    async def _parser_intro() -> str:
        return (
            "🔍 <b>Парсер лотов FunPay</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Отправьте ссылку на лот FunPay — бот скопирует:\n"
            "• краткое и подробное описание (RU)\n"
            "• перевод на английский (Gemini)\n"
            "• для SMM — сообщение после оплаты и <code>ID: ваш_id</code>\n\n"
            "Пример ссылки:\n"
            "<code>https://funpay.com/lots/offer?id=12345678</code>\n\n"
            "<i>Для выхода: /cancel или ◀️ Меню</i>"
        )

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
        if not url:
            return
        if url.startswith("/"):
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

        s = load_settings()
        ai = AIService(s)
        if s.is_gemini_configured():
            await wait.edit_text("⏳ Перевожу описание на английский…")
            try:
                lot.brief_en, lot.full_en = await asyncio.wait_for(
                    asyncio.gather(
                        ai.translate_text(lot.brief_ru or lot.title),
                        ai.translate_text(lot.full_ru or lot.brief_ru or lot.title),
                    ),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                lot.brief_en = lot.brief_ru
                lot.full_en = lot.full_ru
                await wait.edit_text("⚠️ Перевод занял слишком долго — показываю только RU…")
        else:
            lot.brief_en = lot.brief_ru
            lot.full_en = lot.full_ru

        await state.update_data(parsed_lot={
            "source_url": lot.source_url,
            "offer_id": lot.offer_id,
            "title": lot.title,
            "brief_ru": lot.brief_ru,
            "full_ru": lot.full_ru,
            "brief_en": lot.brief_en,
            "full_en": lot.full_en,
            "is_smm_guess": lot.is_smm_guess,
        })
        preview = (
            f"✅ <b>Лот распознан</b>\n\n"
            f"📌 {lot.title}\n"
            f"🆔 FunPay ID: <code>{lot.offer_id}</code>\n\n"
            f"<i>{(lot.brief_ru or '')[:120]}…</i>\n\n"
            f"Выберите тип товара:"
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
        data = await state.get_data()
        lot_data = data.get("parsed_lot") or {}
        from services.funpay_parser import ParsedLot
        lot = ParsedLot(
            source_url=lot_data.get("source_url", ""),
            offer_id=lot_data.get("offer_id", ""),
            title=lot_data.get("title", ""),
            brief_ru=lot_data.get("brief_ru", ""),
            full_ru=lot_data.get("full_ru", ""),
            brief_en=lot_data.get("brief_en", ""),
            full_en=lot_data.get("full_en", ""),
        )
        sections = build_starvell_package(lot, is_smm=False)
        await state.clear()
        await call.answer()
        try:
            from services.funpay_parser import format_copy_message
            preview = format_copy_message(sections)
            if len(preview) <= 4000:
                await call.message.edit_text(preview, parse_mode="HTML", reply_markup=KB.back_menu())
            else:
                await call.message.edit_text("✅ Лот готов — отправляю частями…", reply_markup=KB.back_menu())
                await send_copy_sections(call.message, sections)
        except Exception:
            await call.message.edit_text("✅ Лот готов — отправляю частями…", reply_markup=KB.back_menu())
            await send_copy_sections(call.message, sections)

    @router.callback_query(F.data == CBT.PARSER_SMM)
    async def cb_parser_smm(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        await state.set_state(ParserStates.waiting_smm_id)
        await call.message.edit_text(
            "🚀 <b>SMM-товар</b>\n\n"
            "Старый <code>ID:</code> из описания FunPay будет удалён.\n"
            "Отправьте <b>ID услуги VexBoost</b> (только число):\n\n"
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
        service_id = int(m.group())
        await state.update_data(service_id=service_id)
        await state.set_state(None)
        await message.answer(
            "Включить подсказку про <b>автовыдачу</b> на Starvell?",
            parse_mode="HTML",
            reply_markup=KB.parser_autodelivery_kb(),
        )

    @router.callback_query(F.data.in_({CBT.PARSER_AD_ON, CBT.PARSER_AD_OFF}))
    async def cb_parser_finalize(call: CallbackQuery, state: FSMContext) -> None:
        if not await _access(call.from_user.id):
            return
        data = await state.get_data()
        lot_data = data.get("parsed_lot") or {}
        service_id = int(data.get("service_id") or 0)
        auto_ad = call.data == CBT.PARSER_AD_ON
        from services.funpay_parser import ParsedLot
        lot = ParsedLot(
            source_url=lot_data.get("source_url", ""),
            offer_id=lot_data.get("offer_id", ""),
            title=lot_data.get("title", ""),
            brief_ru=lot_data.get("brief_ru", ""),
            full_ru=lot_data.get("full_ru", ""),
            brief_en=lot_data.get("brief_en", ""),
            full_en=lot_data.get("full_en", ""),
        )
        sections = build_starvell_package(
            lot, is_smm=True, service_id=service_id, auto_delivery=auto_ad,
        )
        await state.clear()
        await call.answer("✅ Готово")
        try:
            from services.funpay_parser import format_copy_message
            preview = format_copy_message(sections)
            if len(preview) <= 4000:
                await call.message.edit_text(preview, parse_mode="HTML", reply_markup=KB.back_menu())
            else:
                await call.message.edit_text("✅ Лот готов — отправляю частями…", reply_markup=KB.back_menu())
                await send_copy_sections(call.message, sections)
        except Exception:
            await call.message.edit_text("✅ Лот готов — отправляю частями…", reply_markup=KB.back_menu())
            await send_copy_sections(call.message, sections)

    return router
