"""
Пример нативного плагина Starvell Cardinal — с карточкой и панелью как в FPC.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from starvell_sdk import MessageContext, StarvellPlugin, on_message

NAME = "Starvell Example Plugin"
VERSION = "2.2.0"
DESCRIPTION = "Отвечает на триггер, демонстрирует карточку и панель плагина"
CREDITS = "Starvell Cardinal"
UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SETTINGS_PAGE = True

TELEGRAM_COMMANDS = [
    {"command": "stvexample", "description": "панель Starvell Example"},
]


class Plugin(StarvellPlugin):
    """Нативный плагин с FPC-style меню."""

    NAME = NAME
    UUID = UUID
    VERSION = VERSION
    DESCRIPTION = DESCRIPTION
    CREDITS = CREDITS
    SETTINGS_PAGE = True
    TELEGRAM_COMMANDS = TELEGRAM_COMMANDS

    def get_settings_schema(self) -> list[dict]:
        return [
            {"key": "enabled", "label": "Активен", "type": "bool", "default": True},
            {
                "key": "trigger",
                "label": "Триггер",
                "type": "text",
                "default": "тест",
                "description": "Слово в сообщении покупателя",
            },
            {"key": "notify_admin", "label": "Уведомлять в TG", "type": "bool", "default": False},
            {"key": "test_reply", "label": "Тест ответа", "type": "action"},
        ]

    async def render_plugin_card_extras(self) -> str:
        enabled = await self.get_cfg("enabled", True)
        trigger = await self.get_cfg("trigger", "тест")
        icon = "🟢" if enabled else "🔴"
        return f"{icon} Триггер: <code>{trigger}</code>"

    async def render_plugin_panel(self) -> tuple[str, InlineKeyboardMarkup]:
        enabled = await self.get_cfg("enabled", True)
        trigger = await self.get_cfg("trigger", "тест")
        text = (
            f"📊 <b>{self.NAME}</b> v{self.VERSION}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Статус: {'🟢 включён' if enabled else '🔴 выключен'}\n"
            f"Триггер: <code>{trigger}</code>\n\n"
            "<i>Нажмите кнопку ниже для настройки и сопровождения.</i>"
        )
        rows = [
            [self.panel_btn("⚙️ Настройки", self.UUID, "open_settings")],
            [self.panel_btn("📨 Тест уведомления", self.UUID, "test_notify")],
            [self.panel_btn("🔄 Обновить панель", self.UUID, "refresh")],
            [self.panel_back_btn(self.UUID)],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def on_panel_action(self, call, action: str) -> bool:
        if action == "open_settings":
            from handlers.tg.plugin_settings import _show_settings
            pm = self.core.plugin_manager
            if pm:
                await _show_settings(call, pm, self.UUID)
            return True
        if action == "test_notify":
            await self.core.notify("📨 Example plugin: тест панели", "notify_orders")
            await call.answer("✅ Уведомление отправлено", show_alert=True)
            return True
        if action == "refresh":
            text, kb = await self.render_plugin_panel()
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            await call.answer("Обновлено")
            return True
        return False

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
