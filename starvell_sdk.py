"""
Публичный SDK для авторов плагинов Starvell.

Скопируйте этот файл в plugins/ или импортируйте напрямую:

    from starvell_sdk import StarvellPlugin, on_message, MessageContext
"""

from core.plugins.context import BumpContext, DeliveryContext, MessageContext, OrderContext, StarvellContext
from core.plugins.hooks import (
    STV_BUMP,
    STV_MESSAGE,
    STV_ORDER_COMPLETED,
    STV_ORDER_PAID,
    STV_ORDER_STATUS,
    STV_POST_DELIVERY,
    STV_PRE_DELIVERY,
    on_bump,
    on_message,
    on_order_completed,
    on_order_paid,
    on_order_status,
    on_post_delivery,
    on_pre_delivery,
)
from core.plugins.starvell_plugin import StarvellPlugin

# Алиас — в каждом плагине класс ДОЛЖЕН называться Plugin
Plugin = StarvellPlugin

__all__ = [
    "Plugin",
    "StarvellPlugin",
    "MessageContext",
    "OrderContext",
    "DeliveryContext",
    "BumpContext",
    "StarvellContext",
    "on_message",
    "on_order_paid",
    "on_order_completed",
    "on_order_status",
    "on_pre_delivery",
    "on_post_delivery",
    "on_bump",
    "STV_MESSAGE",
    "STV_ORDER_PAID",
    "STV_ORDER_COMPLETED",
]
