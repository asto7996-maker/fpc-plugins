"""
transfer_filters.py — отдельная функция чистого перелива канала в канал.

При переносе контента игнорируем:
  • ссылки (URL, text_link, превью web_page, http(s)/t.me в тексте);
  • файлы (document);
  • гифки (animation и gif-документы);
  • голосовые (voice).

Фото, видео, кружки, стикеры и обычный текст без ссылок — оставляем.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# Явные ссылки: http(s), tg://, www., t.me / telegram.me
_URL_RE = re.compile(
    r"(?:https?://|ftp://|tg://|www\.|(?:t\.me|telegram\.me|telegram\.dog)/)\S+",
    re.IGNORECASE,
)

_LINK_ENTITY_TYPES = frozenset(
    {
        "url",
        "textlink",
        "texturl",
        "text_link",
        "text_url",
        "messageentityurl",
        "messageentitytexturl",
        "messageentitytextlink",
    }
)

REASON_LINK = "ссылка"
REASON_FILE = "файл"
REASON_GIF = "гифка"
REASON_VOICE = "голосовое"


def _entity_type_name(entity: Any) -> str:
    raw = getattr(entity, "type", entity)
    name = getattr(raw, "name", None) or getattr(raw, "value", None) or str(raw)
    return (
        str(name)
        .replace("MessageEntityType.", "")
        .replace("MessageEntity", "")
        .replace("_", "")
        .lower()
    )


def _plain_text(msg: Any) -> str:
    parts: list[str] = []
    for attr in ("text", "caption"):
        val = getattr(msg, attr, None)
        if val is None:
            continue
        parts.append(str(val))
    return "\n".join(parts)


def _iter_entities(msg: Any) -> Iterable[Any]:
    for attr in ("entities", "caption_entities"):
        items = getattr(msg, attr, None)
        if items:
            yield from items


def _has_web_preview(msg: Any) -> bool:
    return bool(getattr(msg, "web_page", None) or getattr(msg, "webpage", None))


def _is_gif_document(doc: Any) -> bool:
    if doc is None:
        return False
    mime = (getattr(doc, "mime_type", None) or "").lower()
    name = (getattr(doc, "file_name", None) or "").lower()
    if mime == "image/gif" or name.endswith(".gif"):
        return True
    for attr in getattr(doc, "attributes", None) or []:
        type_name = type(attr).__name__.lower()
        if "animated" in type_name or getattr(attr, "animated", False):
            return True
    return False


def ignored_link_reason(msg: Any) -> Optional[str]:
    """Почему пост считается ссылкой, либо None."""
    if _has_web_preview(msg):
        return REASON_LINK
    for entity in _iter_entities(msg):
        if _entity_type_name(entity) in _LINK_ENTITY_TYPES:
            return REASON_LINK
        if getattr(entity, "url", None):
            return REASON_LINK
    text = _plain_text(msg)
    if text and _URL_RE.search(text):
        return REASON_LINK
    return None


def ignored_media_reason(msg: Any) -> Optional[str]:
    """Почему медиа игнорируем (файл / гифка / голосовое), либо None."""
    if getattr(msg, "voice", None):
        return REASON_VOICE
    if getattr(msg, "animation", None):
        return REASON_GIF
    doc = getattr(msg, "document", None)
    if not doc:
        return None
    if _is_gif_document(doc):
        return REASON_GIF
    return REASON_FILE


def transfer_skip_reason(msg: Any) -> Optional[str]:
    """
    Отдельная функция перелива: почему этот пост не копируем.

    Возвращает короткую причину («ссылка», «файл», «гифка», «голосовое»)
    или None, если пост можно публиковать.
    """
    return ignored_link_reason(msg) or ignored_media_reason(msg)


def should_skip_transfer(msg: Any) -> bool:
    """True, если при чистом переливе пост нужно пропустить."""
    return transfer_skip_reason(msg) is not None


def filter_album_for_transfer(messages: list[Any]) -> tuple[list[Any], Optional[str]]:
    """
    Фильтр альбома для чистого перелива.

    Если в любом элементе есть ссылка — пропускаем весь альбом.
    Файлы / гифки / голосовые выкидываем поштучно.
    Возвращает (оставленные сообщения, причина пропуска всего альбома).
    """
    if not messages:
        return [], "пусто"

    for msg in messages:
        reason = ignored_link_reason(msg)
        if reason:
            return [], reason

    kept = [msg for msg in messages if ignored_media_reason(msg) is None]
    if not kept:
        return [], ignored_media_reason(messages[0]) or "файл"
    return kept, None
