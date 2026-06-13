"""Определение SMM-лотов по описанию (ID: в тексте лота)."""

from __future__ import annotations

import re

SERVICE_ID_RE = re.compile(r"ID:\s*(\d+)", re.IGNORECASE)


def is_smm_lot_order(order: dict) -> bool:
    offer = order.get("offerDetails") or {}
    desc = (offer.get("descriptions") or {}).get("rus") or {}
    parts = [
        str(desc.get("description") or ""),
        str(desc.get("briefDescription") or ""),
        str(offer.get("title") or ""),
    ]
    text = "\n".join(p for p in parts if p)
    return bool(SERVICE_ID_RE.search(text))
