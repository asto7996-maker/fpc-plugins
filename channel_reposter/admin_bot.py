"""
admin_bot.py — админ-панель aiogram 3.x + мастер входа юзербота.

Вход аккаунта (/login):
  API_ID → API_HASH → телефон → код → пароль 2FA

Описание постов:
  Просто пришлите сообщение с обычным форматированием Telegram
  (жирный, курсив, ссылка «в слово») — HTML соберётся сам.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
from database import Database
from formatting import extract_caption_html, safe_preview, validate_telegram_html
from links import parse_post_link

logger = logging.getLogger(__name__)
router = Router()


class AdminStates(StatesGroup):
    waiting_caption = State()
    waiting_interval = State()
    waiting_limit = State()
    waiting_link = State()
    waiting_source = State()
    waiting_target = State()
    # --- auth ---
    auth_api_id = State()
    auth_api_hash = State()
    auth_phone = State()
    auth_code = State()
    auth_password = State()


_db: Optional[Database] = None
_poster = None
_trigger_cycle: Optional[Callable] = None
_auth = None
_on_userbot_ready: Optional[Callable[[], Awaitable[None]]] = None


def set_dependencies(
    db: Database,
    poster,
    trigger_cycle: Optional[Callable] = None,
    auth=None,
    on_userbot_ready: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    global _db, _poster, _trigger_cycle, _auth, _on_userbot_ready
    _db = db
    _poster = poster
    _trigger_cycle = trigger_cycle
    _auth = auth
    _on_userbot_ready = on_userbot_ready


def _require_db() -> Database:
    if _db is None:
        raise RuntimeError("Database не инициализирована")
    return _db


def _require_poster():
    if _poster is None:
        raise RuntimeError("Poster не инициализирован")
    return _poster


def is_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if not config.ADMIN_IDS:
        return True
    return user_id in config.ADMIN_IDS


async def _deny_if_not_admin(event_user_id: Optional[int], reply) -> bool:
    if is_admin(event_user_id):
        return False
    await reply("⛔ Доступ только для администраторов.")
    return True


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def main_menu_kb(is_running: bool) -> InlineKeyboardMarkup:
    toggle_text = "⏸ Пауза" if is_running else "▶️ Старт"
    toggle_cb = "admin:pause" if is_running else "admin:start"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="admin:status"),
                InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb),
            ],
            [
                InlineKeyboardButton(text="🔐 Вход юзербота", callback_data="admin:login"),
            ],
            [
                InlineKeyboardButton(text="✏️ Текст описания", callback_data="admin:caption"),
            ],
            [
                InlineKeyboardButton(text="⏱ Интервал", callback_data="admin:interval"),
                InlineKeyboardButton(text="📦 Лимит", callback_data="admin:limit"),
            ],
            [
                InlineKeyboardButton(text="🔗 Старт-ссылка", callback_data="admin:link"),
            ],
            [
                InlineKeyboardButton(text="📥 Источник", callback_data="admin:source"),
                InlineKeyboardButton(text="📤 Назначение", callback_data="admin:target"),
            ],
            [
                InlineKeyboardButton(text="⚡ Цикл сейчас", callback_data="admin:run_now"),
            ],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")]
        ]
    )


async def _status_text(db: Database) -> str:
    s = db.get_settings()
    state = "🟢 Работает" if s.is_running else "🔴 На паузе"
    caption_preview = safe_preview(s.caption_template, 250) or "<i>(пусто)</i>"

    auth_line = "🔴 Юзербот не авторизован"
    if _auth is not None:
        try:
            auth_line = await _auth.status_text()
        except Exception as e:
            auth_line = f"⚠️ Статус юзербота: {e}"

    engine = "USERBOT" if (_auth and _auth.is_ready) else "Bot API (fallback)"

    return (
        "<b>📊 Channel Reposter</b>\n\n"
        f"{auth_line}\n"
        f"Движок: <b>{engine}</b>\n"
        f"Автопостинг: <b>{state}</b>\n"
        f"Источник: <code>{s.source_channel or '—'}</code>\n"
        f"Назначение: <code>{s.target_channel or '—'}</code>\n"
        f"Progress ID: <code>{s.progress_id}</code>\n"
        f"Следующий: <code>{s.progress_id + 1 if s.progress_id else '—'}</code>\n"
        f"Интервал: <b>{s.interval_hours}</b> ч. | "
        f"За цикл: <b>{s.posts_per_cycle}</b>\n"
        f"Успешно: <b>{db.history_count()}</b>\n"
        f"Старт-ссылка: <code>{s.start_link or 'не задана'}</code>\n\n"
        f"<b>Описание (превью):</b>\n<code>{caption_preview}</code>"
    )


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@router.message(Command("start", "menu", "help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    await state.clear()
    db = _require_db()
    await message.answer(
        "<b>Channel Reposter</b>\n\n"
        "Юзербот копирует медиа из канала-источника в назначение "
        "<b>без</b> метки Forwarded from.\n"
        "В источнике достаточно подписки; в назначении — права админа у аккаунта.\n\n"
        "1️⃣ <b>/login</b> — вход аккаунта (api_id, api_hash, телефон, код, пароль)\n"
        "2️⃣ Укажите каналы и стартовую ссылку\n"
        "3️⃣ Задайте описание: просто напишите текст с жирным/ссылками — "
        "разметку собирать вручную <b>не нужно</b>\n"
        "4️⃣ Старт автопостинга",
        reply_markup=main_menu_kb(db.get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    await message.answer(
        await _status_text(db),
        reply_markup=main_menu_kb(db.get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(Command("login", "auth"))
async def cmd_login(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    await _start_login(message, state)


@router.message(Command("run"))
async def cmd_run(message: Message) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    if db.get_progress_id() <= 0:
        await message.answer("⚠️ Сначала задайте стартовую ссылку.")
        return
    if _auth is None or not _auth.is_ready:
        await message.answer("⚠️ Сначала авторизуйте юзербота: /login")
        return
    db.set_running(True)
    await message.answer(
        "▶️ Автопостинг <b>запущен</b>.",
        reply_markup=main_menu_kb(True),
        parse_mode="HTML",
    )


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    _require_db().set_running(False)
    await message.answer(
        "⏸ Автопостинг <b>на паузе</b>.",
        reply_markup=main_menu_kb(False),
        parse_mode="HTML",
    )


@router.message(Command("set_caption"))
async def cmd_set_caption(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        # Аргументы команды — как есть (HTML от пользователя)
        err = validate_telegram_html(command.args.strip())
        if err:
            await message.answer(f"❌ {err}", parse_mode="HTML")
            return
        _require_db().set_caption(command.args.strip())
        await message.answer("✅ Описание обновлено.")
        return
    await state.set_state(AdminStates.waiting_caption)
    await message.answer(
        "✏️ Пришлите <b>одно сообщение</b> с описанием.\n\n"
        "Как в обычном чате: выделите жирный/курсив, вставьте ссылку в слово — "
        "бот <b>сам</b> сохранит форматирование.\n"
        "Ручная HTML-вёрстка не обязательна (но тоже принимается).",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("set_interval"))
async def cmd_set_interval(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        try:
            hours = float(command.args.strip().replace(",", "."))
            _require_db().set_interval_hours(hours)
            await message.answer(f"✅ Интервал: <b>{hours}</b> ч.", parse_mode="HTML")
        except ValueError as e:
            await message.answer(f"❌ {e}")
        return
    await state.set_state(AdminStates.waiting_interval)
    await message.answer("⏱ Интервал в часах:", reply_markup=cancel_kb())


@router.message(Command("set_limit"))
async def cmd_set_limit(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        try:
            count = int(command.args.strip())
            _require_db().set_posts_per_cycle(count)
            await message.answer(f"✅ Лимит: <b>{count}</b>", parse_mode="HTML")
        except ValueError as e:
            await message.answer(f"❌ {e}")
        return
    await state.set_state(AdminStates.waiting_limit)
    await message.answer("📦 Постов за цикл:", reply_markup=cancel_kb())


@router.message(Command("set_link"))
async def cmd_set_link(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        await _apply_link(message, command.args.strip())
        return
    await state.set_state(AdminStates.waiting_link)
    await message.answer(
        "🔗 Ссылка на пост (публикация со <b>следующего</b>):\n"
        "<code>https://t.me/c/123456789/500</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("set_source"))
async def cmd_set_source(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        _require_db().set_source_channel(command.args.strip())
        await message.answer(f"✅ Источник: <code>{command.args.strip()}</code>", parse_mode="HTML")
        return
    await state.set_state(AdminStates.waiting_source)
    await message.answer("📥 Канал-источник (@user или -100...):", reply_markup=cancel_kb())


@router.message(Command("set_target"))
async def cmd_set_target(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        _require_db().set_target_channel(command.args.strip())
        await message.answer(f"✅ Назначение: <code>{command.args.strip()}</code>", parse_mode="HTML")
        return
    await state.set_state(AdminStates.waiting_target)
    await message.answer("📤 Канал-назначение:", reply_markup=cancel_kb())


@router.message(Command("run_now"))
async def cmd_run_now(message: Message) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    await _do_run_now(message.answer)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin:status")
async def cb_status(callback: CallbackQuery) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    db = _require_db()
    await callback.message.edit_text(  # type: ignore[union-attr]
        await _status_text(db),
        reply_markup=main_menu_kb(db.get_settings().is_running),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:start")
async def cb_start(callback: CallbackQuery) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    db = _require_db()
    if db.get_progress_id() <= 0:
        await callback.answer("Сначала стартовая ссылка", show_alert=True)
        return
    if _auth is None or not _auth.is_ready:
        await callback.answer("Сначала /login юзербота", show_alert=True)
        return
    db.set_running(True)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "▶️ Автопостинг запущен.\n\n" + await _status_text(db),
        reply_markup=main_menu_kb(True),
        parse_mode="HTML",
    )
    await callback.answer("Запущено")


@router.callback_query(F.data == "admin:pause")
async def cb_pause(callback: CallbackQuery) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    db = _require_db()
    db.set_running(False)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "⏸ На паузе.\n\n" + await _status_text(db),
        reply_markup=main_menu_kb(False),
        parse_mode="HTML",
    )
    await callback.answer("Пауза")


@router.callback_query(F.data == "admin:login")
async def cb_login(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await callback.answer()
    await _start_login(callback.message, state)  # type: ignore[arg-type]


@router.callback_query(F.data == "admin:caption")
async def cb_caption(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_caption)
    await callback.message.answer(  # type: ignore[union-attr]
        "✏️ Пришлите описание одним сообщением.\n"
        "Форматируйте как в Telegram — HTML соберётся автоматически.",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:interval")
async def cb_interval(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_interval)
    await callback.message.answer("⏱ Часы:", reply_markup=cancel_kb())  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "admin:limit")
async def cb_limit(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_limit)
    await callback.message.answer("📦 Число постов:", reply_markup=cancel_kb())  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "admin:link")
async def cb_link(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_link)
    await callback.message.answer(  # type: ignore[union-attr]
        "🔗 Ссылка на пост:", reply_markup=cancel_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "admin:source")
async def cb_source(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_source)
    await callback.message.answer("📥 Источник:", reply_markup=cancel_kb())  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "admin:target")
async def cb_target(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_target)
    await callback.message.answer("📤 Назначение:", reply_markup=cancel_kb())  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "admin:run_now")
async def cb_run_now(callback: CallbackQuery) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await callback.answer("Запуск…")
    await _do_run_now(callback.message.answer)  # type: ignore[union-attr]


@router.callback_query(F.data == "admin:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    db = _require_db()
    await callback.message.answer(  # type: ignore[union-attr]
        "Отменено.",
        reply_markup=main_menu_kb(db.get_settings().is_running),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Auth FSM
# ---------------------------------------------------------------------------

async def _start_login(message: Message, state: FSMContext) -> None:
    if _auth is None:
        await message.answer("❌ Auth-модуль не подключён")
        return

    # Подставим уже сохранённые значения как подсказку
    creds = _auth.load_credentials()
    hint = ""
    if creds and creds.api_id:
        hint = f"\n\nРанее: api_id=<code>{creds.api_id}</code>, phone=<code>{creds.phone or '—'}</code>"

    await state.set_state(AdminStates.auth_api_id)
    await message.answer(
        "🔐 <b>Вход юзербота</b>\n\n"
        "1/5 — пришлите <b>API_ID</b> (число с https://my.telegram.org/apps)."
        f"{hint}",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AdminStates.auth_api_id)
async def on_auth_api_id(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❌ API_ID должен быть числом")
        return
    await state.update_data(api_id=int(raw))
    await state.set_state(AdminStates.auth_api_hash)
    await message.answer(
        "2/5 — пришлите <b>API_HASH</b> (строка с my.telegram.org).",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AdminStates.auth_api_hash)
async def on_auth_api_hash(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    raw = (message.text or "").strip()
    if len(raw) < 16:
        await message.answer("❌ Похоже на некорректный API_HASH")
        return
    await state.update_data(api_hash=raw)
    await state.set_state(AdminStates.auth_phone)
    await message.answer(
        "3/5 — номер телефона аккаунта в международном формате:\n"
        "<code>+79001234567</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AdminStates.auth_phone)
async def on_auth_phone(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    phone = (message.text or "").strip()
    if len(phone) < 8:
        await message.answer("❌ Слишком короткий номер")
        return

    data = await state.get_data()
    from userbot_auth import AuthCredentials

    creds = AuthCredentials(
        api_id=int(data["api_id"]),
        api_hash=data["api_hash"],
        phone=phone,
        password="",
    )
    await message.answer("⏳ Отправляю код…")
    try:
        info = await _auth.begin_login(creds)
    except Exception as e:
        logger.exception("begin_login")
        await message.answer(f"❌ Не удалось отправить код: {e}")
        await state.clear()
        return

    await state.set_state(AdminStates.auth_code)
    await message.answer(
        f"✅ {info}\n\n"
        "4/5 — пришлите <b>код</b> из Telegram/SMS (только цифры).",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AdminStates.auth_code)
async def on_auth_code(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    code = (message.text or "").strip()
    try:
        result = await _auth.confirm_code(code)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    except Exception as e:
        logger.exception("confirm_code")
        await message.answer(f"❌ Ошибка входа: {e}")
        await state.clear()
        return

    if result == "password_required":
        await state.set_state(AdminStates.auth_password)
        await message.answer(
            "5/5 — включена двухфакторка. Пришлите <b>облачный пароль</b> 2FA.",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return

    await state.clear()
    if _on_userbot_ready:
        await _on_userbot_ready()
    me_line = await _auth.status_text()
    await message.answer(
        f"✅ Вход выполнен!\n{me_line}\n\n"
        "Теперь укажите каналы, стартовую ссылку и описание — затем «Старт».",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(AdminStates.auth_password)
async def on_auth_password(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    password = (message.text or "").strip()
    # Удалим сообщение с паролем из чата, если можем
    try:
        await message.delete()
    except Exception:
        pass

    try:
        await _auth.confirm_password(password)
    except Exception as e:
        logger.exception("confirm_password")
        await message.answer(f"❌ Неверный пароль или ошибка: {e}")
        return

    await state.clear()
    if _on_userbot_ready:
        await _on_userbot_ready()
    me_line = await _auth.status_text()
    await message.answer(
        f"✅ 2FA принят, вход выполнен!\n{me_line}",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Обычные FSM
# ---------------------------------------------------------------------------

@router.message(AdminStates.waiting_caption)
async def on_caption(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    text = extract_caption_html(message)
    if not text:
        await message.answer("❌ Пустой текст")
        return
    err = validate_telegram_html(text)
    if err:
        await message.answer(f"❌ {err}", parse_mode="HTML")
        return
    _require_db().set_caption(text)
    await state.clear()
    # Превью показываем экранированным — битый HTML не уронит ответ
    await message.answer(
        "✅ Описание сохранено. Форматирование (жирный / курсив / ссылки) учтено.\n\n"
        f"<b>Превью:</b>\n<code>{safe_preview(text, 400)}</code>",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_interval)
async def on_interval(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    try:
        hours = float((message.text or "").strip().replace(",", "."))
        _require_db().set_interval_hours(hours)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    await state.clear()
    await message.answer(
        f"✅ Интервал: <b>{hours}</b> ч.",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_limit)
async def on_limit(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    try:
        count = int((message.text or "").strip())
        _require_db().set_posts_per_cycle(count)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    await state.clear()
    await message.answer(
        f"✅ Лимит: <b>{count}</b>",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_link)
async def on_link(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    await state.clear()
    await _apply_link(message, (message.text or "").strip())


@router.message(AdminStates.waiting_source)
async def on_source(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    value = (message.text or "").strip()
    _require_db().set_source_channel(value)
    await state.clear()
    await message.answer(
        f"✅ Источник: <code>{value}</code>",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_target)
async def on_target(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    value = (message.text or "").strip()
    _require_db().set_target_channel(value)
    await state.clear()
    await message.answer(
        f"✅ Назначение: <code>{value}</code>",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _apply_link(message: Message, link: str) -> None:
    poster = _require_poster()
    db = _require_db()
    try:
        parse_post_link(link)
        chat_ref, msg_id = await poster.apply_start_link(link)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    except Exception as e:
        logger.exception("apply_start_link")
        await message.answer(f"❌ {e}")
        return

    await message.answer(
        "✅ Старт сохранён.\n"
        f"Чат: <code>{chat_ref}</code>\n"
        f"Указанный ID: <code>{msg_id}</code> <i>(не публикуется)</i>\n"
        f"Следующий: <code>{msg_id + 1}</code>",
        reply_markup=main_menu_kb(db.get_settings().is_running),
        parse_mode="HTML",
    )


async def _do_run_now(answer) -> None:
    db = _require_db()
    if db.get_progress_id() <= 0:
        await answer("⚠️ Сначала стартовая ссылка.")
        return
    if _auth is None or not _auth.is_ready:
        await answer("⚠️ Сначала авторизуйте юзербота: /login")
        return

    was_running = db.get_settings().is_running
    db.set_running(True)
    await answer("⚡ Цикл…")
    try:
        count = await _trigger_cycle() if _trigger_cycle else await _require_poster().run_cycle()
        await answer(
            f"✅ Готово. Опубликовано: <b>{count}</b>",
            reply_markup=main_menu_kb(db.get_settings().is_running),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("run_now")
        await answer(f"❌ {e}")
    finally:
        if not was_running:
            db.set_running(False)


def setup_dispatcher(dp: Dispatcher) -> None:
    dp.include_router(router)
