"""
Пример нативного плагина Starvell Cardinal.
"""

from __future__ import annotations

from starvell_sdk import MessageContext, StarvellPlugin, on_message

NAME = "Starvell Example Plugin"
VERSION = "2.1.0"
DESCRIPTION = "Отвечает на триггер и демонстрирует панель настроек"
CREDITS = "Starvell Cardinal"
UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SETTINGS_PAGE = True


class Plugin(StarvellPlugin):
    """Нативный плагин: @on_message + настройки в Telegram."""

    NAME = NAME
    UUID = UUID
    VERSION = VERSION
    DESCRIPTION = DESCRIPTION
    CREDITS = CREDITS
    SETTINGS_PAGE = True

    def get_settings_schema(self) -> list[dict]:
        return [
            {"key": "enabled", "label": "Активен", "type": "bool", "default": True},
            {"key": "trigger", "label": "Триггер", "type": "text", "default": "тест"},
            {"key": "notify_admin", "label": "Уведомлять в TG", "type": "bool", "default": False},
            {"key": "test_reply", "label": "Тест ответа", "type": "action"},
        ]

    @on_message
    async def on_buyer_message(self, ctx: MessageContext) -> None:
        if not await self.get_cfg("enabled", True):
            return
        trigger = str(await self.get_cfg("trigger", "тест")).lower()
        if trigger and trigger in ctx.text.lower():
            await ctx.reply_watermarked("✅ Плагин Starvell Example работает!")
            if await self.get_cfg("notify_admin", False):
                await ctx.notify(
                    f"📨 Example plugin → чат {ctx.chat_id[:8]}",
                    "notify_chats",
                    chat_id=ctx.chat_id,
                )

    async def on_settings_action(self, call, action: str) -> bool:
        if action == "test_reply":
            await call.answer("✅ Плагин отвечает!", show_alert=True)
            return True
        return False
