"""
Plugin Engine — горячая загрузка/перезагрузка плагинов без рестарта бота.
Поддерживает BasePlugin и legacy class Plugin (FPC).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable

from config import PLUGINS_DIR, PLUGIN_STATE_PATH
from core.plugins.base import BasePlugin
from core.plugins.scheduler import TaskScheduler

logger = logging.getLogger("starvell.plugins.engine")


@dataclass
class PluginRecord:
    name: str
    uuid: str
    version: str
    description: str
    credits: str
    path: str
    module: ModuleType | None
    enabled: bool
    instance: BasePlugin | Any | None = None
    load_error: str | None = None
    loaded_at: float = field(default_factory=time.time)
    is_base_plugin: bool = False
    settings_callback: str | None = None
    hook_only: bool = False
    has_settings_page: bool = False
    pinned: bool = False


class PluginEngine:
    """Менеджер плагинов с hot-reload."""

    def __init__(self, core: Any, plugins_dir: str | None = None, state_path: str | None = None) -> None:
        self.core = core
        self.root_dir = plugins_dir or str(PLUGINS_DIR)
        self.state_path = state_path or str(PLUGIN_STATE_PATH)
        self.plugins: dict[str, PluginRecord] = {}
        self.disabled: set[str] = set()
        self._mtime_cache: dict[str, float] = {}
        self._watch_interval = 5.0

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

    def _import_module(self, file_path: str, force_reload: bool = False) -> ModuleType:
        stem = os.path.splitext(os.path.basename(file_path))[0]
        mod_name = f"plugins.{stem}"
        if force_reload and mod_name in sys.modules:
            del sys.modules[mod_name]
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
        pat = re.compile(
            r'^\s*(NAME|UUID|VERSION|DESCRIPTION|CREDITS|SETTINGS_CALLBACK|SETTINGS_PAGE)\s*=\s*["\']?(.+?)["\']?\s*$',
            re.MULTILINE,
        )
        return {m.group(1): m.group(2) for m in pat.finditer(text)}

    def _instantiate(self, module: ModuleType, meta: dict[str, str]) -> tuple[Any | None, bool, str | None]:
        config = getattr(self.core.settings, "__dict__", {}) if hasattr(self.core.settings, "__dict__") else {}
        if hasattr(module, "Plugin"):
            try:
                inst = module.Plugin(self.core, config)
                is_base = isinstance(inst, BasePlugin) or (
                    hasattr(module, "Plugin") and issubclass(module.Plugin, BasePlugin)
                )
                return inst, is_base, None
            except Exception as exc:
                is_base = hasattr(module, "Plugin") and issubclass(getattr(module, "Plugin"), BasePlugin)
                return None, is_base, str(exc)
        return None, False, None

    def load_all(self) -> list[PluginRecord]:
        self._load_state()
        self.plugins.clear()
        self._ensure_dirs()
        for fname in sorted(os.listdir(self.root_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            path = os.path.join(self.root_dir, fname)
            try:
                rec = self._load_one(path)
                self.plugins[rec.uuid] = rec
                self._mtime_cache[path] = os.path.getmtime(path)
            except Exception as exc:
                logger.error("Failed to load %s: %s", fname, exc)
        self.run_pre_init()
        return list(self.plugins.values())

    def run_pre_init(self) -> None:
        """BIND_TO_PRE_INIT — как в FunPay Cardinal при старте."""
        import asyncio

        async def _run():
            await self.dispatch_hook("BIND_TO_PRE_INIT", self.core)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_run())
            else:
                loop.run_until_complete(_run())
        except Exception as exc:
            logger.debug("PRE_INIT: %s", exc)

    def _load_one(self, file_path: str, force_reload: bool = False) -> PluginRecord:
        inline = self._extract_meta(file_path)
        try:
            module = self._import_module(file_path, force_reload=force_reload)
            name = getattr(module, "NAME", None) or inline.get("NAME") or os.path.basename(file_path)
            uuid = getattr(module, "UUID", None) or inline.get("UUID") or name
            version = getattr(module, "VERSION", None) or inline.get("VERSION") or "1.0.0"
            description = getattr(module, "DESCRIPTION", None) or inline.get("DESCRIPTION") or ""
            credits = getattr(module, "CREDITS", None) or inline.get("CREDITS") or ""
            settings_cb = getattr(module, "SETTINGS_CALLBACK", None) or inline.get("SETTINGS_CALLBACK")
            settings_page = getattr(module, "SETTINGS_PAGE", None)
            if settings_page is None:
                settings_page = inline.get("SETTINGS_PAGE", "True").lower() in ("true", "1", "yes")
            else:
                settings_page = bool(settings_page)
        except Exception as exc:
            uuid = inline.get("UUID") or os.path.basename(file_path)
            return PluginRecord(
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
        instance, is_base, err = None, False, None
        hooks = self._fpc_hooks(module) if module else {}
        has_hooks = any(hooks.values())

        if enabled:
            instance, is_base, err = self._instantiate(module, inline)
            if instance and not err:
                try:
                    if is_base:
                        instance.on_load()
                    elif hasattr(instance, "setup"):
                        instance.setup()
                except Exception as exc:
                    err = str(exc)
                    instance = None

        hook_only = has_hooks and instance is None and err is None
        can_run = enabled and (instance is not None or hook_only)

        if enabled and not can_run and not has_hooks:
            err = err or "No Plugin class and no BIND_TO_* hooks"

        return PluginRecord(
            name=name,
            uuid=uuid,
            version=version,
            description=description,
            credits=credits,
            path=file_path,
            module=module,
            enabled=can_run,
            instance=instance,
            load_error=err if not can_run else None,
            is_base_plugin=is_base,
            settings_callback=settings_cb,
            hook_only=hook_only,
            has_settings_page=settings_page and (is_base or bool(settings_cb)),
            pinned=False,
        )

    async def reload_plugin(self, uuid: str) -> PluginRecord | None:
        """Hot-reload одного плагина."""
        rec = self.plugins.get(uuid)
        if not rec:
            return None
        if rec.instance:
            try:
                if rec.is_base_plugin:
                    rec.instance.on_unload()
                elif hasattr(rec.instance, "unload"):
                    rec.instance.unload()
            except Exception as exc:
                logger.warning("unload %s: %s", uuid, exc)
        new_rec = self._load_one(rec.path, force_reload=True)
        self.plugins[uuid] = new_rec
        logger.info("Hot-reload плагина %s v%s", new_rec.name, new_rec.version)
        return new_rec

    async def reload_changed(self) -> list[str]:
        """Перезагружает плагины, чьи файлы изменились на диске."""
        reloaded: list[str] = []
        for uuid, rec in list(self.plugins.items()):
            if not os.path.isfile(rec.path):
                continue
            mtime = os.path.getmtime(rec.path)
            prev = self._mtime_cache.get(rec.path, 0)
            if mtime > prev:
                await self.reload_plugin(uuid)
                self._mtime_cache[rec.path] = mtime
                reloaded.append(uuid)
        return reloaded

    def _fpc_hooks(self, module: ModuleType) -> dict[str, list]:
        hooks: dict[str, list] = {}
        for attr in dir(module):
            if attr.startswith("BIND_TO_") and not attr.endswith("_DELETE"):
                funcs = getattr(module, attr)
                if callable(funcs):
                    funcs = [funcs]
                elif isinstance(funcs, list):
                    funcs = [f for f in funcs if callable(f)]
                else:
                    continue
                hooks[attr] = funcs
        return hooks

    async def dispatch_hook(self, hook: str, core: Any, *args, **kwargs) -> None:
        for rec in self.plugins.values():
            if not rec.enabled or not rec.module:
                continue
            fpc = self._fpc_hooks(rec.module)
            for fn in fpc.get(hook, []):
                try:
                    result = fn(core, *args, **kwargs)
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    logger.exception("Plugin %s hook %s: %s", rec.name, hook, exc)

    def toggle(self, uuid: str) -> bool:
        if uuid in self.disabled:
            self.disabled.discard(uuid)
            enabled = True
        else:
            self.disabled.add(uuid)
            enabled = False
        self._save_state()
        old = self.plugins.get(uuid)
        if old and old.instance:
            try:
                if old.is_base_plugin:
                    old.instance.on_disable() if not enabled else old.instance.on_enable()
                elif not enabled and hasattr(old.instance, "unload"):
                    old.instance.unload()
            except Exception as exc:
                logger.warning("toggle lifecycle %s: %s", uuid, exc)
        self.load_all()
        return enabled

    def unload_all(self) -> None:
        for rec in self.plugins.values():
            if not rec.instance:
                continue
            try:
                if rec.is_base_plugin:
                    rec.instance.on_unload()
                elif hasattr(rec.instance, "unload"):
                    rec.instance.unload()
            except Exception as exc:
                logger.error("Unload %s failed: %s", rec.name, exc)

    def get_plugin_instance(self, uuid: str) -> BasePlugin | Any | None:
        rec = self.plugins.get(uuid)
        return rec.instance if rec else None

    async def sort_records_pinned(self, records: list[PluginRecord]) -> list[PluginRecord]:
        db = getattr(self.core, "db", None)
        if not db:
            return records
        try:
            pins = await db.list_pinned_plugins()
        except Exception:
            return records
        pin_set = set(pins)
        for r in records:
            r.pinned = r.uuid in pin_set
        return sorted(records, key=lambda r: (not r.pinned, r.name.lower()))

    def list_records(self) -> list[PluginRecord]:
        return list(self.plugins.values())

    def get_tg_routers(self) -> list:
        routers = []
        for rec in self.plugins.values():
            if rec.enabled and rec.instance and rec.is_base_plugin:
                router = rec.instance.get_router()
                if router:
                    routers.append(router)
        return routers
