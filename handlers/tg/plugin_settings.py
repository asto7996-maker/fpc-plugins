"""
Настройки плагинов — полный UI: bool, text, int, select, action.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from handlers.tg.loading import loading_skeleton
from core.plugins.settings_cb import parse_uuid_token, resolve_schema_field
from keyboards import cbt as CBT
from keyboards.plugin_settings import plugin_select_keyboard

logger = logging.getLogger("starvell.handlers.plugin_settings")


class PluginSettingsStates(StatesGroup):
    waiting_value = State()


def _parse_uuid_key(payload: str, prefix: str) -> tuple[str, str]:
    return parse_uuid_token(payload, prefix)


def _field_page(inst: Any, field_idx: int) -> int:
    page_size = max(4, inst.settings_page_size()) if hasattr(inst, "settings_page_size") else 10
    return max(0, field_idx // page_size)


def _get_plugin_instance(pm: Any, uuid: str) -> Any | None:
    rec = pm.plugins.get(uuid)
    if not rec or not rec.instance:
        return None
    return rec.instance


def _validate_value(field: dict[str, Any], raw: str) -> tuple[Any | None, str | None]:
    ftype = field.get("type", "str")
    text = raw.strip()

    if ftype == "int":
        try:
            val = int(text)
        except ValueError:
            return None, "Введите целое число"
        min_v = field.get("min")
        max_v = field.get("max")
        if min_v is not None and val < min_v:
            return None, f"Минимум: {min_v}"
        if max_v is not None and val > max_v:
            return None, f"Максимум: {max_v}"
        return val, None

    if ftype in ("text", "str", "multiline"):
        max_len = field.get("max_length", 4000)
        if len(text) > max_len:
            return None, f"Слишком длинно (макс. {max_len})"
        if field.get("required") and not text:
            return None, "Значение не может быть пустым"
        return text, None

    return text, None


def _parse_settings_payload(data: str) -> tuple[str, int]:
    body = data.replace(CBT.PLUGIN_SETTINGS, "", 1)
    parts = body.split(":", 1)
    uuid = parts[0]
    page = 0
    if len(parts) > 1 and parts[1].isdigit():
        page = int(parts[1])
    return uuid, page


def create_plugin_settings_router(ctx: Any) -> Router:
    router = Router(name="plugin_settings")
    pm = ctx.plugin_manager

    @router.callback_query(F.data == "sc:noop")
    async def cb_noop(call: CallbackQuery) -> None:
        await call.answer()

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SETTINGS))
    async def cb_plugin_settings(call: CallbackQuery, state: FSMContext) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        await state.clear()
        uuid, page = _parse_settings_payload(call.data)
        await _show_settings(call, pm, uuid, page=page)

    @router.callback_query(F.data.startswith(CBT.EDIT_PLUGIN))
    async def cb_edit_plugin(call: CallbackQuery, state: FSMContext) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        await state.clear()
        uuid = call.data.replace(CBT.EDIT_PLUGIN, "").split(":")[0]
        await _show_settings(call, pm, uuid)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SETTING))
    async def cb_plugin_setting_toggle(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid, token = _parse_uuid_key(call.data, CBT.PLUGIN_SETTING)
        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        field = resolve_schema_field(inst, token)
        if not field or field.get("type") != "bool":
            await call.answer("Настройка недоступна", show_alert=True)
            return
        key = field["key"]
        page = _field_page(inst, int(token)) if token.isdigit() else 0
        async with loading_skeleton(call):
            await inst.on_setting_toggle(key)
            await _show_settings(call, pm, uuid, page=page, skip_loading=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_EDIT))
    async def cb_plugin_edit_field(call: CallbackQuery, state: FSMContext) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid, token = _parse_uuid_key(call.data, CBT.PLUGIN_EDIT)
        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        field = resolve_schema_field(inst, token)
        if not field:
            await call.answer("Поле не найдено", show_alert=True)
            return
        key = field["key"]
        page = _field_page(inst, int(token)) if token.isdigit() else 0

        ftype = field.get("type", "str")
        if ftype not in ("text", "str", "multiline", "int"):
            await call.answer("Редактирование недоступно", show_alert=True)
            return

        cfg = await inst.plugin_settings.get_all(inst.UUID)
        current = cfg.get(key, field.get("default", ""))
        label = field.get("label", key)
        hint = field.get("description", "")

        await state.set_state(PluginSettingsStates.waiting_value)
        await state.update_data(uuid=uuid, key=key, page=page)

        prompt = f"✏️ <b>{inst.NAME}</b> → <b>{label}</b>\n"
        if hint:
            prompt += f"<i>{hint}</i>\n"
        prompt += f"\nТекущее: <code>{current}</code>\n\n"
        if ftype == "multiline":
            prompt += "Отправьте новый текст (можно несколько строк).\n"
        elif ftype == "int":
            prompt += "Введите число.\n"
        else:
            prompt += "Введите новое значение.\n"
        prompt += "\n/cancel — отмена"

        await call.message.answer(prompt, parse_mode="HTML")
        await call.answer()

    @router.message(PluginSettingsStates.waiting_value)
    async def on_plugin_setting_value(message: Message, state: FSMContext) -> None:
        if not await ctx._has_access(message.from_user.id):
            return

        data = await state.get_data()
        uuid = data.get("uuid")
        key = data.get("key")
        if not uuid or not key:
            await state.clear()
            return

        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await state.clear()
            await message.answer("❌ Плагин не загружен")
            return

        field = inst.get_schema_field(key)
        if not field:
            await state.clear()
            await message.answer("❌ Поле не найдено")
            return

        text_raw = message.text or ""
        if field.get("type") == "multiline":
            text = text_raw
        else:
            text = text_raw.strip()

        if text.lower() in ("/cancel", "отмена"):
            await state.clear()
            await message.answer("❌ Редактирование отменено")
            return

        value, err = _validate_value(field, text)
        if err:
            await message.answer(f"⚠️ {err}\nПопробуйте снова или /cancel")
            return

        await inst.apply_setting(key, value)
        await state.clear()

        page = int(data.get("page") or 0)
        try:
            text_page = await inst.render_settings_text(page=page)
            kb = await inst.build_settings_keyboard(page=page)
        except TypeError:
            text_page = await inst.render_settings_text()
            kb = await inst.build_settings_keyboard()
        await message.answer(f"✅ Сохранено: <b>{field.get('label', key)}</b>", parse_mode="HTML")
        await message.answer(text_page, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SELECT_MENU))
    async def cb_plugin_select_menu(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid, token = _parse_uuid_key(call.data, CBT.PLUGIN_SELECT_MENU)
        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        field = resolve_schema_field(inst, token)
        if not field or field.get("type") != "select":
            await call.answer("Список недоступен", show_alert=True)
            return
        key = field["key"]
        field_idx = int(token) if token.isdigit() else 0
        page = _field_page(inst, field_idx)

        options = field.get("options") or []
        if not options:
            await call.answer("Нет вариантов", show_alert=True)
            return

        cfg = await inst.plugin_settings.get_all(inst.UUID)
        current = cfg.get(key, field.get("default", ""))
        label = field.get("label", key)

        await call.message.edit_text(
            f"📋 <b>{label}</b>\nВыберите значение:",
            parse_mode="HTML",
            reply_markup=plugin_select_keyboard(
                uuid, field_idx, options, current, settings_page=page,
            ),
        )
        await call.answer()

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SELECT_SET))
    async def cb_plugin_select_set(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        body = call.data.replace(CBT.PLUGIN_SELECT_SET, "", 1)
        parts = body.split(":")
        if len(parts) < 3:
            await call.answer("Ошибка данных", show_alert=True)
            return
        uuid, field_token, idx_s = parts[0], parts[1], parts[2]
        try:
            field_idx = int(field_token)
            idx = int(idx_s)
        except ValueError:
            await call.answer("Ошибка", show_alert=True)
            return

        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        field = resolve_schema_field(inst, field_token)
        if not field:
            await call.answer("Поле не найдено", show_alert=True)
            return
        key = field["key"]
        page = _field_page(inst, field_idx)

        options = field.get("options") or []
        if idx < 0 or idx >= len(options):
            await call.answer("Вариант не найден", show_alert=True)
            return

        opt = options[idx]
        value = opt.get("value", opt.get("label", opt)) if isinstance(opt, dict) else opt

        async with loading_skeleton(call):
            await inst.apply_setting(key, value)
            await _show_settings(call, pm, uuid, page=page, skip_loading=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SCHEMA_ACT))
    async def cb_plugin_action(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        uuid, token = _parse_uuid_key(call.data, CBT.PLUGIN_SCHEMA_ACT)
        inst = _get_plugin_instance(pm, uuid)
        field = resolve_schema_field(inst, token) if inst else None
        key = field["key"] if field else token
        page = _field_page(inst, int(token)) if inst and token.isdigit() else 0
        if inst and hasattr(inst, "on_settings_action"):
            handled = await inst.on_settings_action(call, key)
            if handled:
                await _show_settings(call, pm, uuid, page=page, skip_loading=True)
                return
        await call.answer("Действие не реализовано", show_alert=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_RESET))
    async def cb_plugin_reset(call: CallbackQuery, state: FSMContext) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        await state.clear()
        uuid = call.data.replace(CBT.PLUGIN_RESET, "").split(":")[0]
        inst = _get_plugin_instance(pm, uuid)
        if inst:
            from core.plugins.settings_store import PluginSettingsStore

            store = PluginSettingsStore(ctx.db)
            await store.save_all(uuid, {})
            await call.answer("✅ Настройки сброшены")
            await _show_settings(call, pm, uuid)
            return
        await call.answer("Плагин не найден", show_alert=True)

    return router


async def _show_settings(
    call: CallbackQuery,
    pm: Any,
    uuid: str,
    *,
    page: int = 0,
    skip_loading: bool = False,
) -> None:
    rec = pm.plugins.get(uuid)
    if not rec:
        await call.answer("Плагин не найден", show_alert=True)
        return

    inst = rec.instance
    if inst and hasattr(inst, "render_settings_text") and hasattr(inst, "build_settings_keyboard"):
        async def render() -> None:
            try:
                text = await inst.render_settings_text(page=page)
                kb = await inst.build_settings_keyboard(page=page)
            except TypeError:
                text = await inst.render_settings_text()
                kb = await inst.build_settings_keyboard()
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

        try:
            if skip_loading:
                await asyncio.wait_for(render(), timeout=20.0)
            else:
                async with loading_skeleton(call):
                    await asyncio.wait_for(render(), timeout=20.0)
        except asyncio.TimeoutError:
            logger.error("plugin settings timeout %s page %s", uuid, page)
            try:
                await call.answer("Таймаут загрузки настроек", show_alert=True)
            except Exception:
                pass
        except Exception as exc:
            logger.exception("plugin settings %s page %s: %s", uuid, page, exc)
            try:
                await call.answer("Не удалось открыть настройки", show_alert=True)
            except Exception:
                pass
        return

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
    if skip_loading:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        async with loading_skeleton(call):
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
