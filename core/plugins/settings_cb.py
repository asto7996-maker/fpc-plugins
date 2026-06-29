"""
Короткие callback_data для настроек плагинов (лимит Telegram — 64 байта).
"""

from __future__ import annotations

import logging
from typing import Any

from keyboards import cbt as CBT

logger = logging.getLogger("starvell.plugin_settings_cb")

MAX_CALLBACK_BYTES = 64


def _check(data: str) -> str:
    size = len(data.encode("utf-8"))
    if size > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data too long ({size}b): {data!r}")
    return data


def schema_field_by_index(schema: list[dict[str, Any]], idx: int) -> dict[str, Any] | None:
    if 0 <= idx < len(schema):
        return schema[idx]
    return None


def schema_index_of(schema: list[dict[str, Any]], key: str) -> int | None:
    for i, field in enumerate(schema):
        if field.get("key") == key:
            return i
    return None


def resolve_schema_field(inst: Any, token: str) -> dict[str, Any] | None:
    """token — индекс поля (новый формат) или key (legacy)."""
    schema = inst.get_settings_schema() if hasattr(inst, "get_settings_schema") else []
    if token.isdigit():
        return schema_field_by_index(schema, int(token))
    if hasattr(inst, "get_schema_field"):
        return inst.get_schema_field(token)
    return None


def parse_uuid_token(payload: str, prefix: str) -> tuple[str, str]:
    body = payload.replace(prefix, "", 1)
    uuid, _, token = body.partition(":")
    return uuid, token


def cb_setting_toggle(uuid: str, field_idx: int) -> str:
    return _check(f"{CBT.PLUGIN_SETTING}{uuid}:{field_idx}")


def cb_setting_edit(uuid: str, field_idx: int) -> str:
    return _check(f"{CBT.PLUGIN_EDIT}{uuid}:{field_idx}")


def cb_select_menu(uuid: str, field_idx: int) -> str:
    return _check(f"{CBT.PLUGIN_SELECT_MENU}{uuid}:{field_idx}")


def cb_select_set(uuid: str, field_idx: int, opt_idx: int) -> str:
    return _check(f"{CBT.PLUGIN_SELECT_SET}{uuid}:{field_idx}:{opt_idx}")


def cb_schema_action(uuid: str, field_idx: int) -> str:
    return _check(f"{CBT.PLUGIN_SCHEMA_ACT}{uuid}:{field_idx}")


def cb_settings_page(uuid: str, page: int) -> str:
    return _check(f"{CBT.PLUGIN_SETTINGS}{uuid}:{page}")
