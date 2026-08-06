"""
Тесты гайдов: полнота, лимиты VK и связность с кнопками.
Запуск из папки vk_vpn_bot:
    python3 tests/test_guides.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.texts import GUIDE_INTRO, GUIDES, OS_TITLES
from keyboards.menus import guide_os_keyboard

VK_MESSAGE_LIMIT = 4096


def test_all_platforms_covered() -> None:
    assert {"ios", "android", "windows", "macos", "trouble"} <= set(GUIDES)
    assert set(GUIDES) == set(OS_TITLES), "заголовки и тексты разошлись"


def test_fits_vk_limit() -> None:
    assert len(GUIDE_INTRO) < VK_MESSAGE_LIMIT
    for slug, text in GUIDES.items():
        assert len(text) < VK_MESSAGE_LIMIT, f"{slug}: {len(text)}"


def test_install_guides_are_actionable() -> None:
    """В каждом гайде по ОС должно быть где взять клиент, шаги и проверка."""
    for slug in ("ios", "android", "windows", "macos"):
        text = GUIDES[slug]
        assert "Happ" in text, slug
        assert "🔑 Мой ключ" in text, f"{slug}: не сказано, где взять ключ"
        assert "буфер" in text.lower(), f"{slug}: нет шага добавления"
        assert "2ip.ru" in text, f"{slug}: нет способа проверить результат"
        # Нумерованные шаги — иначе это не инструкция
        assert "①" in text and "④" in text, f"{slug}: нет пошаговой части"

    # Источники установки названы конкретно
    assert "App Store" in GUIDES["ios"]
    assert "Google Play" in GUIDES["android"] and "happ.su" in GUIDES["android"]
    assert "happ.su" in GUIDES["windows"]
    assert "App Store" in GUIDES["macos"]


def test_platform_specific_pitfalls() -> None:
    """Каждая ОС имеет свои грабли — они должны быть описаны."""
    assert "код-пароль" in GUIDES["ios"]
    assert "Батарея" in GUIDES["android"]
    assert "TUN" in GUIDES["windows"] and "System Proxy" in GUIDES["windows"]
    assert "Конфиденциальность и безопасность" in GUIDES["macos"]


def test_troubleshooting_covers_real_symptoms() -> None:
    trouble = GUIDES["trouble"]
    for marker in (
        "App not supported",   # так отвечает неактивная подписка
        "маршрутизации",       # трафик не идёт
        "пустой",              # добавлено как сервер, а не подписка
        "рвётся",              # обрывы соединения
        "Задать вопрос",       # куда идти, если ничего не помогло
    ):
        assert marker in trouble, marker


def test_intro_explains_subscription_model() -> None:
    for marker in ("подписк", "sub.paskod.ru", "QR"):
        assert marker in GUIDE_INTRO, marker


def test_keyboard_matches_guides() -> None:
    data = json.loads(guide_os_keyboard())
    payloads = [
        json.loads(b["action"]["payload"])
        if isinstance(b["action"].get("payload"), str)
        else b["action"].get("payload", {})
        for row in data["buttons"]
        for b in row
    ]
    slugs = {p.get("os") for p in payloads if p.get("cmd") == "guide"}
    assert slugs == set(GUIDES), f"кнопки и гайды разошлись: {slugs ^ set(GUIDES)}"
    assert len(data["buttons"]) <= 6, "инлайн-клавиатура VK: не больше 6 строк"


def main() -> None:
    test_all_platforms_covered()
    test_fits_vk_limit()
    test_install_guides_are_actionable()
    test_platform_specific_pitfalls()
    test_troubleshooting_covers_real_symptoms()
    test_intro_explains_subscription_model()
    test_keyboard_matches_guides()
    print("test_guides: OK")


if __name__ == "__main__":
    main()
