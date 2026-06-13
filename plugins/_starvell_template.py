"""
Шаблон нативного плагина Starvell — скопируйте, переименуйте UUID и доработайте.
"""

from __future__ import annotations

from starvell_sdk import MessageContext, OrderContext, StarvellPlugin, on_message, on_order_paid

NAME = "Мой плагин"
UUID = "00000000-0000-0000-0000-000000000001"  # замените на уникальный UUID
VERSION = "1.0.0"
DESCRIPTION = "Краткое описание"
CREDITS = "Ваше имя"
SETTINGS_PAGE = False


class Plugin(StarvellPlugin):
    """class Plugin обязателен — движок ищет именно это имя."""

    NAME = NAME
    UUID = UUID
    VERSION = VERSION
    DESCRIPTION = DESCRIPTION
    CREDITS = CREDITS

    async def on_startup(self) -> None:
        self.log("Плагин запущен")

    @on_message
    async def greet(self, ctx: MessageContext) -> None:
        if "привет" in ctx.text.lower():
            await ctx.reply("Здравствуйте!")

    @on_order_paid
    async def new_order(self, ctx: OrderContext) -> None:
        self.log("Новый заказ #%s от %s", ctx.order_id, ctx.buyer_username)
