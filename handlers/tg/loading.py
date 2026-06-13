"""
UX-хелперы: скелетоны загрузки при ожидании API.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from aiogram.types import CallbackQuery, Message

LOADING_TEXT = "⚡️ <i>Запрос обрабатывается…</i>"


@asynccontextmanager
async def loading_skeleton(
    target: Message | CallbackQuery,
    loading_text: str = LOADING_TEXT,
    min_delay: float = 0.35,
) -> AsyncIterator[Message]:
    """
    Показывает скелетон только если операция длится дольше min_delay.
    Быстрые ответы не мигают «Запрос обрабатывается…».
    """
    if isinstance(target, CallbackQuery):
        message = target.message
        await target.answer()
    else:
        message = target

    if not message:
        yield message  # type: ignore[misc]
        return

    async def _show_loading() -> None:
        await asyncio.sleep(min_delay)
        try:
            await message.edit_text(loading_text, parse_mode="HTML")
        except Exception:
            pass

    loading_task = asyncio.create_task(_show_loading())
    try:
        yield message
    finally:
        loading_task.cancel()
        with suppress(asyncio.CancelledError):
            await loading_task


async def with_loading(
    target: Message | CallbackQuery,
    coro,
    *,
    loading_text: str = LOADING_TEXT,
    min_delay: float = 0.35,
):
    """Обёртка: показать загрузку → выполнить coro → вернуть результат."""
    async with loading_skeleton(target, loading_text=loading_text, min_delay=min_delay):
        return await coro
