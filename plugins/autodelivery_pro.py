"""
Auto-Delivery Pro — нативный плагин Starvell с кастомными шаблонами выдачи.
"""

from __future__ import annotations

from core.delivery.templates import append_refund_disclaimer, render_delivery_template
from starvell_sdk import DeliveryContext, OrderContext, StarvellPlugin, on_order_paid, on_pre_delivery

NAME = "Auto-Delivery Pro"
UUID = "autodelivery-pro"
VERSION = "2.1.0"
DESCRIPTION = "Автовыдача 2.0 с умными плейсхолдерами и шаблонами"
CREDITS = "Starvell Cardinal Team"
SETTINGS_PAGE = True


class Plugin(StarvellPlugin):
    """Переопределяет текст автовыдачи через @on_pre_delivery."""

    NAME = NAME
    UUID = UUID
    VERSION = VERSION
    DESCRIPTION = DESCRIPTION
    CREDITS = CREDITS
    SETTINGS_PAGE = True

    DEFAULT_TEMPLATE = (
        "✅ <b>Заказ #{order_id} выполнен!</b>\n\n"
        "📦 <b>{product_name}</b>\n"
        "👤 Покупатель: {username}\n"
        "📅 {date}\n\n"
        "<code>{content}</code>"
    )

    def get_settings_schema(self) -> list[dict]:
        return [
            {"key": "use_custom_template", "label": "Свой шаблон", "type": "bool", "default": False},
            {"key": "custom_template", "label": "Шаблон выдачи", "type": "text", "default": ""},
        ]

    @on_order_paid
    async def on_paid(self, ctx: OrderContext) -> None:
        self.log("Заказ #%s оплачен (%s)", ctx.order_id, ctx.product_name)

    @on_pre_delivery
    async def customize_delivery(self, ctx: DeliveryContext) -> None:
        if not await self.get_cfg("use_custom_template", False):
            return
        template = await self.get_cfg("custom_template", "") or self.DEFAULT_TEMPLATE
        content = "\n".join(ctx.codes)
        ctx.delivery_text = self.build_delivery_message(
            template=template,
            username=ctx.buyer_username,
            order_id=ctx.order_id,
            product_name=ctx.product_name,
            content=content,
            price=ctx.price,
            quantity=ctx.quantity,
        )
        api = ctx.api()
        if api:
            s = ctx.settings
            ctx.delivery_text = api.apply_watermark(ctx.delivery_text, s.watermark_on, s.watermark_text)

    def build_delivery_message(
        self,
        *,
        template: str,
        username: str,
        order_id: str,
        product_name: str,
        content: str,
        price: str | float = "",
        quantity: int = 1,
    ) -> str:
        text = render_delivery_template(
            template or self.DEFAULT_TEMPLATE,
            username=username,
            order_id=order_id,
            product_name=product_name,
            product=product_name,
            content=content,
            price=price,
            quantity=quantity,
        )
        return append_refund_disclaimer(text, strict=True)
