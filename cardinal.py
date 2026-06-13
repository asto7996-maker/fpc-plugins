"""
Ядро Starvell Cardinal — аналог cardinal.py в FunPay Cardinal.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from config import Settings, load_settings
from database import Database

logger = logging.getLogger("starvell.cardinal")


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


class Cardinal:
    """Центральный объект бота: API, плагины, уведомления."""

    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        event_manager: EventManager | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.db = db or Database()
        self.event_manager = event_manager or EventManager()
        self.plugin_manager: Any = None
        self.logger = logger
        self._apis: dict[str, Any] = {}
        self._notify_cb: Callable[..., Awaitable[None]] | None = None
        self._send_message_cb: Callable | None = None
        self.account = None

    def register_api(self, account_name: str, api: Any) -> None:
        self._apis[account_name] = api
        if account_name == "default":
            self.account = api

    def get_api(self, account_name: str = "default") -> Any | None:
        return self._apis.get(account_name)

    def set_notify_callback(self, cb: Callable[..., Awaitable[None]]) -> None:
        self._notify_cb = cb

    def set_send_message_callback(self, cb: Callable) -> None:
        self._send_message_cb = cb

    async def notify(self, text: str, notify_type: str = "notify_orders", **extra: Any) -> None:
        if self._notify_cb:
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
        if self._send_message_cb:
            await self._send_message_cb(chat_id, text, account_name)

    async def dispatch_plugins(self, hook: str, *args, **kwargs) -> None:
        if self.plugin_manager:
            await self.plugin_manager.dispatch_hook(hook, self, *args, **kwargs)

    def reload_settings(self) -> None:
        self.settings = load_settings()


CardinalCore = Cardinal
