"""
Slash-команды плагинов (/stvexample и т.д.) — как в FPC.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger("starvell.handlers.plugin_slash")


def create_plugin_slash_router(ctx: Any) -> Router:
    router = Router(name="plugin_slash")
    pm = ctx.plugin_manager

    for rec in list(pm.plugins.values()):
        commands = pm.get_plugin_commands(rec.uuid) if hasattr(pm, "get_plugin_commands") else []
        for cmd in commands:
            name = str(cmd.get("command", "")).lstrip("/")
            if not name:
                continue
            uuid = rec.uuid

            async def handler(message: Message, _uuid: str = uuid, _cmd: str = name) -> None:
                if not await ctx._has_access(message.from_user.id):
                    return
                rec2 = pm.plugins.get(_uuid)
                if not rec2 or not rec2.instance:
                    await message.answer("❌ Плагин не загружен")
                    return
                inst = rec2.instance
                if hasattr(inst, "on_telegram_command"):
                    class _FakeCall:
                        message = message
                        from_user = message.from_user
                        async def answer(self, *a, **k):
                            pass
                    handled = await inst.on_telegram_command(_FakeCall(), _cmd)
                    if handled:
                        return
                if hasattr(inst, "has_plugin_panel") and inst.has_plugin_panel():
                    try:
                        result = await inst.render_plugin_panel()
                        if result:
                            text, kb = result
                            await message.answer(text, parse_mode="HTML", reply_markup=kb)
                            return
                    except Exception as exc:
                        logger.exception("slash panel %s: %s", _cmd, exc)
                from handlers.tg.plugin_card import render_plugin_card
                from aiogram.types import CallbackQuery
                # fallback: карточка плагина
                await message.answer(
                    f"ℹ️ Команда /{_cmd} — откройте плагин через меню <b>Плагины</b>.",
                    parse_mode="HTML",
                )

            router.message.register(handler, Command(name))

    return router
