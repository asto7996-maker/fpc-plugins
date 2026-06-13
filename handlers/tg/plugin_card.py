"""
Карточка плагина — FPC-style: меню, команды, панель, удаление.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery

from handlers.tg.loading import loading_skeleton
from handlers.tg.plugins_panel import _render_plugins_page
from keyboards import cbt as CBT
from keyboards.plugin_card import (
    plugin_card_keyboard,
    plugin_card_text,
    plugin_commands_keyboard,
    plugin_commands_text,
    plugin_delete_confirm_keyboard,
)

logger = logging.getLogger("starvell.handlers.plugin_card")


def _rec(pm: Any, uuid: str) -> Any | None:
    return pm.plugins.get(uuid)


def _plugin_capabilities(pm: Any, rec: Any) -> tuple[bool, bool, bool]:
    """has_commands, has_panel, has_settings."""
    inst = rec.instance if rec else None
    commands = pm.get_plugin_commands(rec.uuid) if rec and hasattr(pm, "get_plugin_commands") else []
    has_commands = len(commands) > 0
    has_panel = bool(inst and hasattr(inst, "has_plugin_panel") and inst.has_plugin_panel())
    has_settings = bool(rec and rec.has_settings_page)
    return has_commands, has_panel, has_settings


async def render_plugin_card(call: CallbackQuery, pm: Any, uuid: str, db: Any = None, skip_loading: bool = False) -> None:
    rec = _rec(pm, uuid)
    if not rec:
        await call.answer("Плагин не найден", show_alert=True)
        return

    if db and hasattr(pm, "sort_records_pinned"):
        pins = await db.list_pinned_plugins()
        rec.pinned = uuid in set(pins)

    extra = ""
    inst = rec.instance
    if inst and hasattr(inst, "render_plugin_card_extras"):
        try:
            result = inst.render_plugin_card_extras()
            if hasattr(result, "__await__"):
                extra = await result
            else:
                extra = str(result or "")
        except Exception as exc:
            logger.warning("card extras %s: %s", uuid, exc)

    has_commands, has_panel, has_settings = _plugin_capabilities(pm, rec)
    text = plugin_card_text(rec, extra)
    kb = plugin_card_keyboard(rec, has_commands=has_commands, has_panel=has_panel, has_settings=has_settings)

    async def _do() -> None:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    if skip_loading:
        await _do()
    else:
        async with loading_skeleton(call):
            await _do()


def create_plugin_card_router(ctx: Any) -> Router:
    router = Router(name="plugin_card")
    pm = ctx.plugin_manager

    @router.callback_query(F.data.startswith(CBT.PLUGIN_VIEW))
    async def cb_plugin_view(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_VIEW, "").split(":")[0]
        await render_plugin_card(call, pm, uuid, db=ctx.db)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_ACTIVATE))
    async def cb_plugin_activate(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_ACTIVATE, "").split(":")[0]
        pm.toggle(uuid)
        await call.answer("Статус изменён")
        await render_plugin_card(call, pm, uuid, db=ctx.db, skip_loading=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_COMMANDS))
    async def cb_plugin_commands(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_COMMANDS, "").split(":")[0]
        rec = _rec(pm, uuid)
        if not rec:
            await call.answer("Плагин не найден", show_alert=True)
            return
        commands = pm.get_plugin_commands(uuid) if hasattr(pm, "get_plugin_commands") else []
        async with loading_skeleton(call):
            await call.message.edit_text(
                plugin_commands_text(rec, commands),
                parse_mode="HTML",
                reply_markup=plugin_commands_keyboard(uuid),
            )

    @router.callback_query(F.data.startswith(CBT.PLUGIN_PANEL))
    async def cb_plugin_panel(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_PANEL, "").split(":")[0]
        rec = _rec(pm, uuid)
        if not rec or not rec.instance:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        inst = rec.instance
        if not hasattr(inst, "render_plugin_panel"):
            await call.answer("Панель недоступна", show_alert=True)
            return
        try:
            result = await inst.render_plugin_panel()
        except Exception as exc:
            logger.exception("panel %s: %s", uuid, exc)
            await call.answer(f"Ошибка панели: {exc}", show_alert=True)
            return
        if not result:
            await call.answer("Панель не настроена", show_alert=True)
            return
        text, kb = result
        async with loading_skeleton(call):
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_PANEL_ACT))
    async def cb_plugin_panel_action(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        body = call.data.replace(CBT.PLUGIN_PANEL_ACT, "", 1)
        parts = body.split(":", 1)
        if len(parts) < 2:
            return
        uuid, action = parts[0], parts[1]
        rec = _rec(pm, uuid)
        if not rec or not rec.instance:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        inst = rec.instance
        if hasattr(inst, "on_panel_action"):
            handled = await inst.on_panel_action(call, action)
            if handled:
                return
        await call.answer("Действие не реализовано", show_alert=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_DELETE) & ~F.data.startswith(CBT.PLUGIN_DELETE_OK))
    async def cb_plugin_delete_ask(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_DELETE, "").split(":")[0]
        rec = _rec(pm, uuid)
        if not rec:
            await call.answer("Плагин не найден", show_alert=True)
            return
        await call.message.edit_text(
            f"🗑 Удалить плагин <b>{rec.name}</b>?\n\n"
            f"Файл <code>{os.path.basename(rec.path)}</code> будет удалён с диска.",
            parse_mode="HTML",
            reply_markup=plugin_delete_confirm_keyboard(uuid),
        )
        await call.answer()

    @router.callback_query(F.data.startswith(CBT.PLUGIN_DELETE_OK))
    async def cb_plugin_delete_ok(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_DELETE_OK, "").split(":")[0]
        if hasattr(pm, "delete_plugin"):
            ok, msg = pm.delete_plugin(uuid)
            await call.answer(msg, show_alert=not ok)
            if ok:
                await _render_plugins_page(call, pm, page=1, db=ctx.db)
            return
        await call.answer("Удаление недоступно", show_alert=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_RELOAD))
    async def cb_plugin_reload(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_RELOAD, "").split(":")[0]
        async with loading_skeleton(call):
            if hasattr(pm, "reload_plugin"):
                await pm.reload_plugin(uuid)
            else:
                pm.load_all()
            await render_plugin_card(call, pm, uuid, db=ctx.db, skip_loading=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_PIN))
    async def cb_plugin_pin_card(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_PIN, "").split(":")[0]
        pinned = await ctx.db.toggle_plugin_pin(uuid)
        await call.answer("📌 Закреплён" if pinned else "📌 Откреплён")
        await render_plugin_card(call, pm, uuid, db=ctx.db, skip_loading=True)

    return router
