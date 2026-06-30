"""Middleware: сброс FSM до роутинга."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject, Update

logger = logging.getLogger("starvell.tg.middleware")

ESCAPE_COMMANDS = frozenset({
    "start", "menu", "cancel", "help", "restart", "profile", "status", "ping",
})


class FsmClearOnCommandMiddleware(BaseMiddleware):
    """Сбрасывает FSM до выбора handler — /start работает из любого режима."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message: Message | None = None
        if isinstance(event, Update):
            message = event.message
        elif isinstance(event, Message):
            message = event

        if message and message.text and message.text.startswith("/"):
            cmd = message.text.strip().split()[0].split("@")[0].lstrip("/").lower()
            if cmd in ESCAPE_COMMANDS:
                state: FSMContext | None = data.get("state")
                if state:
                    try:
                        await state.clear()
                    except Exception as exc:
                        logger.debug("fsm clear: %s", exc)

        return await handler(event, data)


def register_tg_middleware(dp: Any) -> None:
    dp.update.outer_middleware(FsmClearOnCommandMiddleware())
