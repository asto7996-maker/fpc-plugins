"""
Декораторы хуков Starvell — регистрируют обработчики событий плагина.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# События Starvell (нативные, не FPC)
STV_MESSAGE = "starvell.message"
STV_ORDER_PAID = "starvell.order.paid"
STV_ORDER_COMPLETED = "starvell.order.completed"
STV_ORDER_STATUS = "starvell.order.status"
STV_PRE_DELIVERY = "starvell.pre_delivery"
STV_POST_DELIVERY = "starvell.post_delivery"
STV_BUMP = "starvell.bump"
STV_STARTUP = "starvell.startup"
STV_SHUTDOWN = "starvell.shutdown"

_HOOK_ATTR = "_starvell_hooks"


def _register(event: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        hooks: dict[str, list] = getattr(func, _HOOK_ATTR, {})
        hooks.setdefault(event, []).append(func)
        setattr(func, _HOOK_ATTR, hooks)
        # Также на уровне функции для сбора при сканировании класса
        if not hasattr(func, "_starvell_event"):
            func._starvell_event = event  # type: ignore[attr-defined]
        return func

    return decorator


def on_message(func: Callable) -> Callable:
    """Новое сообщение покупателя в чате."""
    func._starvell_event = STV_MESSAGE  # type: ignore[attr-defined]
    return func


def on_order_paid(func: Callable) -> Callable:
    """Новый оплаченный заказ (status CREATED)."""
    func._starvell_event = STV_ORDER_PAID  # type: ignore[attr-defined]
    return func


def on_order_completed(func: Callable) -> Callable:
    """Заказ завершён (COMPLETED)."""
    func._starvell_event = STV_ORDER_COMPLETED  # type: ignore[attr-defined]
    return func


def on_order_status(func: Callable) -> Callable:
    """Смена статуса заказа."""
    func._starvell_event = STV_ORDER_STATUS  # type: ignore[attr-defined]
    return func


def on_pre_delivery(func: Callable) -> Callable:
    """Перед автовыдачей (можно отменить через ctx)."""
    func._starvell_event = STV_PRE_DELIVERY  # type: ignore[attr-defined]
    return func


def on_post_delivery(func: Callable) -> Callable:
    """После автовыдачи."""
    func._starvell_event = STV_POST_DELIVERY  # type: ignore[attr-defined]
    return func


def on_bump(func: Callable) -> Callable:
    """После поднятия лотов."""
    func._starvell_event = STV_BUMP  # type: ignore[attr-defined]
    return func


def collect_hooks(plugin_cls: type) -> dict[str, list[Callable]]:
    """Собирает все @on_* хуки из методов класса плагина."""
    result: dict[str, list[Callable]] = {}
    for attr_name in dir(plugin_cls):
        if attr_name.startswith("_"):
            continue
        method = getattr(plugin_cls, attr_name, None)
        event = getattr(method, "_starvell_event", None)
        if event:
            result.setdefault(event, []).append(attr_name)
    return result
