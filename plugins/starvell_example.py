"""
Пример плагина Starvell Cardinal — FPC-style с настройками в Telegram.
"""

from __future__ import annotations

import logging

from core.plugins.base import BasePlugin

NAME = "Starvell Example Plugin"
VERSION = "2.0.0"
DESCRIPTION = "Отвечает на «тест» и демонстрирует панель настроек плагина"
CREDITS = "Starvell Cardinal"
UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SETTINGS_PAGE = True


class Plugin(BasePlugin):
    """Пример: автоответ + настраиваемый триггер."""

    NAME = NAME
    UUID = UUID
    VERSION = VERSION
    DESCRIPTION = DESCRIPTION
    CREDITS = CREDITS
    SETTINGS_PAGE = True

    def on_load(self) -> None:
        self.core.events.on("on_message", self._on_message)
        self.logger.info("ExamplePlugin v%s загружен", VERSION)

    def on_unload(self) -> None:
        self.core.events.off("on_message", self._on_message)

    def get_settings_schema(self) -> list[dict]:
        return [
            {
                "key": "enabled",
                "label": "Активен",
                "type": "bool",
                "default": True,
            },
            {
                "key": "notify_admin",
                "label": "Уведомлять в TG",
                "type": "bool",
                "default": False,
            },
            {
                "key": "test_reply",
                "label": "Тест ответа",
                "type": "action",
            },
        ]

    async def _on_message(self, data: dict) -> None:
        cfg = await self.plugin_settings.get_all(self.UUID)
        if not cfg.get("enabled", True):
            return
        message = data.get("message", "")
        chat_id = data.get("chat_id")
        ctx = data.get("ctx")
        trigger = cfg.get("trigger", "тест")
        if trigger in message.lower() and chat_id and ctx:
            await self.core.send_message(
                chat_id,
                "✅ Плагин Starvell Example работает!",
                ctx.account_name,
            )
            if cfg.get("notify_admin"):
                await self.core.notify(
                    f"📨 Example plugin → чат {chat_id[:8]}",
                    "notify_chats",
                    chat_id=chat_id,
                )

    async def on_settings_action(self, call, action: str) -> bool:
        if action == "test_reply":
            await call.answer("✅ Плагин отвечает!", show_alert=True)
            return True
        return False
