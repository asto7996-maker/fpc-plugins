"""
Базовый класс плагинов Starvell Cardinal.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiogram import Router

    from core.bot_core import BotCore


class BasePlugin(ABC):
    """
    Базовый плагин с доступом к ядру, БД, API и Telegram Router.

    Переопределите метаданные и методы жизненного цикла.
    Используйте ``register_button`` / ``register_handler`` для UI.
    """

    # Метаданные (переопределите в наследнике)
    NAME: str = "Unnamed Plugin"
    UUID: str = "unnamed-plugin"
    VERSION: str = "1.0.0"
    DESCRIPTION: str = ""
    CREDITS: str = ""
    SETTINGS_CALLBACK: str | None = None  # callback_data для кнопки «Настроить»

    def __init__(self, core: BotCore, config: dict[str, Any] | None = None) -> None:
        self.core = core
        self.config = config or {}
        self.logger = logging.getLogger(f"starvell.plugin.{self.UUID}")
        self._router: Router | None = None
        self._enabled = True

    @property
    def db(self):
        return self.core.db

    @property
    def settings(self):
        return self.core.settings

    def get_api(self, account: str = "default"):
        return self.core.get_api(account)

    @abstractmethod
    def on_load(self) -> None:
        """Вызывается при загрузке / hot-reload плагина."""

    def on_unload(self) -> None:
        """Вызывается при выгрузке плагина."""

    def on_enable(self) -> None:
        """Плагин включён админом."""

    def on_disable(self) -> None:
        """Плагин выключен админом."""

    def get_router(self) -> Router | None:
        """Telegram Router плагина (опционально)."""
        return self._router

    def schedule_task(self, job_id: str, coro_factory, interval_seconds: float, **kwargs) -> None:
        """Регистрирует периодическую async-задачу через APScheduler."""
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

    # Legacy FPC-совместимость
    def setup(self) -> None:
        self.on_load()

    def unload(self) -> None:
        self.on_unload()
