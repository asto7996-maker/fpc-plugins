"""Proxy management."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from tg_pool.admin.keyboards import proxies_kb
from tg_pool.admin.routers.common import safe_edit
from tg_pool.admin.states import AddProxyStates
from tg_pool.admin.texts import proxies_text
from tg_pool.config import Settings
from tg_pool.db.models import Proxy
from tg_pool.db.session import session_scope
from tg_pool.services.account_service import AccountService
from tg_pool.services.proxy_parse import ProxyParseError, parse_proxy_line

_PROXY_HINT = (
    "🌐 <b>Новый прокси</b>\n\n"
    "Отправьте строку в одном из форматов:\n"
    "<code>user:password@ip:port</code>\n"
    "<code>socks5://user:password@ip:port</code>\n"
    "<code>http://user:password@ip:port</code>\n"
    "<code>ip:port</code>\n\n"
    "<i>Без схемы по умолчанию используется SOCKS5.</i>"
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
            _PROXY_HINT,
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(AddProxyStates.raw)
    async def add_proxy_raw(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        try:
            parsed = parse_proxy_line(raw)
        except ProxyParseError as exc:
            await message.answer(
                f"❌ {exc}\n\n"
                "Примеры:\n"
                "<code>user:password@ip:port</code>\n"
                "<code>socks5://user:pass@ip:port</code>",
                parse_mode="HTML",
            )
            return
        async with session_scope() as session:
            proxy = await AccountService(session).create_proxy(
                ip=parsed.ip,
                port=parsed.port,
                protocol=parsed.protocol,
                username=parsed.username,
                password=parsed.password,
            )
            pid = proxy.id
        await state.clear()
        await message.answer(
            f"✅ Прокси <b>#{pid}</b> сохранён\n"
            f"<code>{parsed.protocol.value}://{parsed.ip}:{parsed.port}</code>",
            parse_mode="HTML",
            reply_markup=proxies_kb(),
        )

    _ = settings
    return router
