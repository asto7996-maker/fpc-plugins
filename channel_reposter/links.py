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
        if username.lower() in {"c", "s", "joinchat", "addstickers"}:
            raise ValueError(f"Некорректная ссылка на пост: {link}")
        message_id = int(m.group(2))
        return username, message_id

    raise ValueError(
        "Не удалось разобрать ссылку. Ожидается формат:\n"
        "• https://t.me/c/123456789/500\n"
        "• https://t.me/channel_username/500"
    )
