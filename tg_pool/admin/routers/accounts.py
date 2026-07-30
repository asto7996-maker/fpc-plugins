"""Accounts list and per-account actions."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from tg_pool.admin.keyboards import account_actions_kb, accounts_kb
from tg_pool.admin.routers.common import safe_edit
from tg_pool.admin.texts import account_detail_text, accounts_list_text
from tg_pool.config import Settings
from tg_pool.db.models import AccountStatus
from tg_pool.db.session import session_scope
from tg_pool.taskqueue.broker import PoolTask, RedisTaskBroker
from tg_pool.services.account_service import AccountService


def build_accounts_router(
    settings: Settings,
    broker: RedisTaskBroker,
    listeners=None,
) -> Router:
    router = Router(name="accounts")

    @router.callback_query(F.data == "menu:accounts")
    async def cb_accounts(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            accounts = list(await AccountService(session).list_accounts())
        await safe_edit(
            callback,
            accounts_list_text(accounts),
            accounts_kb(accounts),
        )
        await callback.answer()

    async def _show_account_detail(callback: CallbackQuery, account_id: int) -> None:
        async with session_scope() as session:
            acc = await AccountService(session).get_account(account_id)
        if acc is None:
            await callback.answer("Не найден", show_alert=True)
            return
        await safe_edit(
            callback,
            account_detail_text(acc),
            account_actions_kb(account_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("acc:"))
    async def cb_account_detail(callback: CallbackQuery) -> None:
        assert callback.data is not None
        account_id = int(callback.data.split(":")[1])
        await _show_account_detail(callback, account_id)

    @router.callback_query(F.data.startswith("accact:"))
    async def cb_account_action(callback: CallbackQuery) -> None:
        assert callback.data is not None
        _, account_id_s, action = callback.data.split(":", 2)
        account_id = int(account_id_s)

        if action == "activate":
            async with session_scope() as session:
                await AccountService(session).set_status(
                    account_id,
                    AccountStatus.active,
                    last_error=None,
                    flood_until=None,
                )
            if listeners is not None:
                await listeners.refresh_account(account_id)
            await callback.answer("▶️ Запущен")
        elif action == "pause":
            async with session_scope() as session:
                await AccountService(session).set_status(
                    account_id, AccountStatus.paused
                )
            if listeners is not None:
                await listeners.refresh_account(account_id)
            await callback.answer("⏸ На паузе")
        elif action == "spambot":
            await broker.enqueue(PoolTask(kind="spambot_check", account_id=account_id))
            await callback.answer("🧪 SpamBot в очереди")
        elif action == "ping":
            await broker.enqueue(PoolTask(kind="ping_me", account_id=account_id))
            await callback.answer("🏓 Ping в очереди")
        else:
            await callback.answer("Неизвестное действие", show_alert=True)
            return

        # aiogram CallbackQuery is frozen — do not mutate callback.data
        await _show_account_detail(callback, account_id)

    _ = settings
    return router
