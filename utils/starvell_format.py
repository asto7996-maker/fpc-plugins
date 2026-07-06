"""Форматирование данных Starvell API для Telegram."""

from __future__ import annotations

from typing import Any


def wallet_amount_to_rub(amount: float) -> float:
    """Сумма из wallet API (rubBalance и т.п.) — в копейках."""
    if abs(amount - round(amount)) < 1e-9:
        return amount / 100.0
    return amount


def homepage_balance_to_rub(amount: float) -> float:
    """Баланс user.balance с главной — уже в рублях."""
    return amount


def format_rub_balance(value: Any) -> str:
    """Баланс из API (число, строка или dict rubBalance)."""
    if value is None:
        return "—"
    if isinstance(value, dict):
        for key in ("withdrawableRubBalance", "rubBalance", "available", "balance", "amount"):
            if key in value and value[key] is not None:
                try:
                    amount = wallet_amount_to_rub(float(value[key]))
                    return _fmt_rub(amount)
                except (TypeError, ValueError):
                    continue
        return "—"
    try:
        amount = homepage_balance_to_rub(float(value))
    except (TypeError, ValueError):
        text = str(value).strip()
        return text if text else "—"
    return _fmt_rub(amount)


def _fmt_rub(amount: float) -> str:
    formatted = f"{amount:,.2f}".replace(",", " ")
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
                return _fmt_rub(wallet_amount_to_rub(h))
        except (TypeError, ValueError):
            pass
    return ""
