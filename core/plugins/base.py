"""
Базовый класс плагинов — FPC-совместимый с настройками через Telegram.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiogram import Router
    from aiogram.types import CallbackQuery, InlineKeyboardMarkup

    from core.bot_core import BotCore


class BasePlugin(ABC):
    """
    Базовый плагин Starvell Cardinal (аналог FPC Plugin).

    Переопределите метаданные и ``on_load()``.
    Для настроек: ``SETTINGS_PAGE = True``, ``get_settings_schema()``, ``on_setting_change()``.
    """

    NAME: str = "Unnamed Plugin"
    UUID: str = "unnamed-plugin"
    VERSION: str = "1.0.0"
    DESCRIPTION: str = ""
    CREDITS: str = ""
    SETTINGS_PAGE: bool = True
    SETTINGS_CALLBACK: str | None = None

    def __init__(self, core: BotCore, config: dict[str, Any] | None = None) -> None:
        self.core = core
        self.cardinal = core  # FPC alias
        self.config = config or {}
        self.logger = logging.getLogger(f"starvell.plugin.{self.UUID}")
        self._router: Router | None = None
        self._settings_store = None

    @property
    def db(self):
        return self.core.db

    @property
    def settings(self):
        return self.core.settings

    @property
    def plugin_settings(self):
        if self._settings_store is None:
            from core.plugins.settings_store import PluginSettingsStore
            self._settings_store = PluginSettingsStore(self.db)
        return self._settings_store

    def get_api(self, account: str = "default"):
        return self.core.get_api(account)

    @abstractmethod
    def on_load(self) -> None:
        """Вызывается при загрузке / hot-reload."""

    def on_unload(self) -> None:
        pass

    def on_enable(self) -> None:
        pass

    def on_disable(self) -> None:
        pass

    def get_router(self) -> Router | None:
        return self._router

    def get_settings_schema(self) -> list[dict[str, Any]]:
        """
        Схема настроек для UI (как в FPC).

        Каждый элемент: ``{key, label, type, default, description}``
        type: bool | str | int | select
        """
        return []

    async def render_settings_text(self) -> str:
        """Текст страницы настроек плагина."""
        lines = [
            f"⚙️ <b>{self.NAME}</b> v{self.VERSION}",
            "━━━━━━━━━━━━━━━━━━",
            f"<i>{self.DESCRIPTION}</i>",
            "",
        ]
        cfg = await self.plugin_settings.get_all(self.UUID)
        schema = self.get_settings_schema()
        if not schema:
            lines.append("<i>Нет настраиваемых параметров</i>")
        else:
            lines.append("<b>Параметры:</b>")
            for field in schema:
                key = field["key"]
                val = cfg.get(key, field.get("default"))
                label = field.get("label", key)
                if field.get("type") == "bool":
                    icon = "🟢" if val else "🔴"
                    lines.append(f"{icon} {label}")
                else:
                    lines.append(f"• <b>{label}</b>: <code>{val}</code>")
        if self.CREDITS:
            lines.append(f"\n👤 {self.CREDITS}")
        return "\n".join(lines)

    async def build_settings_keyboard(self) -> InlineKeyboardMarkup:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from keyboards import cbt as CBT

        cfg = await self.plugin_settings.get_all(self.UUID)
        rows: list[list[InlineKeyboardButton]] = []

        for field in self.get_settings_schema():
            key = field["key"]
            label = field.get("label", key)
            ftype = field.get("type", "str")
            if ftype == "bool":
                on = bool(cfg.get(key, field.get("default", False)))
                rows.append([
                    InlineKeyboardButton(
                        text=f"{'🟢' if on else '🔴'} {label}",
                        callback_data=f"{CBT.PLUGIN_SETTING}{self.UUID}:{key}",
                    )
                ])
            elif ftype == "action":
                rows.append([
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"{CBT.PLUGIN_ACTION}{self.UUID}:{key}",
                    )
                ])

        rows.append([
            InlineKeyboardButton(text="🔄 Сброс", callback_data=f"{CBT.PLUGIN_RESET}{self.UUID}"),
            InlineKeyboardButton(text="◀️ Плагины", callback_data=CBT.PLUGINS),
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def on_setting_toggle(self, key: str) -> None:
        schema = {f["key"]: f for f in self.get_settings_schema()}
        field = schema.get(key)
        if not field or field.get("type") != "bool":
            return
        cfg = await self.plugin_settings.get_all(self.UUID)
        current = bool(cfg.get(key, field.get("default", False)))
        await self.plugin_settings.set(self.UUID, key, not current)
        await self.on_setting_change(key, not current)

    async def on_setting_change(self, key: str, value: Any) -> None:
        """Хук после изменения настройки."""

    async def on_settings_action(self, call: CallbackQuery, action: str) -> bool:
        """Кастомное действие (type=action). Верните True если обработано."""
        return False

    def schedule_task(self, job_id: str, coro_factory, interval_seconds: float, **kwargs) -> None:
        if self.core.scheduler:
            self.core.scheduler.add_interval_job(
                job_id=f"{self.UUID}:{job_id}",
                func=coro_factory,
                seconds=interval_seconds,
                **kwargs,
            )

    def cancel_task(self, job_id: str) -> None:
        if self.core.scheduler:
            self.core.scheduler.remove_job(f"{self.UUID}:{job_id}")

    def setup(self) -> None:
        self.on_load()

    def unload(self) -> None:
        self.on_unload()
