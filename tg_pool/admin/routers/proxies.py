"""Auto proxy pool — fetch & check public lists (no manual paste)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from tg_pool.admin.keyboards import proxies_kb
from tg_pool.admin.routers.common import safe_edit
from tg_pool.admin.texts import proxies_text
from tg_pool.config import Settings
from tg_pool.db.models import Proxy
from tg_pool.db.session import session_scope
from tg_pool.services.proxy_finder import refresh_proxy_pool, wipe_all_proxies

logger = logging.getLogger(__name__)


async def _pool_view() -> tuple[str, object]:
    async with session_scope() as session:
        rows = list(
            (await session.execute(select(Proxy).order_by(Proxy.id))).scalars().all()
        )
        alive = sum(1 for p in rows if p.is_alive)
    text = (
        "🌐 <b>Авто-пул прокси</b>\n\n"
        "<blockquote>Ручной ввод отключён. Бот сам парсит публичные "
        "проверенные списки и оставляет только те, что достучались до Telegram DC.</blockquote>\n\n"
        f"В пуле: <b>{len(rows)}</b> · живых: <b>{alive}</b>\n\n"
        + proxies_text(rows)
    )
    return text, proxies_kb()


def build_proxies_router(settings: Settings) -> Router:
    router = Router(name="proxies")

    @router.callback_query(F.data == "menu:proxies")
    async def cb_proxies(callback: CallbackQuery) -> None:
        text, kb = await _pool_view()
        await safe_edit(callback, text, kb)
        await callback.answer()

    @router.message(F.text.in_({"🌐 Прокси", "Прокси"}))
    async def reply_proxies(message: Message) -> None:
        text, kb = await _pool_view()
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data == "proxy:refresh")
    async def cb_refresh(callback: CallbackQuery) -> None:
        await callback.answer("Ищу рабочие прокси…")
        status = await callback.message.answer(  # type: ignore[union-attr]
            "🔍 Парсю списки и проверяю доступ к Telegram DC…\n"
            "<i>Это может занять 30–90 секунд.</i>",
            parse_mode="HTML",
        )
        try:
            async with session_scope() as session:
                saved = await refresh_proxy_pool(session, needed=8, replace=True)
                n = len(saved)
            await status.edit_text(
                f"✅ Найдено рабочих прокси: <b>{n}</b>",
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("proxy refresh failed")
            await status.edit_text(
                f"❌ Ошибка поиска: <code>{type(exc).__name__}: {exc}</code>",
                parse_mode="HTML",
            )
        text, kb = await _pool_view()
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)  # type: ignore[union-attr]

    @router.callback_query(F.data == "proxy:wipe")
    async def cb_wipe(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            n = await wipe_all_proxies(session)
        await callback.answer(f"Удалено: {n}", show_alert=True)
        text, kb = await _pool_view()
        await safe_edit(callback, text, kb)

    _ = settings
    return router
