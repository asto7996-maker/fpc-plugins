"""
Тесты компактного UI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keyboards.menus import (
    BTN_HELP,
    BTN_PAY_SBP,
    help_keyboard,
    main_menu_keyboard,
    pay_methods_keyboard,
)
from services.payments import PLATEGA_METHOD_SBP_QR, method_label


def _reply_buttons(raw: str) -> list[int]:
    data = json.loads(raw)
    return [len(row) for row in data["buttons"]]


def test_main_menu_compact() -> None:
    rows = _reply_buttons(main_menu_keyboard(""))
    assert len(rows) == 3
    assert all(n <= 2 for n in rows)
    assert max(rows) <= 2


def test_pay_methods_one_row() -> None:
    rows = _reply_buttons(pay_methods_keyboard())
    assert rows[0] == 3  # СБП | Карта | Крипта
    assert len(rows) <= 2


def test_help_hub() -> None:
    kb = help_keyboard("")
    labels = [
        b["action"]["label"]
        for row in json.loads(kb)["buttons"]
        for b in row
    ]
    assert BTN_HELP not in labels
    assert len(labels) <= 6


def test_short_payment_labels() -> None:
    assert method_label(PLATEGA_METHOD_SBP_QR) == "🏦 СБП"
    assert BTN_PAY_SBP == "🏦 СБП"


def main() -> None:
    test_main_menu_compact()
    test_pay_methods_one_row()
    test_help_hub()
    test_short_payment_labels()
    print("test_ui_compact: OK")


if __name__ == "__main__":
    main()
