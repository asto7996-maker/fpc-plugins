"""
Базовый класс плагина Starvell — пишите плагины ТОЛЬКО для Starvell.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from core.plugins.base import BasePlugin
from core.plugins.context import DeliveryContext, MessageContext, OrderContext
from core.plugins.hooks import (
    STV_BUMP,
    STV_MESSAGE,
    STV_ORDER_COMPLETED,
    STV_ORDER_PAID,
    STV_ORDER_STATUS,
    STV_POST_DELIVERY,
    STV_PRE_DELIVERY,
    STV_SHUTDOWN,
    STV_STARTUP,
    collect_hooks,
    on_bump,
    on_message,
    on_order_completed,
    on_order_paid,
    on_order_status,
    on_post_delivery,
    on_pre_delivery,
)

__all__ = [
    "StarvellPlugin",
    "MessageContext",
    "OrderContext",
    "DeliveryContext",
    "on_message",
    "on_order_paid",
    "on_order_completed",
    "on_order_status",
    "on_pre_delivery",
    "on_post_delivery",
    "on_bump",
]


class StarvellPlugin(BasePlugin):
    """
    Нативный плагин Starvell Cardinal.

    Пример::

        from starvell_sdk import StarvellPlugin, on_message, MessageContext

        NAME = "Мой плагин"
        UUID = "my-plugin-001"
        VERSION = "1.0.0"

        class Plugin(StarvellPlugin):
            async def on_load(self):
                self.log("Загружен")

            @on_message
            async def hello(self, ctx: MessageContext):
                if "привет" in ctx.text.lower():
                    await ctx.reply("Здравствуйте!")
    """

    # Тип плагина для движка
    STV_NATIVE: bool = True

    def __init__(self, core, config=None):
        super().__init__(core, config)
        self._hook_map: dict[str, list[str]] = collect_hooks(self.__class__)

    def log(self, msg: str, *args, level: str = "info") -> None:
        getattr(self.logger, level)(msg, *args)

    async def get_cfg(self, key: str, default: Any = None) -> Any:
        return await self.plugin_settings.get(self.UUID, key, default)

    async def set_cfg(self, key: str, value: Any) -> None:
        await self.plugin_settings.set(self.UUID, key, value)

    def on_load(self) -> None:
        """Синхронная инициализация (переопределите при необходимости)."""
        self.log("%s v%s loaded", self.NAME, self.VERSION)

    async def on_startup(self) -> None:
        """Асинхронный старт после регистрации хуков."""

    async def on_shutdown(self) -> None:
        """Выгрузка плагина."""

    def on_unload(self) -> None:
        pass

    async def dispatch(self, event: str, ctx: Any) -> None:
        """Вызывает зарегистрированные @on_* методы."""
        ctx.plugin = self
        for method_name in self._hook_map.get(event, []):
            method = getattr(self, method_name, None)
            if not method:
                continue
            try:
                result = method(ctx)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self.logger.exception("Hook %s.%s: %s", event, method_name, exc)

    # Удобные override-методы (альтернатива декораторам)
    async def handle_message(self, ctx: MessageContext) -> bool:
        """Верните True если сообщение обработано."""
        return False

    async def handle_order_paid(self, ctx: OrderContext) -> None:
        pass

    async def handle_order_completed(self, ctx: OrderContext) -> None:
        pass

    async def _dispatch_with_fallback(self, event: str, ctx: Any) -> None:
        await self.dispatch(event, ctx)
        if event == STV_MESSAGE and isinstance(ctx, MessageContext):
            await self.handle_message(ctx)
        elif event == STV_ORDER_PAID and isinstance(ctx, OrderContext):
            await self.handle_order_paid(ctx)
        elif event == STV_ORDER_COMPLETED and isinstance(ctx, OrderContext):
            await self.handle_order_completed(ctx)
