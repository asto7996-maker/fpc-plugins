"""
UX-хелперы: скелетоны загрузки при ожидании API.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiogram.types import CallbackQuery, Message

LOADING_TEXT = "⚡️ <i>Запрос обрабатывается…</i>"


@asynccontextmanager
async def loading_skeleton(
    target: Message | CallbackQuery,
    loading_text: str = LOADING_TEXT,
) -> AsyncIterator[Message]:
    """
    Мгновенно показывает скелетон загрузки, затем восстанавливает контекст.

    Usage:
        async with loading_skeleton(call) as msg:
            data = await slow_api()
            await msg.edit_text(format_result(data), ...)
    """
    if isinstance(target, CallbackQuery):
        message = target.message
        await target.answer()
    else:
        message = target

    if not message:
        yield message  # type: ignore[misc]
        return

    original_text = message.html_text or message.text or ""
    original_markup = message.reply_markup

    try:
        await message.edit_text(loading_text, parse_mode="HTML")
    except Exception:
        pass

    try:
        yield message
    finally:
        pass


async def with_loading(
    target: Message | CallbackQuery,
    coro,
    *,
    loading_text: str = LOADING_TEXT,
):
    """Обёртка: показать загрузку → выполнить coro → вернуть результат."""
    if isinstance(target, CallbackQuery):
        msg = target.message
        await target.answer()
    else:
        msg = target

    if msg:
        try:
            await msg.edit_text(loading_text, parse_mode="HTML")
        except Exception:
            pass

    result = await coro
    return result
