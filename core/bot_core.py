"""
Bot Core — центральный объект приложения.
Доступен плагинам: API Starvell, БД, настройки, шина событий, планировщик.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from config import Settings, load_settings
from database import Database

logger = logging.getLogger("starvell.core")


class EventBus:
    """Асинхронная шина событий (расширяемая замена EventManager)."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}

    def on(self, event: str, handler: Callable) -> Callable:
        self._handlers.setdefault(event, []).append(handler)
        return handler

    def off(self, event: str, handler: Callable) -> None:
        if event in self._handlers:
            self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    async def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        payload = data or {}
        for handler in self._handlers.get(event, []):
            try:
                result = handler(payload)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.exception("EventBus %s handler %s failed: %s", event, handler, exc)

    # Совместимость с legacy EventManager
    register_handler = on
    unregister_handler = off

    async def dispatch(self, event: str, data: dict[str, Any]) -> None:
        await self.emit(event, data)


class BotCore:
    """
    Ядро бота — единая точка доступа для плагинов и handlers.

    Attributes:
        settings: Текущие настройки.
        db: SQLite-репозиторий (legacy, миграция на SQLAlchemy — постепенно).
        events: Шина событий.
        plugin_manager: PluginEngine (устанавливается при старте).
        scheduler: AsyncIO планировщик задач.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.db = db or Database()
        self.events = event_bus or EventBus()
        self.event_manager = self.events  # alias FPC
        self.plugin_manager: Any = None
        self.scheduler: Any = None
        self.logger = logger
        self._apis: dict[str, Any] = {}
        self._notify_cb: Callable[..., Awaitable[None]] | None = None
        self.account = None
        self.telegram: Any = None
        self._commands: list[tuple] = []
        self._plugin_commands: dict[str, list[dict]] = {}

    def register_telegram(self, adapter: Any) -> None:
        self.telegram = adapter

    def add_telegram_commands(self, plugin_uuid_or_commands: Any, commands: list | None = None) -> None:
        """FPC: add_telegram_commands(UUID, [...]) или legacy list of tuples."""
        if commands is not None:
            uuid = str(plugin_uuid_or_commands)
            for item in commands:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    cmd, desc = item[0], item[1]
                    self._plugin_commands.setdefault(uuid, []).append({
                        "command": str(cmd).lstrip("/"),
                        "description": str(desc),
                    })
            return
        if isinstance(plugin_uuid_or_commands, list):
            self._commands.extend(plugin_uuid_or_commands)

    def get_plugin_commands(self, plugin_uuid: str) -> list[dict]:
        return list(self._plugin_commands.get(plugin_uuid, []))

    def register_api(self, account_name: str, api: Any) -> None:
        self._apis[account_name] = api
        if account_name == "default":
            self.account = api

    def get_api(self, account_name: str = "default") -> Any | None:
        return self._apis.get(account_name)

    def set_notify_callback(self, cb: Callable[..., Awaitable[None]]) -> None:
        self._notify_cb = cb

    async def notify(self, text: str, notify_type: str = "notify_orders", **extra: Any) -> None:
        if not self._notify_cb:
            return
        try:
            await self._notify_cb(text, notify_type, **extra)
        except TypeError:
            try:
                await self._notify_cb(text, notify_type)
            except TypeError:
                await self._notify_cb(text)

    async def send_message(self, chat_id: str, text: str, account_name: str = "default") -> None:
        api = self.get_api(account_name)
        if api:
            await api.send_message(chat_id, text)

    async def dispatch_plugins(self, hook: str, *args, **kwargs) -> None:
        if self.plugin_manager:
            await self.plugin_manager.dispatch_hook(hook, self, *args, **kwargs)

    def reload_settings(self) -> None:
        self.settings = load_settings()


# Обратная совместимость
Cardinal = BotCore
CardinalCore = BotCore
EventManager = EventBus
