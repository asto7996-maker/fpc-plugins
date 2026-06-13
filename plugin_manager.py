"""
Система плагинов — обратная совместимость.
Используйте core.plugins.manager.PluginEngine для новых плагинов.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any

from core.plugins.manager import PluginEngine, PluginRecord


@dataclass
class PluginMeta:
    """Legacy alias для PluginRecord."""
    name: str
    uuid: str
    version: str
    description: str
    credits: str
    path: str
    module: ModuleType | None
    enabled: bool
    instance: Any | None = None
    load_error: str | None = None


class PluginContext:
    """Контекст, передаваемый плагинам при событиях."""

    def __init__(self, api: Any, db: Any, settings: Any, account_name: str = "default"):
        self.api = api
        self.db = db
        self.settings = settings
        self.account_name = account_name
        self.chat_id: str | None = None
        self.message_author_id: int | None = None


class PluginManager(PluginEngine):
    """Alias PluginManager → PluginEngine (hot-reload, BasePlugin)."""

    def _record_to_meta(self, rec: PluginRecord) -> PluginMeta:
        return PluginMeta(
            name=rec.name,
            uuid=rec.uuid,
            version=rec.version,
            description=rec.description,
            credits=rec.credits,
            path=rec.path,
            module=rec.module,
            enabled=rec.enabled,
            instance=rec.instance,
            load_error=rec.load_error,
        )


from core.bot_core import EventBus, EventManager

__all__ = ["PluginManager", "PluginEngine", "PluginContext", "PluginMeta", "EventManager", "EventBus"]
