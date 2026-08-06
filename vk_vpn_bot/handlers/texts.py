"""
Инструкции по ОС — выразительно и по шагам.
"""

from __future__ import annotations

from handlers.style import footer_hint, header, soft_rule, step

GUIDE_INTRO = (
    f"{header('📖', 'Гайд по подключению')}\n\n"
    f"Выберите систему — пришлю короткие шаги.\n\n"
    f"{footer_hint('кнопки ниже')}"
)

GUIDES: dict[str, str] = {
    "ios": (
        f"{header('🍎', 'iOS')}\n\n"
        f"{step(1, 'Установите Happ из App Store')}\n"
        f"{step(2, 'Скопируйте ключ из бота')}\n"
        f"{step(3, 'В Happ: «+» → из буфера')}\n"
        f"{step(4, 'Включите профиль и разрешите VPN')}\n\n"
        f"{soft_rule()}\n"
        f"✨ Готово — можно пользоваться."
    ),
    "android": (
        f"{header('🤖', 'Android')}\n\n"
        f"{step(1, 'Установите Happ (или v2rayNG / Hiddify)')}\n"
        f"{step(2, 'Скопируйте ключ из бота')}\n"
        f"{step(3, '«+» → импорт из буфера')}\n"
        f"{step(4, 'Подключитесь')}\n\n"
        f"{soft_rule()}\n"
        f"Если трафик не идёт — смените режим маршрутизации."
    ),
    "windows": (
        f"{header('💻', 'Windows')}\n\n"
        f"{step(1, 'Скачайте Happ / Hiddify / v2rayN')}\n"
        f"{step(2, 'Скопируйте ключ из бота')}\n"
        f"{step(3, 'Добавьте профиль из буфера')}\n"
        f"{step(4, 'Connect')}\n\n"
        f"{soft_rule()}\n"
        f"Для браузера удобен System Proxy."
    ),
    "macos": (
        f"{header('🖥', 'macOS')}\n\n"
        f"{step(1, 'Установите Happ / Streisand / Hiddify')}\n"
        f"{step(2, 'Скопируйте ключ из бота')}\n"
        f"{step(3, 'Импортируйте конфиг из буфера')}\n"
        f"{step(4, 'Подключитесь и подтвердите VPN')}\n\n"
        f"{soft_rule()}\n"
        f"Запрос пароля системы при установке — это нормально."
    ),
}

OS_TITLES = {
    "ios": "🍎 iOS",
    "android": "🤖 Android",
    "windows": "💻 Windows",
    "macos": "🖥 macOS",
}
