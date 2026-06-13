"""
Настройки плагинов — FPC-style EDIT_PLUGIN / sc:plugcfg:
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery

from handlers.tg.loading import loading_skeleton
from keyboards import cbt as CBT

logger = logging.getLogger("starvell.handlers.plugin_settings")


def create_plugin_settings_router(ctx: Any) -> Router:
    router = Router(name="plugin_settings")
    pm = ctx.plugin_manager

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SETTINGS))
    async def cb_plugin_settings(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_SETTINGS, "").split(":")[0]
        await _show_settings(call, pm, uuid)

    @router.callback_query(F.data.startswith(CBT.EDIT_PLUGIN))
    async def cb_edit_plugin(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.EDIT_PLUGIN, "").split(":")[0]
        await _show_settings(call, pm, uuid)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SETTING))
    async def cb_plugin_setting_toggle(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        payload = call.data.replace(CBT.PLUGIN_SETTING, "")
        uuid, _, key = payload.partition(":")
        rec = pm.plugins.get(uuid)
        if rec and rec.instance and hasattr(rec.instance, "on_setting_toggle"):
            async with loading_skeleton(call):
                await rec.instance.on_setting_toggle(key)
                await _show_settings(call, pm, uuid, skip_loading=True)
        else:
            await call.answer("Настройка недоступна", show_alert=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_ACTION))
    async def cb_plugin_action(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        payload = call.data.replace(CBT.PLUGIN_ACTION, "")
        uuid, _, action = payload.partition(":")
        rec = pm.plugins.get(uuid)
        if rec and rec.instance and hasattr(rec.instance, "on_settings_action"):
            handled = await rec.instance.on_settings_action(call, action)
            if handled:
                await _show_settings(call, pm, uuid, skip_loading=True)
                return
        await call.answer("Действие не реализовано", show_alert=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_RESET))
    async def cb_plugin_reset(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_RESET, "")
        rec = pm.plugins.get(uuid)
        if rec and rec.instance:
            from core.plugins.settings_store import PluginSettingsStore
            store = PluginSettingsStore(ctx.db)
            await store.save_all(uuid, {})
            await call.answer("✅ Сброшено")
            await _show_settings(call, pm, uuid)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_PIN))
    async def cb_plugin_pin(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid = call.data.replace(CBT.PLUGIN_PIN, "")
        pinned = await ctx.db.toggle_plugin_pin(uuid)
        await call.answer("📌 Закреплён" if pinned else "📌 Откреплён")
        from handlers.tg.plugins_panel import _render_plugins_page
        await _render_plugins_page(call, pm, page=1)

    return router


async def _show_settings(call: CallbackQuery, pm: Any, uuid: str, skip_loading: bool = False) -> None:
    rec = pm.plugins.get(uuid)
    if not rec:
        await call.answer("Плагин не найден", show_alert=True)
        return

    inst = rec.instance
    if inst and hasattr(inst, "render_settings_text"):
        async def render():
            text = await inst.render_settings_text()
            kb = await inst.build_settings_keyboard()
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

        if skip_loading:
            await render()
        else:
            async with loading_skeleton(call):
                await render()
        return

    # Fallback для hook-only / legacy
    text = (
        f"⚙️ <b>{rec.name}</b> v{rec.version}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{rec.description or '—'}\n\n"
        f"Статус: {'🟢 Вкл' if rec.enabled else '🔴 Выкл'}\n"
    )
    if rec.load_error:
        text += f"\n⚠️ {rec.load_error}"
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Плагины", callback_data=CBT.PLUGINS)],
    ])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
