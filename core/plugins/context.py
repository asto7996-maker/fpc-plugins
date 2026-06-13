"""
Контексты событий Starvell для плагинов.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.bot_core import BotCore


@dataclass
class StarvellContext:
    """Базовый контекст — доступ к ядру и API."""

    core: Any
    account_name: str = "default"
    plugin: Any = None

    @property
    def db(self):
        return self.core.db

    @property
    def settings(self):
        return self.core.settings

    def api(self, account: str | None = None):
        return self.core.get_api(account or self.account_name)

    async def notify(self, text: str, notify_type: str = "notify_orders", **extra) -> None:
        await self.core.notify(text, notify_type, **extra)


@dataclass
class MessageContext(StarvellContext):
    """Новое сообщение покупателя в чате Starvell."""

    chat_id: str = ""
    text: str = ""
    author_id: int | None = None
    username: str = ""
    message_id: str = ""
    raw_message: dict = field(default_factory=dict)

    async def reply(self, text: str) -> None:
        api = self.api()
        if api and self.chat_id:
            await api.send_message(self.chat_id, text)

    async def reply_watermarked(self, text: str) -> None:
        api = self.api()
        s = self.settings
        if api:
            text = api.apply_watermark(text, s.watermark_on, s.watermark_text)
        await self.reply(text)


@dataclass
class OrderContext(StarvellContext):
    """Событие заказа Starvell."""

    order: dict = field(default_factory=dict)
    order_id: str = ""
    status: str = ""
    buyer_username: str = ""
    buyer_id: int | None = None
    product_name: str = ""
    price: Any = ""
    quantity: int = 1
    chat_id: str | None = None

    @classmethod
    def from_order(cls, core: Any, order: dict, account_name: str = "default", plugin: Any = None) -> OrderContext:
        buyer = order.get("user") or {}
        offer = order.get("offerDetails") or {}
        desc = (offer.get("descriptions") or {}).get("rus") or {}
        product = (
            str(desc.get("briefDescription") or "").strip()
            or str(desc.get("description") or "").strip()
            or "товар"
        )
        return cls(
            core=core,
            account_name=account_name,
            plugin=plugin,
            order=order,
            order_id=str(order.get("id") or ""),
            status=str(order.get("status") or ""),
            buyer_username=str(buyer.get("username") or ""),
            buyer_id=buyer.get("id"),
            product_name=product,
            price=order.get("basePrice") or order.get("totalPrice") or 0,
            quantity=max(1, int(order.get("quantity") or 1)),
        )

    async def send_to_buyer(self, text: str) -> bool:
        api = self.api()
        if not api:
            return False
        if not self.chat_id and self.buyer_id:
            self.chat_id = await api.find_chat_by_buyer(int(self.buyer_id))
        if self.chat_id:
            await api.send_message(self.chat_id, text)
            return True
        return False


@dataclass
class DeliveryContext(OrderContext):
    """Автовыдача — до/после отправки товара."""

    delivery_text: str = ""
    codes: list[str] = field(default_factory=list)
    success: bool = False
    error: str = ""
    skip_delivery: bool = False

    def cancel(self) -> None:
        """Отменить автовыдачу (вызовите в @on_pre_delivery)."""
        self.skip_delivery = True


@dataclass
class BumpContext(StarvellContext):
    """Событие автоподнятия лотов."""

    categories_bumped: int = 0
