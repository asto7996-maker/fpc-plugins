"""
Единый роутер Premium UI — подключает все панели FPC-style.
"""

from __future__ import annotations

from typing import Any

from aiogram import Router

from handlers.tg.autoresponder import create_autoresponder_router
from handlers.tg.notifications import create_notifications_router
from handlers.tg.plugin_settings import create_plugin_settings_router
from handlers.tg.plugins_panel import create_premium_router


def create_hub_router(ctx: Any) -> Router:
    hub = Router(name="premium_hub")
    hub.include_router(create_premium_router(ctx))
    hub.include_router(create_plugin_settings_router(ctx))
    hub.include_router(create_autoresponder_router(ctx))
    hub.include_router(create_notifications_router(ctx))
    return hub
