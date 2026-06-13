"""
Slash-команды плагинов (/vexboost, /vb_stats и т.д.).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger("starvell.handlers.plugin_slash")

_COMMAND_TIMEOUT = 18.0


def _collect_commands(pm: Any) -> list[tuple[str, str]]:
    """(command_name, plugin_uuid)"""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rec in pm.plugins.values():
        if not rec.enabled or not rec.instance:
            continue
        commands = pm.get_plugin_commands(rec.uuid) if hasattr(pm, "get_plugin_commands") else []
        for cmd in commands:
            name = str(cmd.get("command", "")).lstrip("/").lower()
            if name and name not in seen:
                seen.add(name)
                result.append((name, rec.uuid))
    return result


def create_plugin_slash_router(ctx: Any) -> Router:
    router = Router(name="plugin_slash")
    pm = ctx.plugin_manager

    for name, uuid in _collect_commands(pm):

        async def handler(message: Message, _uuid: str = uuid, _cmd: str = name) -> None:
            if not await ctx._has_access(message.from_user.id):
                await message.answer("⛔ Нет доступа")
                return

            rec = pm.plugins.get(_uuid)
            if not rec or not rec.instance:
                await message.answer("❌ Плагин не загружен")
                return

            inst = rec.instance

            class _SlashCtx:
                is_slash = True
                message = message
                from_user = message.from_user

                async def answer(self, *a, **k):
                    pass

            try:
                if hasattr(inst, "on_telegram_command"):
                    handled = await asyncio.wait_for(
                        inst.on_telegram_command(_SlashCtx(), _cmd),
                        timeout=_COMMAND_TIMEOUT,
                    )
                    if handled:
                        return
            except asyncio.TimeoutError:
                logger.error("slash /%s timeout", _cmd)
                await message.answer(f"❌ Команда /{_cmd} — таймаут. Попробуйте снова.")
                return
            except Exception as exc:
                logger.exception("slash /%s: %s", _cmd, exc)
                await message.answer(f"❌ Ошибка /{_cmd}: {exc}")
                return

            if hasattr(inst, "has_plugin_panel") and inst.has_plugin_panel():
                try:
                    result = await asyncio.wait_for(
                        inst.render_plugin_panel(),
                        timeout=_COMMAND_TIMEOUT,
                    )
                    if result:
                        text, kb = result
                        await message.answer(text, parse_mode="HTML", reply_markup=kb)
                        return
                except asyncio.TimeoutError:
                    await message.answer(f"❌ Панель /{_cmd} — таймаут")
                    return
                except Exception as exc:
                    logger.exception("slash panel %s: %s", _cmd, exc)

            await message.answer(
                f"ℹ️ Команда /{_cmd} — откройте плагин через меню <b>Плагины</b>.",
                parse_mode="HTML",
            )

        router.message.register(handler, Command(name))

    return router
