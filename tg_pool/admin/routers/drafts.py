"""Operator draft cards + Gemini assistant settings (aiogram 3.x)."""

from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from tg_pool.admin.keyboards import (
    _btn,
    account_actions_kb,
    back_home_kb,
    draft_card_kb,
    drafts_settings_kb,
    nav_row,
)
from tg_pool.admin.routers.common import safe_edit
from tg_pool.admin.states import DraftEditStates, GeminiSettingsStates
from tg_pool.admin.texts import account_detail_text, drafts_settings_text, pending_drafts_text
from tg_pool.config import Settings
from tg_pool.db.models import Account, DraftStatus, PendingDraft
from tg_pool.db.session import session_scope
from tg_pool.services.account_service import AccountService
from tg_pool.services.draft_engine import DraftEngine, draft_card_text
from tg_pool.services.draft_service import DraftService
from tg_pool.services.listener_manager import ListenerManager

logger = logging.getLogger(__name__)


def _pending_list_kb(pending: list[PendingDraft]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [_btn(f"#{d.id} {(d.chat_title or str(d.chat_id))[:28]}", f"draft:view:{d.id}")]
        for d in pending
    ]
    rows.append([_btn("⬅️ Настройки", "menu:gemini")])
    rows.append(nav_row(_btn("🏠 Меню", "menu:home")))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_gemini_menu(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        svc = DraftService(session)
        cfg = await svc.get_settings()
        pending = list(await svc.list_pending(limit=15))
        assistant_count = (
            await session.execute(
                select(func.count())
                .select_from(Account)
                .where(Account.assistant_enabled.is_(True))
            )
        ).scalar_one()

    await safe_edit(
        callback,
        drafts_settings_text(
            cfg,
            pending_count=len(pending),
            assistant_accounts=int(assistant_count or 0),
        ),
        drafts_settings_kb(cfg),
    )


def build_drafts_router(
    settings: Settings,
    engine: DraftEngine,
    listeners: ListenerManager | None = None,
) -> Router:
    router = Router(name="drafts")

    @router.callback_query(F.data == "menu:gemini")
    async def cb_gemini_menu(callback: CallbackQuery) -> None:
        await _show_gemini_menu(callback)
        await callback.answer()

    @router.callback_query(F.data == "drafts:pending")
    async def cb_pending_list(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            pending = list(await DraftService(session).list_pending(limit=20))
        if not pending:
            await _show_gemini_menu(callback)
            await callback.answer("Нет pending-черновиков")
            return
        await safe_edit(callback, pending_drafts_text(pending), _pending_list_kb(pending))
        await callback.answer()

    @router.callback_query(F.data.startswith("draft:view:"))
    async def cb_view_draft(callback: CallbackQuery) -> None:
        assert callback.data
        draft_id = int(callback.data.split(":")[-1])
        async with session_scope() as session:
            draft = await DraftService(session).get_draft(draft_id)
            cfg = await DraftService(session).get_settings()
        if draft is None:
            await callback.answer("Не найден", show_alert=True)
            return
        await safe_edit(
            callback,
            draft_card_text(draft, auto_on=bool(cfg.auto_approve_enabled)),
            draft_card_kb(draft.id, auto_on=bool(cfg.auto_approve_enabled)),
        )
        await callback.answer()

    @router.callback_query(F.data == "drafts:toggle_enabled")
    async def cb_toggle_enabled(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            svc = DraftService(session)
            cfg = await svc.get_settings()
            await svc.update_settings(enabled=not cfg.enabled)
        await _show_gemini_menu(callback)
        await callback.answer("Мониторинг переключён")

    @router.callback_query(F.data == "drafts:toggle_auto")
    async def cb_toggle_auto(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            svc = DraftService(session)
            cfg = await svc.get_settings()
            new_val = not cfg.auto_approve_enabled
            await svc.update_settings(auto_approve_enabled=new_val)
        await _show_gemini_menu(callback)
        await callback.answer(
            "⚡️ Авто-режим ВКЛ" if new_val else "Авто-режим ВЫКЛ",
            show_alert=True,
        )

    @router.callback_query(F.data.startswith("draft:auto:"))
    async def cb_card_auto(callback: CallbackQuery) -> None:
        assert callback.data
        draft_id = int(callback.data.split(":")[-1])
        async with session_scope() as session:
            svc = DraftService(session)
            cfg = await svc.get_settings()
            new_val = not cfg.auto_approve_enabled
            await svc.update_settings(auto_approve_enabled=new_val)
            draft = await svc.get_draft(draft_id)
            cfg = await svc.get_settings()
        if draft is None:
            await callback.answer("Черновик не найден", show_alert=True)
            return
        await safe_edit(
            callback,
            draft_card_text(draft, auto_on=bool(cfg.auto_approve_enabled)),
            draft_card_kb(draft.id, auto_on=bool(cfg.auto_approve_enabled)),
        )
        await callback.answer("⚡️ Авто: ON" if new_val else "Авто: OFF")

    @router.callback_query(F.data.startswith("draft:send:"))
    async def cb_send(callback: CallbackQuery) -> None:
        assert callback.data and callback.from_user
        draft_id = int(callback.data.split(":")[-1])
        await callback.answer("Отправка…")
        try:
            sent = await engine.approve_and_send(draft_id, callback.from_user.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Manual send failed")
            await safe_edit(
                callback,
                f"❌ Не удалось отправить черновик #{draft_id}:\n<code>{escape(str(exc))}</code>",
                back_home_kb(),
            )
            return
        await safe_edit(
            callback,
            f"✅ Черновик #{sent.id} отправлен в «{escape(sent.chat_title or str(sent.chat_id))}»",
            back_home_kb(),
        )

    @router.callback_query(F.data.startswith("draft:reject:"))
    async def cb_reject(callback: CallbackQuery) -> None:
        assert callback.data and callback.from_user
        draft_id = int(callback.data.split(":")[-1])
        await engine.reject(draft_id, callback.from_user.id)
        await safe_edit(
            callback,
            f"❌ Черновик #{draft_id} отклонён",
            back_home_kb(),
        )
        await callback.answer("Отклонено")

    @router.callback_query(F.data.startswith("draft:edit:"))
    async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
        assert callback.data
        draft_id = int(callback.data.split(":")[-1])
        async with session_scope() as session:
            draft = await DraftService(session).get_draft(draft_id)
        if draft is None or draft.status != DraftStatus.pending:
            await callback.answer("Недоступен для правки", show_alert=True)
            return
        await state.set_state(DraftEditStates.waiting_text)
        await state.update_data(draft_id=draft_id)
        await safe_edit(
            callback,
            (
                f"✏️ <b>Редактирование черновика #{draft_id}</b>\n\n"
                f"Текущий текст:\n<blockquote>{escape(draft.draft_text)}</blockquote>\n\n"
                "Отправьте новый текст ответа одним сообщением."
            ),
            back_home_kb(),
        )
        await callback.answer()

    @router.message(DraftEditStates.waiting_text)
    async def on_edit_text(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        draft_id = int(data.get("draft_id") or 0)
        await state.clear()
        if not draft_id or not message.text:
            await message.answer("Отмена: пустой ввод.")
            return
        try:
            draft = await engine.update_draft_text(
                draft_id,
                message.text,
                message.from_user.id if message.from_user else 0,
            )
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Ошибка: {escape(str(exc))}", parse_mode="HTML")
            return
        if draft is None:
            await message.answer("Черновик не найден.")
            return
        await message.answer(
            f"✅ Текст черновика #{draft_id} обновлён. Карточка переотправлена операторам.",
            reply_markup=back_home_kb(),
        )

    @router.callback_query(F.data == "drafts:set_key")
    async def cb_set_key(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(GeminiSettingsStates.waiting_api_key)
        await safe_edit(
            callback,
            "🔑 Отправьте Gemini API key одним сообщением.\n"
            "<i>Ключ сохранится в БД (AutoReplySettings).</i>",
            back_home_kb(),
        )
        await callback.answer()

    @router.message(GeminiSettingsStates.waiting_api_key)
    async def on_api_key(message: Message, state: FSMContext) -> None:
        await state.clear()
        key = (message.text or "").strip()
        if not key:
            await message.answer("Пустой ключ — отмена.")
            return
        async with session_scope() as session:
            await DraftService(session).update_settings(gemini_api_key=key)
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            pass
        await message.answer("✅ Gemini API key сохранён.", reply_markup=back_home_kb())

    @router.callback_query(F.data == "drafts:set_promote")
    async def cb_set_promote(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(GeminiSettingsStates.waiting_promote)
        await safe_edit(
            callback,
            "📣 Отправьте username для рекомендации (например <code>@PaskodVPN_bot</code>).",
            back_home_kb(),
        )
        await callback.answer()

    @router.message(GeminiSettingsStates.waiting_promote)
    async def on_promote(message: Message, state: FSMContext) -> None:
        await state.clear()
        raw = (message.text or "").strip()
        if not raw:
            await message.answer("Пусто — отмена.")
            return
        if not raw.startswith("@"):
            raw = f"@{raw}"
        async with session_scope() as session:
            await DraftService(session).update_settings(promote_username=raw[:64])
        await message.answer(
            f"✅ Продвигаем: <code>{escape(raw)}</code>",
            parse_mode="HTML",
            reply_markup=back_home_kb(),
        )

    @router.callback_query(F.data.startswith("drafts:assistant:"))
    async def cb_toggle_assistant(callback: CallbackQuery) -> None:
        assert callback.data
        account_id = int(callback.data.split(":")[-1])
        async with session_scope() as session:
            acc = await session.get(Account, account_id)
            if acc is None:
                await callback.answer("Аккаунт не найден", show_alert=True)
                return
            acc.assistant_enabled = not acc.assistant_enabled
            enabled = acc.assistant_enabled
        if listeners is not None:
            await listeners.refresh_account(account_id)
        await callback.answer("🤖 Assistant ON" if enabled else "Assistant OFF")
        async with session_scope() as session:
            acc = await AccountService(session).get_account(account_id)
        if acc is not None:
            await safe_edit(callback, account_detail_text(acc), account_actions_kb(account_id))

    # silence unused settings warning for future env wiring
    _ = settings
    return router
