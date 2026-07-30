"""Admin alert fan-out via aiogram Bot API."""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot

from tg_pool.config import Settings, get_settings

logger = logging.getLogger(__name__)


class AlertService:
    """Sends emergency notifications to configured admin Telegram IDs."""

    def __init__(self, bot: Optional[Bot] = None, settings: Optional[Settings] = None) -> None:
        self.bot = bot
        self.settings = settings or get_settings()

    def bind_bot(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, account_id: int, phone: str, level: str, message: str) -> None:
        icon = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(level, "📢")
        text = (
            f"{icon} <b>[{level.upper()}] Account #{account_id}</b>\n"
            f"phone: <code>{phone}</code>\n"
            f"{message}"
        )
        if self.bot is None:
            logger.warning("AlertService has no bot bound: %s", text)
            return
        targets = self.settings.admin_ids
        if not targets:
            logger.warning("ADMIN_IDS empty — alert dropped")
            return
        for admin_id in targets:
            try:
                await self.bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                logger.exception("Failed to alert admin %s", admin_id)

    async def as_callback(self, account_id: int, phone: str, level: str, message: str) -> None:
        """Signature compatible with SessionWrapper.on_alert."""
        await self.send(account_id, phone, level, message)
