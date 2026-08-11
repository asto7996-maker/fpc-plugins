"""
Canonical money display for dwar.ru.

Game ``state.money`` is a float where the integer part is gold and the
fractional part is silver (e.g. ``158.13`` → 158 зол. 13 сер.).
``money_gold`` / ``money_silver`` from the API are preferred when present.
"""

from __future__ import annotations

from typing import Any, Optional, Union


Number = Union[int, float]


def split_money(
    money: Number = 0,
    *,
    money_gold: Optional[Number] = None,
    money_silver: Optional[Number] = None,
) -> tuple[int, int]:
    """
    Return ``(gold, silver)`` with silver in ``0..99``.

    Prefer explicit gold/silver fields when either is non-zero; otherwise
    split the combined ``money`` float.
    """
    g_raw = money_gold
    s_raw = money_silver
    if g_raw is not None or s_raw is not None:
        g = int(float(g_raw or 0))
        s = int(round(float(s_raw or 0)))
        # Some payloads put total in money_gold only (as float 12.34)
        if s == 0 and g_raw is not None:
            fv = float(g_raw or 0)
            if abs(fv - g) > 1e-9:
                g = int(fv)
                s = int(round((fv - g) * 100))
        if s < 0:
            s = 0
        if s >= 100:
            g += s // 100
            s = s % 100
        if g or s or float(money or 0) == 0:
            return g, s

    total = float(money or 0)
    if total < 0:
        total = 0.0
    gold = int(total)
    silver = int(round((total - gold) * 100))
    if silver >= 100:
        gold += silver // 100
        silver = silver % 100
    if silver < 0:
        silver = 0
    return gold, silver


def money_to_float(
    money: Number = 0,
    *,
    money_gold: Optional[Number] = None,
    money_silver: Optional[Number] = None,
) -> float:
    """Normalized float gold.silver for deltas / telemetry."""
    g, s = split_money(money, money_gold=money_gold, money_silver=money_silver)
    return float(g) + float(s) / 100.0


def format_money(
    money: Number = 0,
    *,
    money_gold: Optional[Number] = None,
    money_silver: Optional[Number] = None,
    short: bool = False,
) -> str:
    """
    Human-readable money.

    short=False → ``158 зол. 13 сер.``
    short=True  → ``158.13``
    """
    g, s = split_money(money, money_gold=money_gold, money_silver=money_silver)
    if short:
        return f"{g}.{s:02d}"
    if s:
        return f"{g} зол. {s} сер."
    return f"{g} зол."


def format_money_delta(delta: Number, *, short: bool = False) -> str:
    """Signed delta string, e.g. ``+1.25`` or ``+1 зол. 25 сер.``."""
    d = float(delta or 0)
    sign = "+" if d >= 0 else "−"
    abs_d = abs(d)
    if short:
        g, s = split_money(abs_d)
        return f"{sign}{g}.{s:02d}"
    return f"{sign}{format_money(abs_d)}"


def money_from_state(state: Any) -> float:
    """Pull normalized money float from a GameState-like object/dict."""
    if state is None:
        return 0.0
    if isinstance(state, dict):
        return money_to_float(
            state.get("money", 0) or 0,
            money_gold=state.get("money_gold"),
            money_silver=state.get("money_silver"),
        )
    return money_to_float(
        getattr(state, "money", 0) or 0,
        money_gold=getattr(state, "money_gold", None),
        money_silver=getattr(state, "money_silver", None),
    )


def format_money_from_state(state: Any, *, short: bool = False) -> str:
    if state is None:
        return format_money(0, short=short)
    if isinstance(state, dict):
        return format_money(
            state.get("money", 0) or 0,
            money_gold=state.get("money_gold"),
            money_silver=state.get("money_silver"),
            short=short,
        )
    return format_money(
        getattr(state, "money", 0) or 0,
        money_gold=getattr(state, "money_gold", None),
        money_silver=getattr(state, "money_silver", None),
        short=short,
    )
