"""
UX-хелперы: скелетоны загрузки при ожидании API.
"""

from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiogram.types import CallbackQuery, Message

logger = logging.getLogger("starvell.handlers.loading")

LOADING_TEXT = "⚡️ <i>Запрос обрабатывается…</i>"


@asynccontextmanager
async def loading_skeleton(
    target: Message | CallbackQuery,
    loading_text: str = LOADING_TEXT,
) -> AsyncIterator[Message]:
    """
    Показывает скелетон загрузки и гарантирует, что сообщение не останется
    в состоянии «обрабатывается» при ошибке внутри блока.
    """
    message: Message | None = None
    original_text = ""
    original_markup = None

    if isinstance(target, CallbackQuery):
        message = target.message
        try:
            await target.answer()
        except Exception:
            pass
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
    except Exception as exc:
        logger.exception("loading_skeleton failed: %s", exc)
        err = html.escape(str(exc))[:400]
        fallback = f"❌ <b>Ошибка</b>\n\n<code>{err}</code>"
        restored = False
        for text, markup in (
            (fallback, original_markup),
            (fallback, None),
            (original_text or fallback, original_markup),
        ):
            if not text:
                continue
            try:
                await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
                restored = True
                break
            except Exception:
                continue
        if not restored and isinstance(target, CallbackQuery):
            try:
                await target.answer(f"Ошибка: {str(exc)[:180]}", show_alert=True)
            except Exception:
                pass
        raise


async def with_loading(
    target: Message | CallbackQuery,
    coro,
    *,
    loading_text: str = LOADING_TEXT,
):
    """Обёртка: показать загрузку → выполнить coro → вернуть результат."""
    async with loading_skeleton(target, loading_text=loading_text):
        return await coro
