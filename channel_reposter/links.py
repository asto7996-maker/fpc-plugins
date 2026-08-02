"""
links.py — разбор ссылок на посты Telegram (без зависимостей от config).
"""

from __future__ import annotations

import re

# https://t.me/c/123456789/500
_LINK_PRIVATE = re.compile(
    r"(?:https?://)?t\.me/c/(\d+)/(\d+)", re.IGNORECASE
)
# https://t.me/channel_username/500
_LINK_PUBLIC = re.compile(
    r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)", re.IGNORECASE
)
# https://t.me/c/123456789  |  https://t.me/c/123456789/500 (как ссылка на канал)
_CHANNEL_PRIVATE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/c/(\d+)"
    r"(?:/\d+)?/??(?:\?.*)?$",
    re.IGNORECASE,
)
# https://t.me/channel_username  |  @https://t.me/channel_username/...
_CHANNEL_URL = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:s/)?([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_RESERVED_USERNAMES = frozenset(
    {"c", "s", "joinchat", "addstickers", "share", "proxy", "socks", "iv"}
)

# Каноническое значение в БД / настройках для Saved Messages
SAVED_MESSAGES = "me"

# «избранное», me, saved… (после нормализации пробелов/подчёркиваний)
_SAVED_ALIASES = frozenset(
    {
        "me",
        "self",
        "saved",
        "savedmessages",
        "savedmessage",
        "избранное",
        "избранные",
        "избранноесообщения",
        "избранныесообщения",
        "избранноесообщение",
    }
)


def _alias_key(raw: str) -> str:
    return re.sub(r"[\s_\-]+", "", raw.strip().lower())


def is_saved_messages(value: str | int | None) -> bool:
    """True, если значение означает Избранное (Saved Messages)."""
    if value is None or isinstance(value, int):
        return False
    raw = str(value).strip().lstrip("@")
    if not raw:
        return False
    if raw.lower() in ("me", "self", SAVED_MESSAGES):
        return True
    return _alias_key(raw) in _SAVED_ALIASES


def format_channel_label(value: str | int | None) -> str:
    """Человекочитаемая подпись канала для статуса/ответов."""
    if value is None or value == "":
        return "—"
    if is_saved_messages(value):
        return "⭐ Избранное"
    return str(value)


def to_channel_chat_id(value: str | int) -> str:
    """
    Привести числовой id к полному chat_id канала (-100…).

    Примеры:
      35839961           → -10035839961   (id из ссылки t.me/c/35839961/…)
      -10035839961       → -10035839961
      10035839961        → -10035839961   (Bot API id без минуса)
      -123456789         → -123456789     (уже отрицательный — как есть)
    """
    if isinstance(value, int):
        if value > 0:
            return to_channel_chat_id(str(value))
        return str(value)

    raw = str(value).strip()
    if not raw or not raw.lstrip("-").isdigit():
        raise ValueError(f"Не числовой id канала: {value!r}")

    if raw.startswith("-"):
        return raw
    # Уже полный Bot API id без минуса: 100 + internal
    if re.fullmatch(r"100\d{6,}", raw):
        return f"-{raw}"
    return f"-100{raw}"


def normalize_channel(value: str | int | None) -> str:
    """
    Привести канал к виду, понятному Telegram API.

    Примеры:
      @name / name / https://t.me/name / @https://t.me/name/123
      → @name
      35839961 / https://t.me/c/35839961 → -10035839961
      -10035839961 → -10035839961
      избранное / me / saved → me
    """
    if value is None:
        return ""
    if isinstance(value, int):
        return to_channel_chat_id(value)

    raw = str(value).strip()
    if not raw:
        return ""

    if is_saved_messages(raw):
        return SAVED_MESSAGES

    # Числовой id канала/чата (в т.ч. короткий id закрытого канала)
    if raw.lstrip("-").isdigit():
        return to_channel_chat_id(raw)

    # Снять ведущий @ (часто остаётся от «@https://t.me/...»)
    candidate = raw[1:] if raw.startswith("@") else raw

    # Закрытый канал: t.me/c/<internal_id>[/<msg_id>]
    m = _CHANNEL_PRIVATE.search(candidate)
    if m:
        return to_channel_chat_id(m.group(1))

    m = _CHANNEL_URL.search(candidate)
    if m:
        username = m.group(1)
        if username.lower() in _RESERVED_USERNAMES:
            raise ValueError(f"Некорректная ссылка на канал: {raw}")
        return f"@{username}"

    # Уже username без URL
    username = candidate.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", username):
        return f"@{username}"

    raise ValueError(
        "Не удалось распознать канал. Ожидается @username, числовой id "
        "(например 35839961), https://t.me/c/35839961 или «избранное»/me"
    )


def parse_post_link(link: str) -> tuple[str | int, int]:
    """
    Разобрать ссылку на пост Telegram.

    Returns:
        (chat_ref, message_id) где chat_ref — int ID (-100...) или username (str).

    Raises:
        ValueError: если ссылка не распознана.
    """
    link = link.strip()

    m = _LINK_PRIVATE.search(link)
    if m:
        # Приватные каналы: t.me/c/<internal_id>/<msg_id>
        # Полный chat_id = -100 + internal_id
        internal_id = int(m.group(1))
        message_id = int(m.group(2))
        chat_id = int(to_channel_chat_id(internal_id))
        return chat_id, message_id

    m = _LINK_PUBLIC.search(link)
    if m:
        username = m.group(1)
        if username.lower() in _RESERVED_USERNAMES:
            raise ValueError(f"Некорректная ссылка на пост: {link}")
        message_id = int(m.group(2))
        return username, message_id

    raise ValueError(
        "Не удалось разобрать ссылку. Ожидается формат:\n"
        "• https://t.me/c/123456789/500\n"
        "• https://t.me/channel_username/500"
    )
