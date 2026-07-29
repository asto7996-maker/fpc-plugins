"""
admin_bot.py — админ-панель (только Bot API, без api_id/api_hash).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

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


class S(StatesGroup):
    caption = State()
    interval = State()
    limit = State()
    link = State()
    source = State()
    target = State()
    rewrite_channel = State()
    rewrite_limit = State()


_db: Optional[Database] = None
_poster = None
_trigger: Optional[Callable] = None
_bot_username = ""
_rewrite_task: Optional[asyncio.Task] = None
_bridge = None
_bot = None


def set_dependencies(
    db: Database,
    poster=None,
    trigger_cycle: Optional[Callable] = None,
    bot_username: str = "",
    bridge=None,
    bot=None,
    **_kwargs,
) -> None:
    global _db, _poster, _trigger, _bot_username, _bridge, _bot
    _db = db
    _poster = poster
    _trigger = trigger_cycle
    _bot_username = bot_username or ""
    _bridge = bridge
    _bot = bot


def _require_db() -> Database:
    assert _db is not None
    return _db


def _require_poster():
    """Юзербот-poster если есть, иначе Bot API fallback."""
    if _bridge is not None and getattr(_bridge, "poster", None) is not None:
        return _bridge.poster
    assert _poster is not None
    return _poster


def _has_userbot() -> bool:
    return bool(_bridge and _bridge.auth and _bridge.auth.is_ready and _bridge.poster)


async def _call_poster(method_name: str, *args, **kwargs):
    """Вызов метода poster в правильном loop (worker или текущий)."""
    poster = _require_poster()
    method = getattr(poster, method_name)
    if _bridge is not None and poster is getattr(_bridge, "poster", None):
        return await _bridge.call(method(*args, **kwargs))
    return await method(*args, **kwargs)


def is_admin(uid: Optional[int]) -> bool:
    if uid is None:
        return False
    if not config.ADMIN_IDS:
        return True
    return uid in config.ADMIN_IDS


async def _deny(uid: Optional[int], reply) -> bool:
    if is_admin(uid):
        return False
    await reply("⛔ Только для администраторов.")
    return True


def menu_kb(running: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="a:status"),
                InlineKeyboardButton(
                    text="⏸ Пауза" if running else "▶️ Старт",
                    callback_data="a:pause" if running else "a:start",
                ),
            ],
            [InlineKeyboardButton(text="✏️ Текст описания", callback_data="a:caption")],
            [
                InlineKeyboardButton(
                    text="📝 Применить шаблон к опубликованным",
                    callback_data="a:rewrite",
                )
            ],
            [
                InlineKeyboardButton(text="⏱ Интервал", callback_data="a:interval"),
                InlineKeyboardButton(text="📦 Лимит", callback_data="a:limit"),
            ],
            [InlineKeyboardButton(text="🔗 Старт-ссылка", callback_data="a:link")],
            [
                InlineKeyboardButton(text="📥 Источник", callback_data="a:source"),
                InlineKeyboardButton(text="📤 Назначение", callback_data="a:target"),
            ],
            [
                InlineKeyboardButton(text="⚡ Цикл сейчас", callback_data="a:run"),
                InlineKeyboardButton(text="🧪 Тест", callback_data="a:test"),
            ],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="a:cancel")]]
    )


def status_text(db: Database) -> str:
    s = db.get_settings()
    st = "🟢 Работает" if s.is_running else "🔴 На паузе"
    bot = f"@{_bot_username}" if _bot_username else "бот"
    engine = "🟢 USERBOT (читает источник)" if _has_userbot() else "🔴 нет сессии копирования"
    return (
        f"<b>📊 Channel Reposter</b>\n\n"
        f"Админ-бот: <code>{bot}</code>\n"
        f"Движок копирования: {engine}\n"
        f"Автопостинг: <b>{st}</b>\n"
        f"Источник: <code>{s.source_channel or '—'}</code>\n"
        f"Назначение: <code>{s.target_channel or '—'}</code>\n"
        f"Progress ID: <code>{s.progress_id}</code>\n"
        f"Следующий: <code>{s.progress_id + 1 if s.progress_id else '—'}</code>\n"
        f"Интервал: <b>{s.interval_hours}</b> ч. | За цикл: <b>{s.posts_per_cycle}</b>\n"
        f"Скопировано: <b>{db.history_count()}</b>\n"
        f"Старт-ссылка: <code>{s.start_link or 'не задана'}</code>\n\n"
        f"<b>Описание:</b>\n<code>{safe_preview(s.caption_template, 250)}</code>\n\n"
        f"<i>{bot} — админ только в вашем канале. "
        f"Чтение источника — через сохранённый аккаунт. /test</i>"
    )


# ----- commands -----

def _remember_admin(uid: Optional[int]) -> None:
    """Сохраняем чат админа — нужен как staging для загрузки file_id альбомов."""
    if uid and _db is not None:
        _db.set("staging_chat_id", str(uid))


@router.message(Command("start", "menu", "help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    _remember_admin(message.from_user.id if message.from_user else None)
    await state.clear()
    db = _require_db()
    bot = f"@{_bot_username}" if _bot_username else "бота"
    await message.answer(
        "<b>Channel Reposter</b>\n\n"
        f"1️⃣ {bot} — <b>админ только в вашем</b> канале.\n"
        "2️⃣ Источник — публичный @channel (бот туда не нужен).\n"
        "3️⃣ Старт-ссылка, описание, «▶️ Старт».\n\n"
        "Схема: аккаунт читает источник → бот публикует у вас.\n"
        "<b>/test</b> — диагностика + пробный пост.\n"
        "/status /set_source /set_target /set_link /set_caption /run /pause /run_now",
        reply_markup=menu_kb(db.get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    await message.answer(
        status_text(db),
        reply_markup=menu_kb(db.get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(Command("run"))
async def cmd_run(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    s = db.get_settings()
    if not s.source_channel or not s.target_channel:
        await message.answer("⚠️ Укажите источник и назначение.")
        return
    if s.progress_id <= 0:
        await message.answer("⚠️ Сначала стартовая ссылка.")
        return
    db.set_running(True)
    await message.answer("▶️ Автопостинг запущен.", reply_markup=menu_kb(True), parse_mode="HTML")


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    _require_db().set_running(False)
    await message.answer("⏸ Пауза.", reply_markup=menu_kb(False), parse_mode="HTML")


@router.message(Command("run_now"))
async def cmd_run_now(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    await _do_run_now(message.answer)


@router.message(Command("set_caption"))
async def cmd_caption(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        err = validate_telegram_html(command.args.strip())
        if err:
            await message.answer(f"❌ {err}", parse_mode="HTML")
            return
        _require_db().set_caption(command.args.strip())
        await message.answer("✅ Описание обновлено.")
        return
    await state.set_state(S.caption)
    await message.answer(
        "✏️ Пришлите описание одним сообщением.\n"
        "Форматируйте как в Telegram — HTML соберётся сам.",
        reply_markup=cancel_kb(),
    )


@router.message(Command("set_interval"))
async def cmd_interval(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args:
        try:
            h = float(command.args.strip().replace(",", "."))
            _require_db().set_interval_hours(h)
            await message.answer(f"✅ Интервал: <b>{h}</b> ч.", parse_mode="HTML")
        except ValueError as e:
            await message.answer(f"❌ {e}")
        return
    await state.set_state(S.interval)
    await message.answer("⏱ Часы между циклами:", reply_markup=cancel_kb())


@router.message(Command("set_limit"))
async def cmd_limit(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args:
        try:
            n = int(command.args.strip())
            _require_db().set_posts_per_cycle(n)
            await message.answer(f"✅ Лимит: <b>{n}</b>", parse_mode="HTML")
        except ValueError as e:
            await message.answer(f"❌ {e}")
        return
    await state.set_state(S.limit)
    await message.answer("📦 Постов за цикл:", reply_markup=cancel_kb())


@router.message(Command("set_link"))
async def cmd_link(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args:
        await _apply_link(message, command.args.strip())
        return
    await state.set_state(S.link)
    await message.answer(
        "🔗 Ссылка на пост (публикация со <b>следующего</b>):\n"
        "<code>https://t.me/channel/123</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("set_source"))
async def cmd_source(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args:
        _require_db().set_source_channel(command.args.strip())
        await message.answer(f"✅ Источник: <code>{command.args.strip()}</code>", parse_mode="HTML")
        return
    await state.set_state(S.source)
    await message.answer(
        "📥 Канал-источник — публичный <code>@username</code>\n"
        "<i>Админом бота там делать не нужно.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("set_target"))
async def cmd_target(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args:
        _require_db().set_target_channel(command.args.strip())
        await message.answer(f"✅ Назначение: <code>{command.args.strip()}</code>", parse_mode="HTML")
        return
    await state.set_state(S.target)
    await message.answer(
        "📤 Ваш канал-назначение (@name или -100…)\n"
        f"<i>Сюда добавьте бота админом.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("rewrite", "apply_caption"))
async def cmd_rewrite(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    args = (command.args or "").strip().split()
    if args:
        channel = args[0]
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        await _run_rewrite(message, channel, limit)
        return
    await _prompt_rewrite(message, state)


@router.message(Command("cancel_rewrite", "stop_rewrite"))
async def cmd_cancel_rewrite(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    poster = _require_poster()
    if hasattr(poster, "cancel_rewrite"):
        poster.cancel_rewrite()
    await message.answer("⏹ Запрошена остановка rewrite.")


@router.message(Command("test"))
async def cmd_test(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    await _run_test(message)


# ----- callbacks -----

@router.callback_query(F.data == "a:status")
async def cb_status(c: CallbackQuery) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    db = _require_db()
    await c.message.edit_text(  # type: ignore[union-attr]
        status_text(db), reply_markup=menu_kb(db.get_settings().is_running), parse_mode="HTML"
    )
    await c.answer()


@router.callback_query(F.data == "a:start")
async def cb_start(c: CallbackQuery) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    db = _require_db()
    s = db.get_settings()
    if not s.source_channel or not s.target_channel or s.progress_id <= 0:
        await c.answer("Сначала каналы и стартовая ссылка", show_alert=True)
        return
    db.set_running(True)
    await c.message.edit_text(  # type: ignore[union-attr]
        "▶️ Запущено.\n\n" + status_text(db),
        reply_markup=menu_kb(True),
        parse_mode="HTML",
    )
    await c.answer("Старт")


@router.callback_query(F.data == "a:pause")
async def cb_pause(c: CallbackQuery) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    db = _require_db()
    db.set_running(False)
    await c.message.edit_text(  # type: ignore[union-attr]
        "⏸ Пауза.\n\n" + status_text(db),
        reply_markup=menu_kb(False),
        parse_mode="HTML",
    )
    await c.answer("Пауза")


@router.callback_query(F.data == "a:caption")
async def cb_caption(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await state.set_state(S.caption)
    await c.message.answer(  # type: ignore[union-attr]
        "✏️ Пришлите описание (можно с форматированием Telegram):",
        reply_markup=cancel_kb(),
    )
    await c.answer()


@router.callback_query(F.data == "a:interval")
async def cb_interval(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await state.set_state(S.interval)
    await c.message.answer("⏱ Часы:", reply_markup=cancel_kb())  # type: ignore[union-attr]
    await c.answer()


@router.callback_query(F.data == "a:limit")
async def cb_limit(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await state.set_state(S.limit)
    await c.message.answer("📦 Число:", reply_markup=cancel_kb())  # type: ignore[union-attr]
    await c.answer()


@router.callback_query(F.data == "a:link")
async def cb_link(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await state.set_state(S.link)
    await c.message.answer("🔗 Ссылка на пост:", reply_markup=cancel_kb())  # type: ignore[union-attr]
    await c.answer()


@router.callback_query(F.data == "a:source")
async def cb_source(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await state.set_state(S.source)
    await c.message.answer(  # type: ignore[union-attr]
        "📥 Источник — публичный <code>@username</code>\n"
        "<i>Админство бота в источнике не нужно.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "a:target")
async def cb_target(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await state.set_state(S.target)
    await c.message.answer(  # type: ignore[union-attr]
        "📤 Ваш канал (назначение).\n"
        "<i>Бот должен быть там админом.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "a:run")
async def cb_run(c: CallbackQuery) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await c.answer("Запуск…")
    await _do_run_now(c.message.answer)  # type: ignore[union-attr]


@router.callback_query(F.data == "a:test")
async def cb_test(c: CallbackQuery) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await c.answer()
    await _run_test(c.message)  # type: ignore[arg-type]


@router.callback_query(F.data == "a:rewrite")
async def cb_rewrite(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny(c.from_user.id, c.answer):
        return
    await c.answer()
    await _prompt_rewrite(c.message, state)  # type: ignore[arg-type]


@router.callback_query(F.data == "a:cancel")
async def cb_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    db = _require_db()
    await c.message.answer("Отменено.", reply_markup=menu_kb(db.get_settings().is_running))  # type: ignore[union-attr]
    await c.answer()


# ----- FSM -----

@router.message(S.caption)
async def on_caption(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    text = extract_caption_html(message)
    if not text:
        await message.answer("❌ Пусто")
        return
    err = validate_telegram_html(text)
    if err:
        await message.answer(f"❌ {err}", parse_mode="HTML")
        return
    _require_db().set_caption(text)
    await state.clear()
    s = _require_db().get_settings()
    await message.answer(
        f"✅ Сохранено.\n<code>{safe_preview(text, 400)}</code>",
        reply_markup=menu_kb(s.is_running),
        parse_mode="HTML",
    )


@router.message(S.interval)
async def on_interval(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    try:
        h = float((message.text or "").strip().replace(",", "."))
        _require_db().set_interval_hours(h)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    await state.clear()
    await message.answer(
        f"✅ {h} ч.",
        reply_markup=menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(S.limit)
async def on_limit(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    try:
        n = int((message.text or "").strip())
        _require_db().set_posts_per_cycle(n)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    await state.clear()
    await message.answer(
        f"✅ Лимит {n}",
        reply_markup=menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(S.link)
async def on_link(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    await state.clear()
    await _apply_link(message, (message.text or "").strip())


@router.message(S.source)
async def on_source(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    v = (message.text or "").strip()
    _require_db().set_source_channel(v)
    await state.clear()
    await message.answer(
        f"✅ Источник: <code>{v}</code>",
        reply_markup=menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(S.target)
async def on_target(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    v = (message.text or "").strip()
    _require_db().set_target_channel(v)
    await state.clear()
    await message.answer(
        f"✅ Назначение: <code>{v}</code>",
        reply_markup=menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(S.rewrite_channel)
async def on_rw_ch(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    await state.update_data(rw_ch=(message.text or "").strip())
    await state.set_state(S.rewrite_limit)
    await message.answer(
        "Сколько последних опубликованных постов обновить?\n"
        "Число или <code>all</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(S.rewrite_limit)
async def on_rw_lim(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    raw = (message.text or "").strip().lower()
    try:
        limit = None if raw in {"all", "все", "*"} else int(raw)
        if limit is not None and limit < 1:
            raise ValueError("Число ≥ 1")
    except ValueError:
        await message.answer("❌ Число или <code>all</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    await state.clear()
    await _run_rewrite(message, data.get("rw_ch") or "", limit)


# ----- helpers -----

async def _apply_link(message: Message, link: str) -> None:
    try:
        chat, mid = parse_post_link(link)
        if isinstance(chat, int):
            await message.answer(
                "⚠️ Приватная ссылка t.me/c/…\n"
                "Лучше публичная: <code>https://t.me/username/123</code>",
                parse_mode="HTML",
            )
        chat, mid = await _call_poster("apply_start_link", link)
    except Exception as e:
        await message.answer(f"❌ {e}")
        return
    await message.answer(
        f"✅ Старт: <code>{chat}</code>\n"
        f"Указанный ID <code>{mid}</code> не публикуется\n"
        f"Следующий: <code>{mid + 1}</code>",
        reply_markup=menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


async def _do_run_now(answer) -> None:
    db = _require_db()
    s = db.get_settings()
    if not s.source_channel or not s.target_channel or s.progress_id <= 0:
        await answer("⚠️ Нужны каналы и стартовая ссылка. Сначала /test")
        return
    if not _has_userbot():
        await answer(
            "❌ Нет сессии для чтения источника.\n"
            "Обычный бот не видит посты чужого канала, если он там не админ.\n"
            "Нужна рабочая сессия юзербота (уже была) или /login."
        )
        return
    was = s.is_running
    db.set_running(True)
    await answer("⚡ Цикл…")
    try:
        n = await _call_poster("run_cycle")
        await answer(
            f"✅ Опубликовано: <b>{n}</b>",
            reply_markup=menu_kb(db.get_settings().is_running),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("run_now")
        await answer(f"❌ {e}")
    finally:
        if not was:
            db.set_running(False)


async def _run_test(message: Message) -> None:
    """Полная диагностика + пробная публикация 1 поста."""
    db = _require_db()
    s = db.get_settings()
    lines = ["<b>🧪 Тест-режим</b>\n"]
    ok_flags = []

    # 1) settings
    src = (s.source_channel or "").strip()
    dst = (s.target_channel or "").strip()
    if src and not src.startswith("@") and not src.lstrip("-").isdigit():
        src = "@" + src
        db.set_source_channel(src)
    if dst and not dst.startswith("@") and not dst.lstrip("-").isdigit():
        dst = "@" + dst
        db.set_target_channel(dst)

    lines.append(f"Источник: <code>{src or '❌ не задан'}</code>")
    lines.append(f"Назначение: <code>{dst or '❌ не задан'}</code>")
    lines.append(f"Progress: <code>{s.progress_id}</code> → next <code>{s.progress_id + 1 if s.progress_id else '—'}</code>")
    ok_flags.append(bool(src and dst and s.progress_id > 0))

    # 2) bot admin in target
    if _bot is None or not dst:
        lines.append("Бот в назначении: ❌ нет бота/канала")
        ok_flags.append(False)
    else:
        try:
            me = await _bot.get_me()
            member = await _bot.get_chat_member(dst, me.id)
            can_post = getattr(member, "can_post_messages", None)
            status = member.status
            good = status in ("administrator", "creator") and (can_post in (True, None) or status == "creator")
            lines.append(
                f"Бот админ в вашем канале: {'✅' if good else '❌'} "
                f"(status=<code>{status}</code>, can_post=<code>{can_post}</code>)"
            )
            ok_flags.append(bool(good))
        except Exception as e:
            lines.append(f"Бот админ в вашем канале: ❌ {e}")
            ok_flags.append(False)

    # 3) userbot session
    if _has_userbot():
        try:
            b = _bridge
            st = await b.call(b.auth.status_text())
            lines.append(f"Сессия копирования: ✅ {st}")
            ok_flags.append(True)
        except Exception as e:
            lines.append(f"Сессия копирования: ❌ {e}")
            ok_flags.append(False)
    else:
        lines.append(
            "Сессия копирования: ❌ нет\n"
            "<i>Без неё Bot API не читает чужой канал (бот не может быть «подписчиком»).</i>"
        )
        ok_flags.append(False)

    # 4) userbot can see source
    if _has_userbot() and src:
        try:
            async def _probe_source():
                client = _bridge.auth.client
                chat = await client.get_chat(src)
                return f"{chat.title} (id={chat.id})"

            info = await _bridge.call(_probe_source())
            lines.append(f"Источник доступен аккаунту: ✅ {info}")
            ok_flags.append(True)
        except Exception as e:
            lines.append(f"Источник доступен аккаунту: ❌ {e}")
            ok_flags.append(False)

    # 5) Bot API copy probe (expected fail if bot not in source)
    if _bot and src and dst and s.progress_id > 0:
        mid = s.progress_id + 1
        try:
            sent = await _bot.copy_message(chat_id=dst, from_chat_id=src, message_id=mid)
            lines.append(f"Bot API copy #{mid}: ✅ (msg {sent.message_id})")
            try:
                await _bot.delete_message(dst, sent.message_id)
            except Exception:
                pass
        except Exception as e:
            lines.append(
                f"Bot API copy #{mid}: ❌ <code>{e}</code>\n"
                f"<i>Это нормально, если бот не админ в источнике — копируем юзерботом.</i>"
            )

    # 6) Real publish test via userbot (1 post), then optional note
    await message.answer("\n".join(lines), parse_mode="HTML")

    if not _has_userbot():
        await message.answer(
            "🛠 Чтобы заработало без добавления бота в источник — нужна сессия аккаунта.\n"
            "Если раньше уже делали /login, перезапустите процесс; иначе выполните /login один раз.",
            reply_markup=menu_kb(db.get_settings().is_running),
        )
        return

    await message.answer("⏳ Пробую опубликовать <b>1</b> пост юзерботом…", parse_mode="HTML")
    was = db.get_settings().is_running
    db.set_running(True)
    # temporarily limit 1
    old_limit = db.get_settings().posts_per_cycle
    db.set_posts_per_cycle(1)
    try:
        n = await _call_poster("run_cycle")
        if n > 0:
            await message.answer(
                f"✅ Тест успешен: опубликовано <b>{n}</b> пост(ов).\nМожно жать «Старт».",
                reply_markup=menu_kb(was),
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "⚠️ Движок отработал, но 0 постов.\n"
                "Возможны дыры в ID после progress, нет медиа, или аккаунт не видит посты.\n"
                "Попробуйте обновить стартовую ссылку на свежий пост.",
                reply_markup=menu_kb(was),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.exception("test cycle")
        await message.answer(f"❌ Ошибка теста: {e}", reply_markup=menu_kb(was))
    finally:
        db.set_posts_per_cycle(old_limit)
        db.set_running(was)


async def _prompt_rewrite(message: Message, state: FSMContext) -> None:
    db = _require_db()
    s = db.get_settings()
    if not (s.caption_template or "").strip():
        await message.answer("⚠️ Сначала задайте описание.")
        return
    target = s.target_channel
    if target:
        await state.update_data(rw_ch=target)
        await state.set_state(S.rewrite_limit)
        await message.answer(
            f"Канал: <code>{target}</code>\n"
            "Сколько последних опубликованных постов обновить?\n"
            "Число или <code>all</code>",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return
    await state.set_state(S.rewrite_channel)
    await message.answer("✍️ Канал назначения:", reply_markup=cancel_kb())


async def _run_rewrite(message: Message, channel: str, limit: Optional[int]) -> None:
    global _rewrite_task
    if not channel:
        await message.answer("❌ Канал не задан")
        return
    if _rewrite_task and not _rewrite_task.done():
        await message.answer("⚠️ Rewrite уже идёт. /cancel_rewrite")
        return

    await message.answer(f"⏳ Обновляю подписи в <code>{channel}</code>…", parse_mode="HTML")

    async def job():
        try:
            result = await _call_poster("rewrite_captions_in_channel", channel, max_posts=limit)
            st = "⏹ Отменено" if result.get("cancelled") else "✅ Готово"
            await message.answer(
                f"{st}\n"
                f"Просмотрено: <b>{result.get('scanned', 0)}</b>\n"
                f"Обновлено: <b>{result.get('updated', 0)}</b>\n"
                f"Пропущено: <b>{result.get('skipped', 0)}</b>\n"
                f"Ошибок: <b>{result.get('errors', 0)}</b>",
                reply_markup=menu_kb(_require_db().get_settings().is_running),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("rewrite")
            await message.answer(f"❌ {e}")

    _rewrite_task = asyncio.create_task(job())


def setup_dispatcher(dp: Dispatcher) -> None:
    dp.include_router(router)
