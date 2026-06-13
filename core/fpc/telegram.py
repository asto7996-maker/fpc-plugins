"""
FPC-совместимый Telegram-адаптер для aiogram 3.
Позволяет портированным плагинам использовать cardinal.telegram.* API.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger("starvell.fpc.telegram")


class TelegramAdapter:
    """
    Эмуляция FunPay Cardinal ``cardinal.telegram`` на aiogram.

    Поддерживает: ``cbq_handler``, ``msg_handler``, ``authorized_users``.
    """

    def __init__(self) -> None:
        self.bot: Any = None
        self.router = Router(name="fpc_compat")
        self.authorized_users: list[int] = []
        self._owner_id: int = 0

    def attach(self, bot: Any, dp: Any, owner_id: int, admin_ids: list[int]) -> None:
        self.bot = bot
        self._owner_id = owner_id
        self.authorized_users = list({owner_id, *admin_ids} - {0})
        dp.include_router(self.router)

    def is_authorized(self, user_id: int) -> bool:
        if not self._owner_id:
            return True
        return user_id in self.authorized_users

    def cbq_handler(
        self,
        func: Callable,
        regexp: str | None = None,
        startswith: str | None = None,
        func_data: Callable | None = None,
    ) -> Callable:
        """Регистрирует callback handler (FPC-style)."""

        async def wrapper(call: CallbackQuery) -> None:
            if not self.is_authorized(call.from_user.id):
                await call.answer("⛔", show_alert=True)
                return
            try:
                result = func(call)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.exception("FPC cbq_handler: %s", exc)

        if regexp:
            pattern = re.compile(regexp)
            self.router.callback_query.register(wrapper, F.data.regexp(pattern))
        elif startswith:
            self.router.callback_query.register(wrapper, F.data.startswith(startswith))
        elif func_data:
            # func_data(call) -> bool filter
            self.router.callback_query.register(
                wrapper,
                F.func(lambda c: isinstance(c, CallbackQuery) and bool(func_data(c))),
            )
        else:
            self.router.callback_query.register(wrapper)
        return func

    def msg_handler(self, func: Callable, regexp: str | None = None) -> Callable:
        async def wrapper(message: Message) -> None:
            if not self.is_authorized(message.from_user.id):
                return
            try:
                result = func(message)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.exception("FPC msg_handler: %s", exc)

        if regexp:
            self.router.message.register(wrapper, F.text.regexp(regexp))
        else:
            self.router.message.register(wrapper)
        return func

    async def answer(self, chat_id: int, text: str, **kwargs) -> Any:
        if self.bot:
            return await self.bot.send_message(chat_id, text, **kwargs)
        return None
