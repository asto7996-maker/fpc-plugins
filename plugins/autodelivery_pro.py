"""
Auto-Delivery Pro — пример продвинутого плагина на BasePlugin.

Демонстрирует:
- умные плейсхолдеры {username}, {order_id}, {date}, {product_name}
- доступ к BotCore и API
- жёсткий дисклеймер о невозврате
"""

from __future__ import annotations

import logging
from typing import Any

from core.delivery.templates import append_refund_disclaimer, render_delivery_template
from core.plugins.base import BasePlugin

logger = logging.getLogger("starvell.plugin.autodelivery_pro")

NAME = "Auto-Delivery Pro"
UUID = "autodelivery-pro"
VERSION = "2.0.0"
DESCRIPTION = "Автовыдача 2.0 с умными плейсхолдерами и шаблонами"
CREDITS = "Starvell Cardinal Team"
SETTINGS_CALLBACK = "sc:adel"


class Plugin(BasePlugin):
    """Плагин автовыдачи — расширяет встроенную логику шаблонами."""

    NAME = NAME
    UUID = UUID
    VERSION = VERSION
    DESCRIPTION = DESCRIPTION
    CREDITS = CREDITS
    SETTINGS_CALLBACK = SETTINGS_CALLBACK

    DEFAULT_TEMPLATE = (
        "✅ <b>Заказ #{order_id} выполнен!</b>\n\n"
        "📦 <b>{product_name}</b>\n"
        "👤 Покупатель: {username}\n"
        "📅 {date}\n\n"
        "<code>{content}</code>"
    )

    def on_load(self) -> None:
        self.logger.info("Auto-Delivery Pro v%s загружен", VERSION)
        self.core.events.on("order:paid", self._on_order_paid)

    def on_unload(self) -> None:
        self.core.events.off("order:paid", self._on_order_paid)
        self.logger.info("Auto-Delivery Pro выгружен")

    async def _on_order_paid(self, payload: dict[str, Any]) -> None:
        """Дополнительная обработка (основная выдача — в automation)."""
        order = payload.get("order") or {}
        order_id = str(order.get("id") or "")
        if order_id:
            self.logger.debug("order:paid hook #%s", order_id)

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
        """Формирует сообщение выдачи с плейсхолдерами и дисклеймером."""
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

    @staticmethod
    def extract_product_name(order: dict) -> str:
        offer = order.get("offerDetails") or {}
        desc = (offer.get("descriptions") or {}).get("rus") or {}
        return (
            str(desc.get("briefDescription") or "").strip()
            or str(desc.get("description") or "").strip()
            or "товар"
        )
