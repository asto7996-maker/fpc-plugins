"""
Dataclass-события Starvell (как в Lumus Starvell Bot).
Плагины LSB/FPC получают их через BIND_TO_*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NewMessageEvent:
    chat_id: str
    username: str
    text: str
    interlocutor_id: int | None = None
    raw: dict = field(default_factory=dict)
    is_new_chat: bool = False
    message_id: str = ""
    account_name: str = "default"


@dataclass
class NewOrderEvent:
    order_id: str
    username: str
    lot_title: str
    price: float
    raw: dict = field(default_factory=dict)
    account_name: str = "default"


@dataclass
class PaymentEvent:
    order_id: str
    username: str
    amount: float
    raw: dict = field(default_factory=dict)
    account_name: str = "default"


@dataclass
class OrderConfirmEvent:
    order_id: str
    username: str
    raw: dict = field(default_factory=dict)
    account_name: str = "default"


@dataclass
class ReviewEvent:
    order_id: str
    username: str
    rating: int
    text: str
    raw: dict = field(default_factory=dict)
    account_name: str = "default"
