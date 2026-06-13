"""Нормализация денежных сумм Starvell API."""

from __future__ import annotations

from typing import Any


def starvell_money_to_rub(value: Any) -> float:
    """
    Starvell API передаёт суммы в копейках (целое число).
    Пример: totalPrice=224 → 2.24 ₽. Дробные значения считаем уже рублями.
    """
    if value is None:
        return 0.0
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.0
    if raw == 0:
        return 0.0
    if abs(raw - round(raw)) > 1e-6:
        return raw
    return raw / 100.0
