"""
Smoke-тесты оформления и юридических документов.
Запуск из папки vk_vpn_bot:
    python tests/test_ui_legal.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.helpers import format_info_menu, format_key_message, format_welcome
from handlers.style import brand, bullet, footer_hint, header, step
from keyboards.menus import (
    BTN_GUIDE,
    BTN_HELP,
    BTN_INFO,
    BTN_PRIVACY,
    BTN_TERMS,
    info_inline_keyboard,
    info_keyboard,
    main_menu_keyboard,
)
from legal.documents import ALL_DOCS, DOCS_BY_SLUG, format_doc_for_bot


class _Settings:
    bot_name = "Paskod VPN"
    trial_days = 3
    trial_traffic_gb = 10
    trial_devices = 1
    cabinet_url = "https://cabinet.paskod.ru"
    welcome_text = "test"
    support_text = "support"
    support_url = "https://vk.com"
    support_admin_ids = ()
    main_admin_vk_id = 634094665
    main_admin_username = "xylophaze"
    group_id = 240702990
    renew_days = 30
    bedolaga_api_key = ""


def test_style() -> None:
    assert "𝗣" in brand("Paskod") or "P" in brand("P")
    assert header("✨", "Тест") == "✨ Тест"
    assert "━━" not in header("✨", "Тест")
    assert step(1, "шаг") == "1. шаг"
    assert "①" not in step(1, "шаг")
    assert bullet("пункт") == "• пункт"
    assert footer_hint("далее") == "далее"
    assert "╰" not in footer_hint()


def test_key_message_is_calm() -> None:
    """Сообщение «🔑 Ключ» — без линий, блока «Что делать» и кружковых цифр."""
    msg = format_key_message(
        "https://sub.paskod.ru/demo",
        _Settings(),
        is_trial=False,
    )
    assert "Ваша ссылка" in msg
    assert "https://sub.paskod.ru/demo" in msg
    assert "Happ" in msg
    assert "1." in msg and "3." in msg
    assert "Что делать" not in msg
    assert "━━" not in msg
    assert "①" not in msg
    assert "📋" not in msg
    assert "╰" not in msg
    assert "·  ·" not in msg


def test_docs() -> None:
    assert len(ALL_DOCS) >= 5
    for doc in ALL_DOCS:
        assert doc.slug in DOCS_BY_SLUG
        text, total = format_doc_for_bot(doc, page=1)
        assert doc.title in text
        assert total >= 1
        assert len(text) < 4096


def test_menus_and_copy() -> None:
    settings = _Settings()
    welcome = format_welcome(settings, "Алекс")
    assert "Алекс" in welcome
    assert "✨" in welcome
    # Без каталога тексты обходятся без цифр, но остаются осмысленными
    assert "бесплатно" in welcome or "10 ГБ" in welcome
    assert "1 устройство" in welcome

    info = format_info_menu(settings)
    assert "Инфо" in info
    assert "документ" in info.lower() or "друг" in info.lower()

    assert "ℹ️" in BTN_INFO
    assert "🛡️" in BTN_PRIVACY
    assert "📜" in BTN_TERMS

    import json

    kb = main_menu_keyboard(settings.cabinet_url)
    data = json.loads(kb)
    labels = [btn["action"]["label"] for row in data["buttons"] for btn in row]
    assert BTN_HELP in labels
    assert BTN_GUIDE in labels

    ik = info_inline_keyboard()
    idata = json.loads(ik)
    ilabels = [
        (btn["action"].get("label") or "")
        for row in idata["buttons"]
        for btn in row
    ]
    assert any("Приватность" in x for x in ilabels)
    assert any("Рефералка" in x for x in ilabels)
    assert info_keyboard()  # reply — только «Назад»

def test_miniapp_files() -> None:
    root = Path(__file__).resolve().parents[1] / "miniapp" / "legal"
    for name in ("index.html", "privacy.html", "terms.html", "offer.html", "rules.html", "faq.html"):
        path = root / name
        assert path.is_file(), name
        html = path.read_text(encoding="utf-8")
        assert "Paskod" in html
        assert len(html) > 200


def main() -> None:
    test_style()
    test_docs()
    test_menus_and_copy()
    test_miniapp_files()
    print("test_ui_legal: OK")


if __name__ == "__main__":
    main()
