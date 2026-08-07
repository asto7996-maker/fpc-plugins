"""
Тесты системы администрации.
Запуск из папки vk_vpn_bot:
    python3 tests/test_admin.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keyboards.menus import BTN_ADMIN, main_menu_keyboard
from services.admin import (
    DEFAULT_MAIN_ADMIN_VK_ID,
    bedolaga_admin_id_for_main_admin,
    is_main_admin,
    main_admin_vk_id,
    vk_pseudo_telegram_id,
)


class _Settings:
    main_admin_vk_id = DEFAULT_MAIN_ADMIN_VK_ID
    main_admin_username = "xylophaze"
    support_admin_ids = ()


def _labels(raw: str) -> list[str]:
    data = json.loads(raw)
    return [
        b["action"].get("label") or ""
        for row in data["buttons"]
        for b in row
    ]


def test_main_admin_id() -> None:
    settings = _Settings()
    assert main_admin_vk_id(settings) == 634094665
    assert is_main_admin(634094665, settings) is True
    assert is_main_admin(1, settings) is False


def test_pseudo_telegram_id() -> None:
    assert vk_pseudo_telegram_id(634094665) == 8_634_094_665
    settings = _Settings()
    assert bedolaga_admin_id_for_main_admin(settings) == 8_634_094_665


def test_admin_menu_button() -> None:
    without = _labels(main_menu_keyboard("", show_admin=False))
    with_admin = _labels(main_menu_keyboard("", show_admin=True))
    assert BTN_ADMIN not in without
    assert BTN_ADMIN in with_admin


def main() -> None:
    test_main_admin_id()
    test_pseudo_telegram_id()
    test_admin_menu_button()
    print("test_admin: OK")


if __name__ == "__main__":
    main()
