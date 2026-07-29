"""
formatting.py — сохранение жирного / курсива / ссылок без ручной HTML-вёрстки.

Админ просто пишет сообщение в Telegram как обычно (выделяет текст,
вставляет ссылку «в слово»). Мы сохраняем готовый HTML из entities.
"""

from __future__ import annotations

import html
import re
from typing import Optional

from aiogram.types import Message

# Разрешённые теги Telegram HTML
_ALLOWED_TAG = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|tg-spoiler)\b[^>]*>"
    r"|<a\s+href=\"[^\"]*\">|</a>",
    re.IGNORECASE,
)


def extract_caption_html(message: Message) -> str:
    """
    Достать подпись/текст с форматированием.

    Приоритет:
      1) message.html_text / html_caption — Telegram entities → HTML
      2) сырой text/caption (будет экранирован при необходимости)
    """
    # aiogram сам собирает HTML из entities (bold/italic/text_link/…)
    rich = message.html_text or getattr(message, "html_caption", None)
    if rich and rich.strip():
        return rich.strip()

    raw = (message.text or message.caption or "").strip()
    return raw


def looks_like_html(text: str) -> bool:
    return bool(re.search(r"</?[a-zA-Z][^>]*>", text or ""))


def safe_preview(text: str, limit: int = 300) -> str:
    """Превью для статуса: экранируем, чтобы битый HTML не ломал сообщение."""
    plain = text or ""
    if len(plain) > limit:
        plain = plain[:limit] + "…"
    return html.escape(plain)


def validate_telegram_html(text: str) -> Optional[str]:
    """
    Грубая проверка парности популярных тегов.
    Возвращает текст ошибки или None, если ок.
    """
    if not text:
        return None
    if not looks_like_html(text):
        return None

    # Считаем простые парные теги
    pairs = ("b", "strong", "i", "em", "u", "s", "code", "pre", "a")
    for tag in pairs:
        opens = len(re.findall(rf"<{tag}\b", text, flags=re.I))
        closes = len(re.findall(rf"</{tag}>", text, flags=re.I))
        if opens != closes:
            return (
                f"Непарный тег &lt;{tag}&gt; (открыто {opens}, закрыто {closes}). "
                "Лучше просто отправьте текст с форматированием Telegram — "
                "бот сам соберёт HTML."
            )
    return None
