"""Main menu, help, profile, stats navigation."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from tg_pool.admin.keyboards import (
    main_menu_kb,
    profile_kb,
    reply_menu_kb,
    stats_kb,
)
from tg_pool.admin.nav import (
    REPLY_ACCOUNTS,
    REPLY_ADD,
    REPLY_ADMIN,
    REPLY_GEMINI,
    REPLY_HELP,
    REPLY_HOME,
    REPLY_PROFILE,
    REPLY_STATS,
)
from tg_pool.admin.routers.common import clear_state, safe_edit, show_main_menu
from tg_pool.admin.texts import help_text, main_menu_text, profile_text, stats_text
from tg_pool.config import Settings
from tg_pool.db.models import Account, AccountStatus, Proxy
from tg_pool.db.session import session_scope


def build_menu_router(settings: Settings) -> Router:
    router = Router(name="menu")

    async def _show_menu(message: Message, is_creator: bool, state: FSMContext | None) -> None:
        await clear_state(state)
        await message.answer(
            main_menu_text(),
            parse_mode="HTML",
            reply_markup=reply_menu_kb(is_creator=is_creator),
        )
        await message.answer(
            "Выберите раздел:",
            parse_mode="HTML",
            reply_markup=main_menu_kb(is_creator=is_creator),
        )

    @router.message(Command("menu"))
    async def cmd_menu(
        message: Message,
        state: FSMContext,
        is_creator: bool = False,
    ) -> None:
        await _show_menu(message, is_creator, state)

    @router.message(F.text.in_(REPLY_HOME))
    async def reply_home(
        message: Message,
        state: FSMContext,
        is_creator: bool = False,
    ) -> None:
        await _show_menu(message, is_creator, state)

    @router.message(F.text.in_(REPLY_HELP))
    @router.message(Command("help"))
    async def cmd_help(message: Message, state: FSMContext) -> None:
        from tg_pool.admin.keyboards import back_home_kb

        await clear_state(state)
        await message.answer(help_text(), parse_mode="HTML", reply_markup=back_home_kb())

    @router.message(F.text.in_(REPLY_PROFILE))
    @router.message(Command("profile"))
    async def cmd_profile(
        message: Message,
        state: FSMContext,
        panel_user=None,
        is_creator: bool = False,
    ) -> None:
        await clear_state(state)
        if panel_user is None:
            await message.answer("Профиль недоступен.")
            return
        await message.answer(
            profile_text(panel_user),
            parse_mode="HTML",
            reply_markup=profile_kb(is_creator=is_creator),
        )

    @router.message(F.text.in_(REPLY_ACCOUNTS))
    async def reply_accounts(message: Message, state: FSMContext) -> None:
        from tg_pool.admin.keyboards import accounts_kb
        from tg_pool.admin.texts import accounts_list_text
        from tg_pool.services.account_service import AccountService

        await clear_state(state)
        async with session_scope() as session:
            accounts = list(await AccountService(session).list_accounts())
        await message.answer(
            accounts_list_text(accounts),
            parse_mode="HTML",
            reply_markup=accounts_kb(accounts),
        )

    @router.message(F.text.in_(REPLY_ADD))
    async def reply_add(message: Message, state: FSMContext) -> None:
        from tg_pool.admin.keyboards import add_account_kb

        await clear_state(state)
        await message.answer(
            "➕ <b>Добавить аккаунт</b>\n\nВыберите способ импорта:",
            parse_mode="HTML",
            reply_markup=add_account_kb(),
        )

    @router.message(F.text.in_(REPLY_STATS))
    async def reply_stats(message: Message, state: FSMContext) -> None:
        await clear_state(state)
        text = await _stats_text()
        await message.answer(text, parse_mode="HTML", reply_markup=stats_kb())

    @router.message(F.text.in_(REPLY_GEMINI))
    async def reply_gemini(message: Message, state: FSMContext) -> None:
        from tg_pool.admin.keyboards import drafts_settings_kb
        from tg_pool.admin.texts import drafts_settings_text
        from tg_pool.services.draft_service import DraftService

        await clear_state(state)
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
        await message.answer(
            drafts_settings_text(
                cfg,
                pending_count=len(pending),
                assistant_accounts=int(assistant_count or 0),
            ),
            parse_mode="HTML",
            reply_markup=drafts_settings_kb(cfg),
        )

    @router.message(F.text.in_(REPLY_ADMIN))
    async def reply_admin(
        message: Message,
        state: FSMContext,
        is_creator: bool = False,
    ) -> None:
        await clear_state(state)
        if not is_creator and (
            message.from_user is None or message.from_user.id != settings.creator_id
        ):
            await message.answer("🔒 Только для суперадминистратора.")
            return
        from tg_pool.admin.keyboards import access_kb
        from tg_pool.admin.texts import admin_panel_text

        await message.answer(
            admin_panel_text(),
            parse_mode="HTML",
            reply_markup=access_kb(),
        )

    async def _stats_text() -> str:
        async with session_scope() as session:
            total = (
                await session.execute(select(func.count()).select_from(Account))
            ).scalar_one()
            counts = {}
            for st in AccountStatus:
                counts[st] = (
                    await session.execute(
                        select(func.count())
                        .select_from(Account)
                        .where(Account.status == st)
                    )
                ).scalar_one()
            actions = (
                await session.execute(
                    select(func.coalesce(func.sum(Account.total_actions_today), 0))
                )
            ).scalar_one()
            proxies = (
                await session.execute(select(func.count()).select_from(Proxy))
            ).scalar_one()
        return stats_text(
            total=int(total),
            active=int(counts.get(AccountStatus.active, 0)),
            flood=int(counts.get(AccountStatus.flood_wait, 0)),
            banned=int(counts.get(AccountStatus.banned, 0)),
            paused=int(counts.get(AccountStatus.paused, 0)),
            spambot=int(counts.get(AccountStatus.spambot, 0)),
            actions_today=int(actions or 0),
            proxies=int(proxies),
        )

    @router.callback_query(F.data == "menu:home")
    @router.callback_query(F.data == "nav:back")
    @router.callback_query(F.data == "nav:refresh")
    async def cb_home(
        callback: CallbackQuery,
        state: FSMContext,
        is_creator: bool = False,
    ) -> None:
        await clear_state(state)
        await show_main_menu(callback, is_creator=is_creator, edit=True)

    @router.callback_query(F.data == "menu:help")
    async def cb_help(callback: CallbackQuery, state: FSMContext) -> None:
        from tg_pool.admin.keyboards import back_home_kb

        await clear_state(state)
        await safe_edit(callback, help_text(), back_home_kb())
        await callback.answer()

    @router.callback_query(F.data == "menu:profile")
    async def cb_profile(
        callback: CallbackQuery,
        state: FSMContext,
        panel_user=None,
        is_creator: bool = False,
    ) -> None:
        await clear_state(state)
        if panel_user is None:
            await callback.answer("Нет профиля", show_alert=True)
            return
        await safe_edit(
            callback,
            profile_text(panel_user),
            profile_kb(is_creator=is_creator),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:stats")
    async def cb_stats(callback: CallbackQuery, state: FSMContext) -> None:
        await clear_state(state)
        await safe_edit(callback, await _stats_text(), stats_kb())
        await callback.answer()

    return router
