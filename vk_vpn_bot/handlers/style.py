"""
Визуальный стиль сообщений Paskod VK.

Спокойное оформление без декоративных линий и редких Unicode-символов.
VK не поддерживает HTML в обычных сообщениях — только эмодзи и простой текст.
"""

from __future__ import annotations

# Mathematical Sans-Serif Bold — для латиницы/цифр в бренде и кодах
_LATIN_BOLD = str.maketrans(
    {
        **{c: chr(0x1D5D4 + i) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
        **{c: chr(0x1D5EE + i) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")},
        **{c: chr(0x1D7EC + i) for i, c in enumerate("0123456789")},
    }
)


def brand(text: str) -> str:
    """Премиум-начертание для бренда / кодов (латиница и цифры)."""
    return text.translate(_LATIN_BOLD)


def card_rule() -> str:
    """Совместимость: разделители больше не рисуем."""
    return ""


def soft_rule() -> str:
    """Совместимость: разделители больше не рисуем."""
    return ""


def header(emoji: str, title: str) -> str:
    """Шапка экрана: эмодзи + заголовок."""
    return f"{emoji} {title}"


def subhead(emoji: str, title: str) -> str:
    return f"{emoji} {title}"


def bullet(text: str, mark: str = "•") -> str:
    return f"{mark} {text}"


def kv(key: str, value: str) -> str:
    return f"{key}: {value}"


def step(n: int, text: str) -> str:
    return f"{n}. {text}"


def footer_hint(text: str = "Выберите действие ниже") -> str:
    return text


def success_banner(text: str) -> str:
    return f"✅ {text}"


def warn_banner(text: str) -> str:
    return f"⚠️ {text}"


def error_banner(text: str) -> str:
    return f"❌ {text}"
