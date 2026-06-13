"""
Настройки плагинов — полный UI: bool, text, int, select, action.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from handlers.tg.loading import loading_skeleton
from core.plugins.tg_callback import field_key_by_index, parse_field_cb, parse_select_set_cb
from keyboards import cbt as CBT
from keyboards.plugin_settings import plugin_select_keyboard

logger = logging.getLogger("starvell.handlers.plugin_settings")


class PluginSettingsStates(StatesGroup):
    waiting_value = State()


def _parse_uuid_key(payload: str, prefix: str) -> tuple[str, str]:
    """Legacy: uuid:key — для обратной совместимости."""
    body = payload.replace(prefix, "", 1)
    uuid, _, key = body.partition(":")
    return uuid, key


def _resolve_field_key(inst: Any, uuid: str, payload: str, prefix: str) -> tuple[str, str | None]:
    """Разбор callback: сначала индекс поля, иначе legacy key."""
    body = payload.replace(prefix, "", 1)
    uuid_part, _, tail = body.partition(":")
    if tail.isdigit():
        key = field_key_by_index(inst, int(tail))
        return uuid_part, key
    return _parse_uuid_key(payload, prefix)


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


def create_plugin_settings_router(ctx: Any) -> Router:
    router = Router(name="plugin_settings")
    pm = ctx.plugin_manager

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SETTINGS))
    async def cb_plugin_settings(call: CallbackQuery, state: FSMContext) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        await state.clear()
        uuid = call.data.replace(CBT.PLUGIN_SETTINGS, "").split(":")[0]
        await _show_settings(call, pm, uuid)

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
        inst_probe = None
        try:
            uuid_probe, idx = parse_field_cb(call.data, CBT.PLUGIN_SETTING)
            inst_probe = _get_plugin_instance(pm, uuid_probe)
            if inst_probe:
                key_probe = field_key_by_index(inst_probe, idx)
                if key_probe:
                    uuid, key = uuid_probe, key_probe
                else:
                    uuid, key = _parse_uuid_key(call.data, CBT.PLUGIN_SETTING)
            else:
                uuid, key = _parse_uuid_key(call.data, CBT.PLUGIN_SETTING)
        except ValueError:
            uuid, key = _parse_uuid_key(call.data, CBT.PLUGIN_SETTING)
        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        field = inst.get_schema_field(key) if hasattr(inst, "get_schema_field") else None
        if not field or field.get("type") != "bool":
            await call.answer("Настройка недоступна", show_alert=True)
            return
        async with loading_skeleton(call):
            await inst.on_setting_toggle(key)
            await _show_settings(call, pm, uuid, skip_loading=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_EDIT))
    async def cb_plugin_edit_field(call: CallbackQuery, state: FSMContext) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        inst_probe = _get_plugin_instance(pm, call.data.replace(CBT.PLUGIN_EDIT, "").split(":")[0])
        if inst_probe:
            uuid, key = _resolve_field_key(inst_probe, "", call.data, CBT.PLUGIN_EDIT)
        else:
            uuid, key = _parse_uuid_key(call.data, CBT.PLUGIN_EDIT)
        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        field = inst.get_schema_field(key)
        if not field:
            await call.answer("Поле не найдено", show_alert=True)
            return

        ftype = field.get("type", "str")
        if ftype not in ("text", "str", "multiline", "int"):
            await call.answer("Редактирование недоступно", show_alert=True)
            return

        cfg = await inst.plugin_settings.get_all(inst.UUID)
        current = cfg.get(key, field.get("default", ""))
        label = field.get("label", key)
        hint = field.get("description", "")

        await state.set_state(PluginSettingsStates.waiting_value)
        await state.update_data(uuid=uuid, key=key)

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

        text_page = await inst.render_settings_text()
        kb = await inst.build_settings_keyboard()
        await message.answer(f"✅ Сохранено: <b>{field.get('label', key)}</b>", parse_mode="HTML")
        await message.answer(text_page, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SELECT_MENU))
    async def cb_plugin_select_menu(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        inst_probe = _get_plugin_instance(pm, call.data.replace(CBT.PLUGIN_SELECT_MENU, "").split(":")[0])
        if inst_probe:
            uuid, key = _resolve_field_key(inst_probe, "", call.data, CBT.PLUGIN_SELECT_MENU)
            field_idx = next(
                (i for i, f in enumerate(inst_probe.get_settings_schema()) if f.get("key") == key),
                0,
            )
        else:
            uuid, key = _parse_uuid_key(call.data, CBT.PLUGIN_SELECT_MENU)
            field_idx = 0
        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        field = inst.get_schema_field(key)
        if not field or field.get("type") != "select":
            await call.answer("Список недоступен", show_alert=True)
            return

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
            reply_markup=plugin_select_keyboard(uuid, field_idx, options, current),
        )
        await call.answer()

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SELECT_SET))
    async def cb_plugin_select_set(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        try:
            uuid, field_idx, idx = parse_select_set_cb(call.data, CBT.PLUGIN_SELECT_SET)
        except ValueError:
            body = call.data.replace(CBT.PLUGIN_SELECT_SET, "", 1)
            parts = body.split(":")
            if len(parts) < 3:
                await call.answer("Ошибка данных", show_alert=True)
                return
            uuid, key_legacy, idx_s = parts[0], parts[1], parts[2]
            inst_tmp = _get_plugin_instance(pm, uuid)
            field_idx = 0
            if inst_tmp and not key_legacy.isdigit():
                found = next(
                    (i for i, f in enumerate(inst_tmp.get_settings_schema()) if f.get("key") == key_legacy),
                    None,
                )
                if found is not None:
                    field_idx = found
            try:
                idx = int(idx_s)
            except ValueError:
                await call.answer("Ошибка", show_alert=True)
                return

        inst = _get_plugin_instance(pm, uuid)
        if not inst:
            await call.answer("Плагин не загружен", show_alert=True)
            return
        key = field_key_by_index(inst, field_idx)
        if not key:
            await call.answer("Поле не найдено", show_alert=True)
            return
        field = inst.get_schema_field(key)
        if not field:
            await call.answer("Поле не найдено", show_alert=True)
            return

        options = field.get("options") or []
        if idx < 0 or idx >= len(options):
            await call.answer("Вариант не найден", show_alert=True)
            return

        opt = options[idx]
        value = opt.get("value", opt.get("label", opt)) if isinstance(opt, dict) else opt

        async with loading_skeleton(call):
            await inst.apply_setting(key, value)
            await _show_settings(call, pm, uuid, skip_loading=True)

    @router.callback_query(F.data.startswith(CBT.PLUGIN_SCHEMA_ACT))
    async def cb_plugin_action(call: CallbackQuery) -> None:
        if not await ctx._has_access(call.from_user.id):
            return
        inst_probe = _get_plugin_instance(pm, call.data.replace(CBT.PLUGIN_SCHEMA_ACT, "").split(":")[0])
        if inst_probe:
            uuid, key = _resolve_field_key(inst_probe, "", call.data, CBT.PLUGIN_SCHEMA_ACT)
        else:
            uuid, key = _parse_uuid_key(call.data, CBT.PLUGIN_SCHEMA_ACT)
        inst = _get_plugin_instance(pm, uuid)
        if inst and hasattr(inst, "on_settings_action"):
            handled = await inst.on_settings_action(call, key)
            if handled:
                await _show_settings(call, pm, uuid, skip_loading=True)
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


async def _show_settings(call: CallbackQuery, pm: Any, uuid: str, skip_loading: bool = False) -> None:
    rec = pm.plugins.get(uuid)
    if not rec:
        await call.answer("Плагин не найден", show_alert=True)
        return

    inst = rec.instance
    if inst and hasattr(inst, "render_settings_text") and hasattr(inst, "build_settings_keyboard"):
        async def render() -> None:
            try:
                text = await inst.render_settings_text()
                kb = await inst.build_settings_keyboard()
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception as exc:
                logger.exception("settings render %s: %s", uuid, exc)
                await call.answer(f"Ошибка настроек: {exc}", show_alert=True)
                raise

        if skip_loading:
            try:
                await render()
            except Exception:
                pass
        else:
            async with loading_skeleton(call):
                try:
                    await render()
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
