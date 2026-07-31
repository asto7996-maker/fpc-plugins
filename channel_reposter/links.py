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
# https://t.me/channel_username  |  @https://t.me/channel_username/...
_CHANNEL_URL = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:s/)?([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_RESERVED_USERNAMES = frozenset(
    {"c", "s", "joinchat", "addstickers", "share", "proxy", "socks", "iv"}
)


def normalize_channel(value: str | int | None) -> str:
    """
    Привести канал к виду, понятному Telegram API.

    Примеры:
      @name / name / https://t.me/name / @https://t.me/name/123
      → @name
      -100123… / 123… → строка с id
    """
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)

    raw = str(value).strip()
    if not raw:
        return ""

    # Числовой id канала/чата
    if raw.lstrip("-").isdigit():
        return raw

    # Снять ведущий @ (часто остаётся от «@https://t.me/...»)
    candidate = raw[1:] if raw.startswith("@") else raw

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
        "Не удалось распознать канал. Ожидается @username или https://t.me/username"
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
        chat_id = int(f"-100{internal_id}")
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
