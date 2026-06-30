"""
Единый роутер Premium UI — подключает все панели FPC-style.
"""

from __future__ import annotations

from typing import Any

from aiogram import Router

from handlers.tg.autoresponder import create_autoresponder_router
from handlers.tg.backup_panel import create_backup_router
from handlers.tg.help_panel import create_help_router
from handlers.tg.lot_parser import create_lot_parser_router
from handlers.tg.notifications import create_notifications_router
from handlers.tg.profile_panel import create_profile_router
from handlers.tg.plugin_card import create_plugin_card_router
from handlers.tg.plugin_settings import create_plugin_settings_router
from handlers.tg.plugin_slash import create_plugin_slash_router
from handlers.tg.plugin_upload import create_plugin_upload_router
from handlers.tg.plugins_panel import create_premium_router


def create_hub_router(ctx: Any) -> Router:
    hub = Router(name="premium_hub")
    hub.include_router(create_premium_router(ctx))
    hub.include_router(create_plugin_card_router(ctx))
    hub.include_router(create_plugin_settings_router(ctx))
    hub.include_router(create_plugin_slash_router(ctx))
    hub.include_router(create_plugin_upload_router(ctx))
    hub.include_router(create_profile_router(ctx))
    hub.include_router(create_lot_parser_router(ctx))
    hub.include_router(create_backup_router(ctx))
    hub.include_router(create_help_router(ctx))
    hub.include_router(create_autoresponder_router(ctx))
    hub.include_router(create_notifications_router(ctx))
    return hub
