"""
Контексты событий Starvell для плагинов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.bot_core import BotCore

logger = logging.getLogger("starvell.context")


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
    handled: bool = False

    def mark_handled(self) -> None:
        """Плагин обработал сообщение — ядро не шлёт welcome/ИИ."""
        self.handled = True

    async def reply(self, text: str) -> bool:
        api = self.api() or self.core.get_api("default")
        if not api or not self.chat_id:
            logger.error(
                "MessageContext.reply: не отправлено (api=%s chat_id=%s account=%s)",
                bool(api), self.chat_id, self.account_name,
            )
            return False
        try:
            await api.send_message(str(self.chat_id), text)
            return True
        except Exception as exc:
            logger.error("MessageContext.reply failed chat=%s: %s", self.chat_id, exc)
            return False

    async def reply_watermarked(self, text: str) -> bool:
        api = self.api() or self.core.get_api("default")
        s = self.settings
        if api:
            text = api.apply_watermark(text, s.watermark_on, s.watermark_text)
        return await self.reply(text)


def _starvell_amount_to_rub(value: Any) -> float:
    """Starvell API передаёт суммы в копейках (целое число)."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return 0.0
    if val == 0:
        return 0.0
    # 224 → 2.24 ₽; уже дробные рубли (2.24) не трогаем
    if abs(val - round(val)) < 1e-9:
        return round(val) / 100.0
    return val


def _resolve_order_price(order: dict) -> float:
    """Итоговая сумма покупки в рублях."""
    total = order.get("totalPrice")
    if total is not None:
        val = _starvell_amount_to_rub(total)
        if val > 0:
            return val
    base = order.get("basePrice")
    qty = max(1, int(order.get("quantity") or 1))
    if base is not None:
        try:
            unit = _starvell_amount_to_rub(base)
            if unit > 0:
                return unit * qty
        except (TypeError, ValueError):
            pass
        val = _starvell_amount_to_rub(base)
        if val > 0:
            return val
    for key in ("price", "amount", "sum", "total"):
        if order.get(key) is not None:
            val = _starvell_amount_to_rub(order[key])
            if val > 0:
                return val
    return 0.0


def format_rub(amount: Any) -> str:
    try:
        return f"{float(amount):.2f} ₽"
    except (TypeError, ValueError):
        return "0.00 ₽"


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
            price=_resolve_order_price(order),
            quantity=max(1, int(order.get("quantity") or 1)),
        )

    async def send_to_buyer(self, text: str) -> bool:
        api = self.api() or self.core.get_api("default")
        if not api:
            logger.error("OrderContext.send_to_buyer: API недоступен order=%s", self.order_id)
            return False
        if not self.chat_id and self.buyer_id:
            self.chat_id = await api.find_chat_by_buyer(int(self.buyer_id))
        if not self.chat_id:
            logger.error(
                "OrderContext.send_to_buyer: чат не найден order=%s buyer=%s",
                self.order_id, self.buyer_id,
            )
            return False
        try:
            await api.send_message(str(self.chat_id), text)
            return True
        except Exception as exc:
            logger.error("OrderContext.send_to_buyer failed order=%s: %s", self.order_id, exc)
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
