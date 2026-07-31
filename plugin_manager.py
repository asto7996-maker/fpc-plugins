"""
Система плагинов Starvell Cardinal (аналог FunPay Cardinal).
Плагины — одиночные .py файлы в каталоге plugins/ с классом Plugin.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable

from config import PLUGINS_DIR, PLUGIN_STATE_PATH

logger = logging.getLogger("starvell.plugins")


@dataclass
class PluginMeta:
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


class EventManager:
    """Менеджер событий (как в FunPay Cardinal)."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}

    def register_handler(self, event: str, handler: Callable) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def unregister_handler(self, event: str, handler: Callable) -> None:
        if event in self._handlers:
            self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    async def dispatch(self, event: str, data: dict[str, Any]) -> None:
        for handler in self._handlers.get(event, []):
            try:
                result = handler(data)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.exception("Handler %s for %s failed: %s", handler, event, exc)


class CardinalCore:
    """Ядро бота — точка доступа для плагинов (аналог cardinal в FunPay Cardinal)."""

    def __init__(self, settings: Any, db: Any, event_manager: EventManager) -> None:
        self.settings = settings
        self.db = db
        self.event_manager = event_manager
        self.logger = logging.getLogger("starvell.cardinal")
        self._apis: dict[str, Any] = {}
        self._send_message_cb: Callable | None = None

    def register_api(self, account_name: str, api: Any) -> None:
        self._apis[account_name] = api

    def get_api(self, account_name: str = "default") -> Any | None:
        return self._apis.get(account_name)

    def set_send_message_callback(self, cb: Callable) -> None:
        self._send_message_cb = cb

    async def send_message(self, chat_id: str, text: str, account_name: str = "default") -> None:
        api = self.get_api(account_name)
        if api:
            await api.send_message(chat_id, text)
        if self._send_message_cb:
            await self._send_message_cb(chat_id, text, account_name)


class PluginManager:
    """Загрузка и управление плагинами."""

    def __init__(self, cardinal: CardinalCore, plugins_dir: str | None = None, state_path: str | None = None):
        self.cardinal = cardinal
        self.root_dir = plugins_dir or str(PLUGINS_DIR)
        self.state_path = state_path or str(PLUGIN_STATE_PATH)
        self.plugins: dict[str, PluginMeta] = {}
        self.disabled: set[str] = set()
        self._logger = logger

    def _ensure_dirs(self) -> None:
        os.makedirs(self.root_dir, exist_ok=True)
        state_dir = os.path.dirname(self.state_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        abs_plugins = os.path.abspath(self.root_dir)
        if abs_plugins not in sys.path:
            sys.path.insert(0, abs_plugins)

    def _load_state(self) -> None:
        self._ensure_dirs()
        if not os.path.exists(self.state_path):
            self.disabled = set()
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            self.disabled = {str(x) for x in (data.get("disabled") or [])}
        except Exception:
            self.disabled = set()

    def _save_state(self) -> None:
        self._ensure_dirs()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"disabled": sorted(self.disabled)}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def _import_module(self, file_path: str) -> ModuleType:
        stem = os.path.splitext(os.path.basename(file_path))[0]
        mod_name = f"plugins.{stem}"
        spec = importlib.util.spec_from_file_location(mod_name, file_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load spec for {file_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module

    def _extract_meta(self, file_path: str) -> dict[str, str]:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read(8000)
        except Exception:
            return {}
        pat = re.compile(r'^\s*(NAME|UUID|VERSION|DESCRIPTION|CREDITS)\s*=\s*["\'](.+?)["\']\s*$', re.MULTILINE)
        return {m.group(1): m.group(2) for m in pat.finditer(text)}

    def load_all(self) -> list[PluginMeta]:
        """Сканирует каталог plugins/ и загружает все плагины."""
        self._load_state()
        self.plugins.clear()
        self._ensure_dirs()

        for fname in sorted(os.listdir(self.root_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            path = os.path.join(self.root_dir, fname)
            try:
                meta = self._load_one(path)
                self.plugins[meta.uuid] = meta
            except Exception as exc:
                self._logger.error("Failed to load %s: %s", fname, exc)

        return list(self.plugins.values())

    def _load_one(self, file_path: str) -> PluginMeta:
        inline = self._extract_meta(file_path)
        try:
            module = self._import_module(file_path)
            name = getattr(module, "NAME", None) or inline.get("NAME") or os.path.basename(file_path)
            uuid = getattr(module, "UUID", None) or inline.get("UUID") or name
            version = getattr(module, "VERSION", None) or inline.get("VERSION") or "1.0.0"
            description = getattr(module, "DESCRIPTION", None) or inline.get("DESCRIPTION") or ""
            credits = getattr(module, "CREDITS", None) or inline.get("CREDITS") or ""
        except Exception as exc:
            uuid = inline.get("UUID") or os.path.basename(file_path)
            return PluginMeta(
                name=inline.get("NAME", file_path),
                uuid=uuid,
                version="?",
                description="",
                credits="",
                path=file_path,
                module=None,
                enabled=False,
                load_error=str(exc),
            )

        enabled = uuid not in self.disabled
        instance = None
        if enabled and hasattr(module, "Plugin"):
            try:
                config = self.cardinal.settings.__dict__ if hasattr(self.cardinal.settings, "__dict__") else {}
                instance = module.Plugin(self.cardinal, config)
                if hasattr(instance, "setup"):
                    instance.setup()
            except Exception as exc:
                self._logger.error("Plugin %s setup failed: %s", name, exc)
                return PluginMeta(
                    name=name, uuid=uuid, version=version, description=description,
                    credits=credits, path=file_path, module=module, enabled=False, load_error=str(exc),
                )

        return PluginMeta(
            name=name, uuid=uuid, version=version, description=description,
            credits=credits, path=file_path, module=module, enabled=enabled, instance=instance,
        )

    def toggle(self, uuid: str) -> bool:
        if uuid in self.disabled:
            self.disabled.discard(uuid)
            enabled = True
        else:
            self.disabled.add(uuid)
            enabled = False
        self._save_state()
        self.load_all()
        return enabled

    def unload_all(self) -> None:
        for meta in self.plugins.values():
            if meta.instance and hasattr(meta.instance, "unload"):
                try:
                    meta.instance.unload()
                except Exception as exc:
                    self._logger.error("Unload %s failed: %s", meta.name, exc)
