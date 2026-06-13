"""
Fluent-локализация (ru/en).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("starvell.i18n")

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
_translator = None


def setup_i18n(locale: str = "ru") -> None:
    global _translator
    try:
        from fluentogram import TranslatorHub

        hub = TranslatorHub(
            locales=["ru", "en"],
            translators={
                "ru": ("ru", "main"),
                "en": ("en", "main"),
            },
            locale_dir=str(LOCALES_DIR),
        )
        _translator = hub.get_translator_by_locale(locale)
        logger.info("i18n: locale=%s", locale)
    except ImportError:
        logger.warning("fluentogram не установлен — используются строки по умолчанию")
        _translator = None
    except Exception as exc:
        logger.warning("i18n init failed: %s", exc)
        _translator = None


def t(key: str, **kwargs) -> str:
    """Получить локализованную строку."""
    if _translator is None:
        return _FALLBACK.get(key, key).format(**kwargs) if kwargs else _FALLBACK.get(key, key)
    try:
        return _translator.get(key, **kwargs)
    except Exception:
        return _FALLBACK.get(key, key)


_FALLBACK: dict[str, str] = {
    "menu-title": "🤖 <b>Starvell Cardinal</b>",
    "menu-subtitle": "Премиум-панель управления магазином",
    "btn-back": "◀️ Назад",
    "btn-home": "🏠 Главное меню",
    "btn-refresh": "🔄 Обновить",
    "loading": "⚡️ Запрос обрабатывается…",
    "plugins-title": "🔌 Панель плагинов",
    "status-on": "🟢 Включено",
    "status-off": "🔴 Выключено",
}
