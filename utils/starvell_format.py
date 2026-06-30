"""Форматирование данных Starvell API для Telegram."""

from __future__ import annotations

from typing import Any


def format_rub_balance(value: Any) -> str:
    """Баланс из API (число, строка или dict rubBalance)."""
    if value is None:
        return "—"
    if isinstance(value, dict):
        for key in ("withdrawableRubBalance", "rubBalance", "available", "balance", "amount"):
            if key in value and value[key] is not None:
                try:
                    amount = float(value[key])
                    return _fmt_rub(amount, wallet=True)
                except (TypeError, ValueError):
                    continue
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text if text else "—"
    return _fmt_rub(amount, wallet=False)


def _fmt_rub(amount: float, *, wallet: bool) -> str:
    if not wallet and abs(amount - round(amount)) < 1e-9 and amount >= 100:
        amount = amount / 100.0
    formatted = f"{amount:,.2f}".replace(",", " ")
    # 3 239,00 ₽
    parts = formatted.split(".")
    if len(parts) == 2:
        return f"{parts[0]},{parts[1]} ₽"
    return f"{formatted} ₽"


def format_hold_balance(value: Any) -> str:
    if isinstance(value, dict):
        held = value.get("holdedRubBalance") or value.get("holded") or 0
        try:
            h = float(held)
            if h > 0:
                return format_rub_balance(h)
        except (TypeError, ValueError):
            pass
    return ""
