"""
Шаблоны автовыдачи 2.0 — умные плейсхолдеры.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from config import REFUND_DISCLAIMER_STRICT

PLACEHOLDERS = (
    "{username}",
    "{order_id}",
    "{date}",
    "{product_name}",
    "{product}",
    "{content}",
    "{price}",
    "{quantity}",
)


def render_delivery_template(template: str, **ctx: Any) -> str:
    """
    Подставляет плейсхолдеры в шаблон выдачи.

    Поддерживает: {username}, {order_id}, {date}, {product_name}, {product},
    {content}, {price}, {quantity}
    """
    data = dict(ctx)
    if "product_name" not in data and "product" in data:
        data["product_name"] = data["product"]
    if "product" not in data and "product_name" in data:
        data["product"] = data["product_name"]
    if "date" not in data:
        data["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")

    result = template
    for key, val in data.items():
        for brace in (f"{{{key}}}", f"${key}"):
            result = result.replace(brace, str(val))
    return result


def append_refund_disclaimer(text: str, strict: bool = True) -> str:
    """Добавляет жёсткий дисклеймер о невозврате."""
    disclaimer = REFUND_DISCLAIMER_STRICT if strict else ""
    if disclaimer and disclaimer not in text:
        return f"{text.rstrip()}\n\n{disclaimer}"
    return text
