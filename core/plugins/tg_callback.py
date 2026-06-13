"""Кодирование callback_data для настроек плагинов (лимит Telegram — 64 байта)."""

from __future__ import annotations

from typing import Any


def field_cb(prefix: str, uuid: str, index: int) -> str:
    return f"{prefix}{uuid}:{index}"


def parse_field_cb(payload: str, prefix: str) -> tuple[str, int]:
    body = payload.replace(prefix, "", 1)
    uuid, _, idx_s = body.partition(":")
    return uuid, int(idx_s)


def parse_select_set_cb(payload: str, prefix: str) -> tuple[str, int, int]:
    body = payload.replace(prefix, "", 1)
    parts = body.split(":")
    if len(parts) < 3:
        raise ValueError("invalid select callback")
    return parts[0], int(parts[1]), int(parts[2])


def field_key_by_index(instance: Any, index: int) -> str | None:
    schema = instance.get_settings_schema() if hasattr(instance, "get_settings_schema") else []
    if 0 <= index < len(schema):
        return schema[index].get("key")
    return None


def field_index_by_key(instance: Any, key: str) -> int | None:
    schema = instance.get_settings_schema() if hasattr(instance, "get_settings_schema") else []
    for idx, field in enumerate(schema):
        if field.get("key") == key:
            return idx
    return None
