"""Хелперы чатов Starvell (порт Lumus Starvell Bot)."""

from __future__ import annotations

from typing import Any


def message_text(msg: dict[str, Any]) -> str:
    content = str(msg.get("content") or msg.get("text") or "").strip()
    if content:
        return content
    images = msg.get("images") or []
    if isinstance(images, list) and images:
        return "📷 Фото"
    return ""


def message_author_id(msg: dict[str, Any]) -> int | None:
    author_id = msg.get("authorId") or msg.get("author")
    if author_id is None:
        return None
    try:
        return int(author_id)
    except (TypeError, ValueError):
        return None


def is_auto_message(msg: dict[str, Any]) -> bool:
    metadata = msg.get("metadata") or {}
    return bool(metadata.get("isAuto"))


def extract_interlocutor(chat: dict[str, Any], my_user_id: int | None) -> tuple[str, int | None]:
    participants = chat.get("participants") or []
    username = "Покупатель"
    interlocutor_id: int | None = None
    for p in participants:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        try:
            if pid is not None and (my_user_id is None or int(pid) != int(my_user_id)):
                username = str(p.get("username") or "Покупатель")
                interlocutor_id = int(pid)
                break
        except (TypeError, ValueError):
            continue
    return username, interlocutor_id
