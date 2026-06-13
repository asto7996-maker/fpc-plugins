"""
Хранилище настроек плагинов (как storage/plugins в FPC, но в SQLite).
"""

from __future__ import annotations

import json
from typing import Any


class PluginSettingsStore:
    """Per-plugin JSON settings."""

    def __init__(self, db) -> None:
        self._db = db

    async def get(self, plugin_uuid: str, key: str, default: Any = None) -> Any:
        data = await self.get_all(plugin_uuid)
        return data.get(key, default)

    async def get_all(self, plugin_uuid: str) -> dict[str, Any]:
        async with __import__("aiosqlite").connect(self._db.path) as conn:
            cur = await conn.execute(
                "SELECT config_json FROM plugin_settings WHERE plugin_uuid = ?",
                (plugin_uuid,),
            )
            row = await cur.fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0]) or {}
        except Exception:
            return {}

    async def set(self, plugin_uuid: str, key: str, value: Any) -> None:
        data = await self.get_all(plugin_uuid)
        data[key] = value
        await self.save_all(plugin_uuid, data)

    async def save_all(self, plugin_uuid: str, data: dict[str, Any]) -> None:
        import time

        blob = json.dumps(data, ensure_ascii=False)
        async with __import__("aiosqlite").connect(self._db.path) as conn:
            await conn.execute(
                """
                INSERT INTO plugin_settings (plugin_uuid, config_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(plugin_uuid) DO UPDATE SET
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (plugin_uuid, blob, int(time.time())),
            )
            await conn.commit()

    async def delete_plugin(self, plugin_uuid: str) -> None:
        async with __import__("aiosqlite").connect(self._db.path) as conn:
            await conn.execute("DELETE FROM plugin_settings WHERE plugin_uuid = ?", (plugin_uuid,))
            await conn.commit()
