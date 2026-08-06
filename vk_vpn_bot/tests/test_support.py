"""
Тесты кнопки поддержки: ссылка, клавиатура, маршрутизация.
Запуск из папки vk_vpn_bot:
    python3 tests/test_support.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keyboards.menus import (
    BTN_ASK,
    BTN_BACK,
    BTN_INFO,
    help_keyboard,
    support_wait_keyboard,
)
from services.support import is_self_dialog_url, usable_support_url


class _Settings:
    def __init__(self, url: str, group_id: int = 240702990) -> None:
        self.support_url = url
        self.group_id = group_id


def _labels(raw: str) -> list[tuple[str, str]]:
    data = json.loads(raw)
    return [
        (b["action"]["type"], b["action"].get("label") or b["action"].get("link", ""))
        for row in data["buttons"]
        for b in row
    ]


def test_self_dialog_detection() -> None:
    gid = 240702990
    assert is_self_dialog_url(f"https://vk.com/im?sel=-{gid}", gid) is True
    assert is_self_dialog_url(f"https://vk.com/im.php?sel=-{gid}", gid) is True
    assert is_self_dialog_url("https://vk.com/im?sel=-999", gid) is False
    assert is_self_dialog_url("https://vk.com/im?sel=634094665", gid) is False
    assert is_self_dialog_url("https://t.me/paskod_support", gid) is False
    assert is_self_dialog_url("", gid) is False
    # Некорректный sel не должен ломать проверку
    assert is_self_dialog_url("https://vk.com/im?sel=abc", gid) is False


def test_usable_support_url() -> None:
    gid = 240702990
    # Ссылка на собственный диалог бесполезна — её не показываем
    assert usable_support_url(_Settings(f"https://vk.com/im?sel=-{gid}")) == ""
    assert usable_support_url(_Settings("")) == ""
    # VK принимает в open_link только https
    assert usable_support_url(_Settings("http://example.com/help")) == ""
    assert (
        usable_support_url(_Settings("https://t.me/paskod_support"))
        == "https://t.me/paskod_support"
    )


def test_support_keyboard() -> None:
    without = _labels(help_keyboard(""))
    labels = [label for _, label in without]
    assert BTN_ASK in labels
    assert BTN_INFO in labels
    assert BTN_BACK in labels

    with_link = _labels(help_keyboard("https://t.me/paskod_support"))
    kinds = [kind for kind, _ in with_link]
    assert "open_link" in kinds


def test_support_wait_keyboard() -> None:
    wait = _labels(support_wait_keyboard())
    assert [label for _, label in wait] == [BTN_BACK], "из режима вопроса нужен выход"


def test_routing() -> None:
    from tools.diagnose_support import resolve

    assert resolve(BTN_ASK) == "cmd_ask_question"
    assert resolve("📖 Помощь") == "cmd_help"
    assert resolve(BTN_BACK) == "cmd_start"


def main() -> None:
    test_self_dialog_detection()
    test_usable_support_url()
    test_support_keyboard()
    test_support_wait_keyboard()
    test_routing()
    print("test_support: OK")


if __name__ == "__main__":
    main()
