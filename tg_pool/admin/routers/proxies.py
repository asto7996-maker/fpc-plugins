"""Proxy management."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from tg_pool.admin.keyboards import proxies_kb
from tg_pool.admin.routers.common import safe_edit
from tg_pool.admin.states import AddProxyStates
from tg_pool.admin.texts import proxies_text
from tg_pool.config import Settings
from tg_pool.db.models import Proxy, ProxyProtocol
from tg_pool.db.session import session_scope
from tg_pool.services.account_service import AccountService

PROXY_RE = re.compile(
    r"^(?P<proto>socks5|http)://(?:(?P<user>[^:]+):(?P<password>[^@]+)@)?"
    r"(?P<ip>[^:]+):(?P<port>\d+)$",
    re.IGNORECASE,
)


def build_proxies_router(settings: Settings) -> Router:
    router = Router(name="proxies")

    @router.callback_query(F.data == "menu:proxies")
    async def cb_proxies(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            rows = (
                await session.execute(select(Proxy).order_by(Proxy.id))
            ).scalars().all()
        await safe_edit(callback, proxies_text(rows), proxies_kb())
        await callback.answer()

    @router.callback_query(F.data == "proxy:add")
    async def cb_add_proxy(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddProxyStates.raw)
        await callback.message.answer(  # type: ignore[union-attr]
            "🌐 <b>Новый прокси</b>\n\n"
            "Отправьте строку:\n"
            "<code>socks5://user:pass@ip:port</code>\n"
            "или <code>http://ip:port</code>",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(AddProxyStates.raw)
    async def add_proxy_raw(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        m = PROXY_RE.match(raw)
        if not m:
            await message.answer(
                "❌ Формат: <code>socks5://user:pass@ip:port</code>",
                parse_mode="HTML",
            )
            return
        async with session_scope() as session:
            proxy = await AccountService(session).create_proxy(
                ip=m.group("ip"),
                port=int(m.group("port")),
                protocol=ProxyProtocol(m.group("proto").lower()),
                username=m.group("user"),
                password=m.group("password"),
            )
            pid = proxy.id
        await state.clear()
        await message.answer(
            f"✅ Прокси <b>#{pid}</b> сохранён",
            parse_mode="HTML",
            reply_markup=proxies_kb(),
        )

    return router
