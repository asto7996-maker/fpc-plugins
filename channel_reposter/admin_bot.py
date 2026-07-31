"""
admin_bot.py — красивая админ-панель управления.

Публикация контента — только через USERBOT (api_id / api_hash).
Этот бот управляет настройками и не постит в каналы.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
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
from scheduling import humanize_duration, parse_duration
from userbot_auth import AuthCredentials

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
    # login wizard
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


_db: Optional[Database] = None
_poster = None
_bot_username = ""
_rewrite_task: Optional[asyncio.Task] = None
_cycle_task: Optional[asyncio.Task] = None
_bridge = None
_bot = None
_started_at = time.monotonic()

# Ручной цикл не должен «висеть» вечно, если поток юзербота умер
CYCLE_TIMEOUT = 3600.0
REWRITE_TIMEOUT = 6 * 3600.0


def set_dependencies(
    db: Database,
    poster=None,
    bot_username: str = "",
    bridge=None,
    bot=None,
    **_kwargs,
) -> None:
    global _db, _poster, _bot_username, _bridge, _bot
    _db = db
    _poster = poster
    _bot_username = bot_username or ""
    _bridge = bridge
    _bot = bot


def _require_db() -> Database:
    assert _db is not None
    return _db


def _has_userbot() -> bool:
    return bool(_bridge and _bridge.auth and _bridge.auth.is_ready and _bridge.poster)


def _busy() -> bool:
    p = getattr(_bridge, "poster", None) if _bridge else None
    return bool(p and getattr(p, "_busy", False))


async def _call_poster(method_name: str, *args, timeout: Optional[float] = 120.0, **kwargs):
    poster = None
    if _bridge is not None and getattr(_bridge, "poster", None) is not None:
        poster = _bridge.poster
    else:
        poster = _poster
    assert poster is not None
    method = getattr(poster, method_name)
    if _bridge is not None and poster is getattr(_bridge, "poster", None):
        return await _bridge.call(method(*args, **kwargs), timeout=timeout)
    return await method(*args, **kwargs)


def _spawn(coro) -> asyncio.Task:
    return asyncio.create_task(coro)


def is_admin(uid: Optional[int]) -> bool:
    if uid is None:
        return False
    if not config.ADMIN_IDS:
        return True
    return uid in config.ADMIN_IDS


async def _ack(
    c: CallbackQuery,
    text: Optional[str] = None,
    *,
    show_alert: bool = False,
) -> None:
    """answer callback; никогда не роняет хендлер (query too old / already answered)."""
    try:
        await c.answer(text=text, show_alert=show_alert)
    except Exception as e:
        logger.debug("callback ack skipped: %s", e)


async def _deny(uid: Optional[int], reply: Callable) -> bool:
    if is_admin(uid):
        return False
    try:
        await reply("⛔ Только для администраторов.")
    except Exception:
        logger.debug("deny reply failed", exc_info=True)
    return True


async def _replace(c: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Обновить сообщение с меню, а если нельзя — прислать новое."""
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")  # type: ignore
    except Exception:
        try:
            await c.message.answer(text, reply_markup=kb, parse_mode="HTML")  # type: ignore
        except Exception:
            logger.debug("не удалось обновить меню", exc_info=True)


def _start_autopost(db: Database) -> None:
    """Включить автопост и попросить планировщик не ждать старый интервал."""
    db.set_running(True)
    db.clear_last_error()
    db.run_asap()


def _start_hint(db: Database) -> str:
    s = db.get_settings()
    return (
        f"Первый цикл — в течение нескольких секунд, дальше каждые "
        f"<b>{humanize_duration(s.interval_seconds)}</b> "
        f"по <b>{s.posts_per_cycle}</b> публикаций."
    )


async def _deny_cb(c: CallbackQuery) -> bool:
    if is_admin(c.from_user.id if c.from_user else None):
        return False
    await _ack(c, "⛔ Только для администраторов.", show_alert=True)
    return True


def _remember_admin(uid: Optional[int]) -> None:
    if uid and _db is not None:
        _db.set("staging_chat_id", str(uid))


# ----- keyboards -----

def menu_kb(running: bool, *, catchup: bool = False, notify: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="a:status"),
                InlineKeyboardButton(
                    text="⏸ Пауза" if running else "▶️ Старт",
                    callback_data="a:pause" if running else "a:start",
                ),
            ],
            [
                InlineKeyboardButton(text="⚡ Цикл сейчас", callback_data="a:run"),
                InlineKeyboardButton(text="🧪 Тест", callback_data="a:test"),
            ],
            [InlineKeyboardButton(text="🔐 Вход юзербота", callback_data="a:login")],
            [InlineKeyboardButton(text="✏️ Описание", callback_data="a:caption")],
            [
                InlineKeyboardButton(text="🔗 Старт-ссылка", callback_data="a:link"),
                InlineKeyboardButton(text="📜 С начала", callback_data="a:oldest"),
            ],
            [
                InlineKeyboardButton(text="📥 Источник", callback_data="a:source"),
                InlineKeyboardButton(text="📤 Назначение", callback_data="a:target"),
            ],
            [
                InlineKeyboardButton(text="⏱ Интервал", callback_data="a:interval"),
                InlineKeyboardButton(text="📦 Лимит", callback_data="a:limit"),
            ],
            [
                InlineKeyboardButton(
                    text=f"🚀 Догон: {'вкл' if catchup else 'выкл'}",
                    callback_data="a:catchup",
                ),
                InlineKeyboardButton(
                    text=f"🔔 Отчёты: {'вкл' if notify else 'выкл'}",
                    callback_data="a:notify",
                ),
            ],
            [
                InlineKeyboardButton(text="📝 Rewrite подписей", callback_data="a:rewrite"),
                InlineKeyboardButton(text="🧹 Разблокировать", callback_data="a:unstick"),
            ],
        ]
    )


def main_kb(db: Optional[Database] = None) -> InlineKeyboardMarkup:
    """Меню с актуальными состояниями тумблеров."""
    database = db or _db
    if database is None:
        return menu_kb(False)
    s = database.get_settings()
    return menu_kb(s.is_running, catchup=s.catchup_enabled, notify=s.notify_cycles)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="a:cancel")]]
    )


def interval_kb() -> InlineKeyboardMarkup:
    """Быстрый выбор интервала — без угадывания единиц измерения."""
    presets = [
        ("30 сек", 30),
        ("1 мин", 60),
        ("5 мин", 300),
        ("15 мин", 900),
        ("30 мин", 1800),
        ("1 ч", 3600),
        ("2 ч", 7200),
        ("6 ч", 21600),
        ("12 ч", 43200),
        ("24 ч", 86400),
    ]
    rows = []
    for i in range(0, len(presets), 3):
        rows.append(
            [
                InlineKeyboardButton(text=title, callback_data=f"a:iv:{secs}")
                for title, secs in presets[i : i + 3]
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="a:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _next_run_line(db: Database, s) -> str:
    if not s.is_running:
        return "⏭ Следующий цикл: <i>на паузе</i>"
    if _busy():
        return "⏭ Следующий цикл: <i>идёт прямо сейчас</i>"
    left = db.get_next_run() - time.time()
    if left <= 0:
        return "⏭ Следующий цикл: <b>вот-вот</b>"
    return f"⏭ Следующий цикл через <b>{humanize_duration(left)}</b>"


def status_text(db: Database) -> str:
    s = db.get_settings()
    st = "🟢 Работает" if s.is_running else "🔴 На паузе"
    engine = "🟢 USERBOT готов" if _has_userbot() else "🔴 нужен вход (api_id / api_hash)"
    busy = "⏳ публикация…" if _busy() else "idle"
    backlog = db.backlog()
    err_text, err_at = db.get_last_error()
    last_pub = db.get_last_published_at()

    lines = [
        "<b>✨ Channel Reposter</b>",
        "<i>Чистый юзербот · без Bot API заливки</i>",
        "",
        f"Движок: {engine}",
        f"Автопост: <b>{st}</b> · <code>{busy}</code>",
        _next_run_line(db, s),
        "",
        f"📥 Источник: <code>{s.source_channel or '—'}</code>",
        f"📤 Назначение: <code>{s.target_channel or '—'}</code>",
        f"📍 Progress: <code>{s.progress_id}</code> → next "
        f"<code>{s.progress_id + 1 if s.progress_id >= 0 else '—'}</code>",
        f"📚 В очереди: <b>{backlog}</b> ID"
        + (f" (последний в источнике <code>{db.get_latest_source_id()}</code>)" if backlog else ""),
        f"⏱ Интервал: <b>{humanize_duration(s.interval_seconds)}</b> · "
        f"за цикл: <b>{s.posts_per_cycle}</b>",
        f"🚀 Догон: <b>{'вкл' if s.catchup_enabled else 'выкл'}</b>"
        + (
            f" (каждые {humanize_duration(s.catchup_seconds)}, пока есть очередь)"
            if s.catchup_enabled
            else ""
        ),
        f"✅ Скопировано: <b>{db.history_count()}</b> · ошибок: <b>{db.error_count()}</b>",
    ]
    if last_pub:
        lines.append(
            f"🕒 Последняя публикация: <b>{humanize_duration(time.time() - last_pub)}</b> назад"
        )
    lines.append(f"🔗 Старт: <code>{s.start_link or 'не задан'}</code>")
    if err_text:
        ago = humanize_duration(time.time() - err_at) if err_at else "—"
        lines.append(f"⚠️ Последняя ошибка ({ago} назад): <code>{safe_preview(err_text, 160)}</code>")
    lines += [
        "",
        f"<b>Описание</b>\n<code>{safe_preview(s.caption_template, 220)}</code>",
        "",
        "<i>Порядок: от старых постов к новым.\n"
        "«📜 С начала» — с первого поста (история сбрасывается).\n"
        "«🔗 Старт-ссылка» — со следующего после указанного поста.</i>",
    ]
    return "\n".join(lines)


# ----- commands -----

@router.message(Command("start", "menu", "help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    _remember_admin(message.from_user.id if message.from_user else None)
    await state.clear()
    db = _require_db()
    await message.answer(
        "<b>✨ Channel Reposter</b>\n\n"
        "Перезалив каналов через <b>api_id + api_hash</b> (юзербот).\n"
        "Бот — только панель. Контент льёт аккаунт.\n\n"
        "1️⃣ «🔐 Вход» — api_id, api_hash, телефон, код\n"
        "2️⃣ Источник + назначение\n"
        "3️⃣ Ссылка на пост или «С начала»\n"
        "4️⃣ Описание → ⏱ Интервал → ▶️ Старт\n\n"
        "Команды: /status /run /pause /run_now /test\n"
        "/interval 2ч · /limit 5 · /reconnect · /login\n"
        "Если кажется, что бот повис: /ping и /unstick\n\n"
        + status_text(db),
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    await message.answer(
        status_text(db),
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    await state.set_state(S.api_id)
    await message.answer(
        "🔐 <b>Вход юзербота</b>\n\n"
        "1/5 — пришлите <b>api_id</b> (число с my.telegram.org)",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("run_now"))
async def cmd_run_now(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    await _do_run_now(message.answer)


@router.message(Command("test"))
async def cmd_test(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    await _run_test(message)


@router.message(Command("run"))
async def cmd_run(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    s = db.get_settings()
    if not s.source_channel or not s.target_channel:
        await message.answer("⚠️ Укажите источник и назначение.")
        return
    if s.progress_id < 0:
        await message.answer("⚠️ Сначала стартовая ссылка или «С начала».")
        return
    if not _has_userbot():
        await message.answer("❌ Сначала «🔐 Вход» (api_id / api_hash).")
        return
    _start_autopost(db)
    await message.answer(
        f"▶️ Автопостинг запущен.\n{_start_hint(db)}",
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    _require_db().set_running(False)
    await message.answer("⏸ Пауза.", reply_markup=main_kb(), parse_mode="HTML")


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    """
    Самая дешёвая проверка «жив ли бот».

    Не трогает ни БД, ни поток юзербота: если панель отвечает на /ping,
    но молчит на остальное — проблема не в поллинге.
    """
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    uptime = humanize_duration(time.monotonic() - _started_at)
    busy = "идёт цикл" if _busy() else "свободен"
    await message.answer(
        f"🏓 Панель жива. Работает {uptime}, движок: {busy}.",
        parse_mode="HTML",
    )


def _clear_stuck() -> list[str]:
    """Снять зависшие блокировки. Работает без потока юзербота."""
    global _cycle_task, _rewrite_task
    cleared: list[str] = []

    poster = getattr(_bridge, "poster", None) if _bridge else _poster
    if poster is not None and getattr(poster, "is_busy", False):
        seconds = getattr(poster, "busy_seconds", 0.0)
        poster.force_unlock()
        cleared.append(f"цикл висел {humanize_duration(seconds)}")

    for name, task in (("ручной цикл", _cycle_task), ("rewrite", _rewrite_task)):
        if task is not None and not task.done():
            task.cancel()
            cleared.append(f"фоновая задача «{name}» снята")
    _cycle_task = None
    _rewrite_task = None

    if _db is not None:
        _db.run_asap()
    return cleared


@router.message(Command("unstick"))
async def cmd_unstick(message: Message) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    await _do_unstick(message.answer)


@router.callback_query(F.data == "a:unstick")
async def cb_unstick(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    await _ack(c, "Снимаю блокировки")
    await _do_unstick(c.message.answer)  # type: ignore


async def _do_unstick(answer: Callable) -> None:
    cleared = _clear_stuck()
    db = _require_db()
    body = (
        "🧹 <b>Снято:</b>\n" + "\n".join(f"• {item}" for item in cleared)
        if cleared
        else "🧹 Зависших блокировок не найдено."
    )
    await answer(
        f"{body}\n\nСледующий цикл — при первой возможности.\n\n" + status_text(db),
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


@router.message(Command("interval"))
async def cmd_interval(message: Message, state: FSMContext) -> None:
    """/interval 2ч — быстро, без кнопок."""
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    raw = (message.text or "").partition(" ")[2].strip()
    db = _require_db()
    if not raw:
        await _ask_interval(message.answer, state)
        return
    try:
        seconds = parse_duration(raw)
        db.set_interval_seconds(seconds)
    except ValueError as e:
        await message.answer(f"❌ {e}", reply_markup=interval_kb())
        return
    await state.clear()
    db.run_asap()
    await message.answer(
        f"✅ Интервал: <b>{humanize_duration(seconds)}</b>\n{_interval_note(db, seconds)}",
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


@router.message(Command("limit"))
async def cmd_limit(message: Message, state: FSMContext) -> None:
    """/limit 5 — публикаций за цикл."""
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    raw = (message.text or "").partition(" ")[2].strip()
    if not raw:
        await state.set_state(S.limit)
        await message.answer(
            "📦 Сколько <b>публикаций</b> за один цикл?\n"
            "<i>Альбом = 1 публикация.</i>",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return
    db = _require_db()
    try:
        count = int(raw)
        if count < 1 or count > 100:
            raise ValueError("нужно целое число от 1 до 100")
        db.set_posts_per_cycle(count)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    await state.clear()
    s = db.get_settings()
    await message.answer(
        f"✅ Лимит: <b>{count}</b> публикаций каждые "
        f"<b>{humanize_duration(s.interval_seconds)}</b>",
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


@router.message(Command("reconnect"))
async def cmd_reconnect(message: Message) -> None:
    """Переподнять юзербота из сохранённой сессии (без кода)."""
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    if _bridge is None or _bridge.auth is None:
        await message.answer("❌ Юзербот не инициализирован.")
        return
    await message.answer("♻️ Переподключаю юзербота…")
    try:
        ok = await _bridge.call(_bridge.auth.ensure_started(), timeout=120)
    except Exception as e:
        await message.answer(f"❌ {e}")
        return
    if ok and _bridge.auth.client is not None:
        from poster import ChannelPoster

        _bridge.poster = ChannelPoster(_bridge.auth.client, _require_db())
        await message.answer("✅ Юзербот на связи.", reply_markup=main_kb())
    else:
        await message.answer(
            "❌ Не удалось. Нужен «🔐 Вход» заново.", reply_markup=main_kb()
        )


# ----- callbacks (answer FIRST) -----

@router.callback_query(F.data == "a:status")
async def cb_status(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    db = _require_db()
    text = status_text(db)
    kb = main_kb(db)
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")  # type: ignore
    except Exception:
        await c.message.answer(text, reply_markup=kb, parse_mode="HTML")  # type: ignore


@router.callback_query(F.data == "a:start")
async def cb_start(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    db = _require_db()
    s = db.get_settings()
    if not _has_userbot():
        await _ack(c, "Сначала вход юзербота", show_alert=True)
        return
    if not s.source_channel or not s.target_channel or s.progress_id < 0:
        await _ack(c, "Каналы + стартовая точка", show_alert=True)
        return
    _start_autopost(db)
    await _ack(c, "Старт")
    text = f"▶️ Запущено.\n{_start_hint(db)}\n\n" + status_text(db)
    await _replace(c, text, main_kb(db))


@router.callback_query(F.data == "a:pause")
async def cb_pause(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    db = _require_db()
    db.set_running(False)
    await _ack(c, "Пауза")
    await _replace(c, "⏸ Пауза.\n\n" + status_text(db), main_kb(db))


@router.callback_query(F.data == "a:catchup")
async def cb_catchup(c: CallbackQuery) -> None:
    """Догон: пока есть очередь — публикуем чаще пользовательского интервала."""
    if await _deny_cb(c):
        return
    db = _require_db()
    s = db.get_settings()
    new_state = not s.catchup_enabled
    db.set_catchup(new_state)
    if new_state:
        db.run_asap()
    await _ack(c, "Догон включён" if new_state else "Догон выключен")
    hint = (
        "🚀 <b>Догон включён</b>\n"
        f"Пока в источнике есть необработанные посты, цикл идёт каждые "
        f"<b>{humanize_duration(s.catchup_seconds)}</b>, а не раз в "
        f"{humanize_duration(s.interval_seconds)}.\n"
        "Как только очередь закончится — вернётся обычный интервал."
        if new_state
        else "🚀 <b>Догон выключен</b> — соблюдается только обычный интервал."
    )
    await _replace(c, hint + "\n\n" + status_text(db), main_kb(db))


@router.callback_query(F.data == "a:notify")
async def cb_notify(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    db = _require_db()
    new_state = not db.get_settings().notify_cycles
    db.set_notify_cycles(new_state)
    await _ack(c, "Отчёты включены" if new_state else "Отчёты выключены")
    text = (
        "🔔 Буду присылать отчёт после каждого успешного цикла."
        if new_state
        else "🔕 Отчёты о циклах выключены (об ошибках сообщу всё равно)."
    )
    await _replace(c, text + "\n\n" + status_text(db), main_kb(db))


@router.callback_query(F.data == "a:login")
async def cb_login(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    await state.set_state(S.api_id)
    await c.message.answer(  # type: ignore
        "🔐 <b>Вход юзербота</b>\n\n"
        "1/5 — <b>api_id</b> с my.telegram.org",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "a:caption")
async def cb_caption(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    await state.set_state(S.caption)
    await c.message.answer(  # type: ignore
        "✏️ Пришлите описание (жирный/курсив/ссылки — как в Telegram):",
        reply_markup=cancel_kb(),
    )


async def _ask_interval(answer: Callable, state: FSMContext) -> None:
    await state.set_state(S.interval)
    s = _require_db().get_settings()
    await answer(
        f"⏱ <b>Интервал между циклами</b>\n"
        f"Сейчас: <b>{humanize_duration(s.interval_seconds)}</b>\n\n"
        "Выберите кнопкой или напишите своё:\n"
        "<code>30с</code> · <code>15мин</code> · <code>2ч</code> · "
        "<code>1д</code> · <code>1ч 30мин</code>\n"
        "<i>Число без буквы = минуты.</i>",
        reply_markup=interval_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "a:interval")
async def cb_interval(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    await _ask_interval(c.message.answer, state)  # type: ignore


@router.callback_query(F.data.startswith("a:iv:"))
async def cb_interval_preset(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await state.clear()
    db = _require_db()
    try:
        seconds = float((c.data or "").split(":")[-1])
        db.set_interval_seconds(seconds)
    except (ValueError, TypeError):
        await _ack(c, "Не понял значение", show_alert=True)
        return
    db.run_asap()
    await _ack(c, f"Интервал: {humanize_duration(seconds)}")
    await _replace(
        c,
        f"✅ Интервал: <b>{humanize_duration(seconds)}</b>\n"
        f"{_interval_note(db, seconds)}\n\n" + status_text(db),
        main_kb(db),
    )


@router.callback_query(F.data.startswith("a:cu:"))
async def cb_catchup_preset(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    db = _require_db()
    try:
        seconds = float((c.data or "").split(":")[-1])
    except (ValueError, TypeError):
        await _ack(c, "Не понял значение", show_alert=True)
        return
    db.set_catchup_seconds(seconds)
    db.set_catchup(True)
    db.run_asap()
    await _ack(c, f"Догон: {humanize_duration(seconds)}")
    await _replace(
        c,
        f"🚀 Догон включён с интервалом <b>{humanize_duration(seconds)}</b>.\n\n"
        + status_text(db),
        main_kb(db),
    )


def _interval_note(db: Database, seconds: float) -> str:
    s = db.get_settings()
    note = (
        f"Каждый цикл — до <b>{s.posts_per_cycle}</b> публикаций, "
        f"значит примерно <b>{s.posts_per_cycle}</b> постов за "
        f"{humanize_duration(seconds)}."
    )
    if seconds < 60:
        note += "\n⚠️ Меньше минуты — Telegram может ограничить аккаунт (flood)."
    if s.catchup_enabled:
        note += (
            f"\n🚀 Догон включён: пока есть очередь, цикл идёт каждые "
            f"{humanize_duration(s.catchup_seconds)}."
        )
    return note


@router.callback_query(F.data == "a:limit")
async def cb_limit(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    await state.set_state(S.limit)
    await c.message.answer(  # type: ignore
        "📦 Сколько <b>публикаций</b> за один цикл?\n"
        "<i>Альбом = 1 публикация (не число файлов внутри).</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "a:link")
async def cb_link(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    await state.set_state(S.link)
    await c.message.answer(  # type: ignore
        "🔗 Ссылка на пост-источник:\n"
        "<code>https://t.me/channel/123</code>\n\n"
        "Указанный пост <b>не</b> публикуется — начнём со следующего.",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "a:oldest")
async def cb_oldest(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    await _ack(c, "Ищем…")
    if not _has_userbot():
        await c.message.answer("❌ Сначала вход юзербота.")  # type: ignore
        return
    await c.message.answer("📜 Ищу самый старый пост в источнике…")  # type: ignore

    async def _job():
        try:
            found = await _call_poster("seek_oldest", timeout=180)
            await c.message.answer(  # type: ignore
                f"✅ Режим: <b>от самого старого к новым</b>\n"
                f"Первый пост: <code>{found}</code>\n"
                f"Progress=<code>{found - 1}</code> → next <code>{found}</code>\n"
                f"История сброшена — пойдёт полная перезаливка с шаблоном.\n"
                f"Нажмите ▶️ Старт.",
                reply_markup=main_kb(),
                parse_mode="HTML",
            )
        except Exception as e:
            await c.message.answer(f"❌ {e}")  # type: ignore

    _spawn(_job())


@router.callback_query(F.data == "a:source")
async def cb_source(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    await state.set_state(S.source)
    await c.message.answer(  # type: ignore
        "📥 Источник — публичный <code>@username</code>\n"
        "<i>Админство не нужно, достаточно подписки аккаунта.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "a:target")
async def cb_target(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    await state.set_state(S.target)
    await c.message.answer(  # type: ignore
        "📤 Назначение — ваш канал.\n"
        "<i>Юзербот должен быть там админом с правом постить.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "a:run")
async def cb_run(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    await _ack(c, "Запуск…")
    await _do_run_now(c.message.answer)  # type: ignore


@router.callback_query(F.data == "a:test")
async def cb_test(c: CallbackQuery) -> None:
    if await _deny_cb(c):
        return
    await _ack(c, "Тест…")
    await _run_test(c.message)  # type: ignore


@router.callback_query(F.data == "a:rewrite")
async def cb_rewrite(c: CallbackQuery, state: FSMContext) -> None:
    if await _deny_cb(c):
        return
    await _ack(c)
    db = _require_db()
    s = db.get_settings()
    if not (s.caption_template or "").strip():
        await c.message.answer("⚠️ Сначала описание.")  # type: ignore
        return
    if s.target_channel:
        await state.update_data(rw_ch=s.target_channel)
        await state.set_state(S.rewrite_limit)
        await c.message.answer(  # type: ignore
            f"Канал: <code>{s.target_channel}</code>\nСколько постов? Число или <code>all</code>",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return
    await state.set_state(S.rewrite_channel)
    await c.message.answer("✍️ Канал:", reply_markup=cancel_kb())  # type: ignore


@router.callback_query(F.data == "a:cancel")
async def cb_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await _ack(c)
    await state.clear()
    db = _require_db()
    await c.message.answer("Отменено.", reply_markup=main_kb(db))  # type: ignore


# ----- FSM: login -----

@router.message(S.api_id)
async def on_api_id(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❌ api_id — только число")
        return
    await state.update_data(api_id=int(raw))
    await state.set_state(S.api_hash)
    await message.answer("2/5 — пришлите <b>api_hash</b>", parse_mode="HTML", reply_markup=cancel_kb())


@router.message(S.api_hash)
async def on_api_hash(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    h = (message.text or "").strip()
    if len(h) < 16:
        await message.answer("❌ Слишком короткий api_hash")
        return
    await state.update_data(api_hash=h)
    await state.set_state(S.phone)
    await message.answer(
        "3/5 — номер телефона в формате <code>+79001234567</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )


@router.message(S.phone)
async def on_phone(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    phone = (message.text or "").strip().replace(" ", "")
    data = await state.get_data()
    creds = AuthCredentials(
        api_id=int(data["api_id"]),
        api_hash=data["api_hash"],
        phone=phone,
    )
    await message.answer("📨 Отправляю код…")
    try:
        assert _bridge and _bridge.auth
        msg = await _bridge.call(_bridge.auth.begin_login(creds), timeout=60)
        await state.set_state(S.code)
        await message.answer(
            f"✅ {msg}\n\n4/5 — пришлите <b>код</b> из Telegram",
            parse_mode="HTML",
            reply_markup=cancel_kb(),
        )
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ {e}", reply_markup=main_kb())


@router.message(S.code)
async def on_code(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    code = (message.text or "").strip()
    try:
        assert _bridge and _bridge.auth
        result = await _bridge.call(_bridge.auth.confirm_code(code), timeout=60)
        if result == "password_required":
            await state.set_state(S.password)
            await message.answer("5/5 — облачный пароль 2FA:", reply_markup=cancel_kb())
            return
        await _after_login_ok(message, state)
    except Exception as e:
        await message.answer(f"❌ {e}")


@router.message(S.password)
async def on_password(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    try:
        assert _bridge and _bridge.auth
        await _bridge.call(_bridge.auth.confirm_password(message.text or ""), timeout=60)
        await _after_login_ok(message, state)
    except Exception as e:
        await message.answer(f"❌ {e}")


async def _after_login_ok(message: Message, state: FSMContext) -> None:
    await state.clear()
    db = _require_db()
    # Поднимаем poster на готовом клиенте: планировщик подхватит его сам,
    # перезапуск бота после входа не нужен.
    if _bridge and _bridge.auth and _bridge.auth.client:
        from poster import ChannelPoster

        _bridge.poster = ChannelPoster(_bridge.auth.client, db)
        db.run_asap()
    await message.answer(
        "✅ Юзербот авторизован.\nМожно задавать каналы и жать ▶️ Старт.",
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


# ----- FSM: settings -----

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
    await message.answer(
        f"✅ Описание сохранено.\n<code>{safe_preview(text, 400)}</code>",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.message(S.interval)
async def on_interval(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    try:
        seconds = parse_duration(message.text or "")
        db.set_interval_seconds(seconds)
    except ValueError as e:
        await message.answer(f"❌ {e}", reply_markup=interval_kb())
        return
    await state.clear()
    db.run_asap()
    await message.answer(
        f"✅ Интервал: <b>{humanize_duration(seconds)}</b>\n{_interval_note(db, seconds)}",
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


@router.message(S.limit)
async def on_limit(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    try:
        n = int((message.text or "").strip())
        if n < 1:
            raise ValueError("нужно целое число ≥ 1")
        if n > 100:
            raise ValueError("слишком много (макс. 100 за цикл)")
        _require_db().set_posts_per_cycle(n)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    await state.clear()
    await message.answer(
        f"✅ Лимит: <b>{n}</b> публикаций за цикл\n"
        f"<i>Альбом считается как 1 публикация (не по числу фото/видео).</i>",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.message(S.link)
async def on_link(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    link = (message.text or "").strip()
    await state.clear()
    await _apply_link(message, link)


@router.message(S.source)
async def on_source(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    from links import normalize_channel

    try:
        raw = normalize_channel((message.text or "").strip())
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    _require_db().set_source_channel(raw)
    await state.clear()
    await message.answer(
        f"✅ Источник: <code>{raw}</code>",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.message(S.target)
async def on_target(message: Message, state: FSMContext) -> None:
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    from links import normalize_channel

    try:
        raw = normalize_channel((message.text or "").strip())
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    _require_db().set_target_channel(raw)
    await state.clear()
    await message.answer(
        f"✅ Назначение: <code>{raw}</code>",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.message(S.rewrite_channel)
async def on_rw_ch(message: Message, state: FSMContext) -> None:
    await state.update_data(rw_ch=(message.text or "").strip())
    await state.set_state(S.rewrite_limit)
    await message.answer("Сколько постов? Число или <code>all</code>", parse_mode="HTML", reply_markup=cancel_kb())


@router.message(S.rewrite_limit)
async def on_rw_limit(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().lower()
    data = await state.get_data()
    await state.clear()
    limit = None if raw == "all" else int(raw) if raw.isdigit() else None
    if raw != "all" and limit is None:
        await message.answer("❌ Число или all")
        return
    await _run_rewrite(message, data.get("rw_ch", ""), limit)


async def _apply_link(message: Message, link: str) -> None:
    if not _has_userbot():
        # всё равно сохраним progress локально
        try:
            from links import normalize_channel

            chat, mid = parse_post_link(link)
            src = normalize_channel(chat)
            db = _require_db()
            db.set_source_channel(src)
            db.set_start_link(link)
            db.set_progress_id(mid)
            db.run_asap()
            await message.answer(
                f"✅ Ссылка сохранена (юзербот ещё не онлайн).\n"
                f"Источник <code>{src}</code> · next <code>{mid + 1}</code>",
                parse_mode="HTML",
                reply_markup=main_kb(db),
            )
            return
        except Exception as e:
            await message.answer(f"❌ {e}")
            return
    try:
        chat, mid = await _call_poster("apply_start_link", link, timeout=30)
        await message.answer(
            f"✅ Старт после <code>{mid}</code>\n"
            f"Чат: <code>{chat}</code>\n"
            f"Следующий пост: <code>{mid + 1}</code>\n"
            f"Фоновый цикл остановлен, история после этой точки сброшена.\n"
            f"Нажмите ▶️ Старт или ⚡ Цикл сейчас.",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
    except Exception as e:
        await message.answer(f"❌ {e}")


# ----- run / test (background) -----

async def _do_run_now(answer) -> None:
    global _cycle_task
    db = _require_db()
    s = db.get_settings()
    if not _has_userbot():
        await answer("❌ Сначала «🔐 Вход» юзербота.")
        return
    if not s.source_channel or not s.target_channel or s.progress_id < 0:
        await answer("⚠️ Нужны каналы и стартовая точка.")
        return
    if _cycle_task and not _cycle_task.done():
        await answer("⏳ Цикл уже идёт…")
        return
    await answer("⚡ Цикл в фоне. Кнопки свободны.")

    async def _job():
        try:
            # force=True — ручной цикл работает и на паузе, флаги не трогаем
            result = await _call_poster("run_cycle", timeout=CYCLE_TIMEOUT, force=True)
            await answer(_cycle_report(db, result), reply_markup=main_kb(db), parse_mode="HTML")
        except Exception as e:
            logger.exception("run_now")
            await answer(f"❌ {e}")

    _cycle_task = _spawn(_job())


def _cycle_report(db: Database, result) -> str:
    """Человеческий отчёт о цикле для панели."""
    published = getattr(result, "published", int(result or 0))
    reason = getattr(result, "reason", "")
    if published:
        text = f"✅ Опубликовано: <b>{published}</b>"
        backlog = getattr(result, "backlog", db.backlog())
        if backlog:
            text += f"\nОсталось в очереди: <b>{backlog}</b>"
        return text

    s = db.get_settings()
    hints = {
        "up_to_date": "Новых постов в источнике нет — всё уже перезалито.",
        "source_empty": "Источник пуст или недоступен аккаунту юзербота.",
        "no_start": "Не задана стартовая точка: «🔗 Старт-ссылка» или «📜 С начала».",
        "paused": "Автопостинг на паузе.",
        "aborted": "Цикл был прерван (например, новой командой).",
        "flood": (
            "Telegram просит подождать "
            f"{humanize_duration(getattr(result, 'flood_seconds', 0))} (flood)."
        ),
        "fatal": getattr(result, "fatal_text", "") or "критическая ошибка",
        "error": getattr(result, "error", "") or "ошибка цикла",
    }
    hint = hints.get(reason, "Публиковать было нечего.")
    return (
        f"⚠️ Опубликовано: <b>0</b>\n{hint}\n"
        f"<i>Progress <code>{s.progress_id}</code> → next "
        f"<code>{s.progress_id + 1}</code>, последний ID источника "
        f"<code>{db.get_latest_source_id() or '—'}</code></i>"
    )


async def _run_test(message: Message) -> None:
    global _cycle_task
    db = _require_db()
    s = db.get_settings()
    # Починить старые записи вида @https://t.me/...
    from links import normalize_channel

    try:
        if s.source_channel:
            fixed = normalize_channel(s.source_channel)
            if fixed != s.source_channel:
                db.set_source_channel(fixed)
                s = db.get_settings()
        if s.target_channel:
            fixed = normalize_channel(s.target_channel)
            if fixed != s.target_channel:
                db.set_target_channel(fixed)
                s = db.get_settings()
    except ValueError:
        pass

    lines = ["<b>🧪 Диагностика</b>\n"]
    src = (s.source_channel or "").strip()
    dst = (s.target_channel or "").strip()
    lines.append(f"Источник: <code>{src or '❌'}</code>")
    lines.append(f"Назначение: <code>{dst or '❌'}</code>")
    lines.append(f"Progress: <code>{s.progress_id}</code> → <code>{s.progress_id + 1 if s.progress_id >= 0 else '—'}</code>")
    lines.append(f"Юзербот: {'✅ онлайн' if _has_userbot() else '❌ нет сессии'}")
    lines.append(f"Занятость: {'⏳ цикл' if _busy() else 'idle'}")
    lines.append(f"Автопост: {'🟢 включён' if s.is_running else '🔴 на паузе'}")
    lines.append(_next_run_line(db, s).replace("⏭ ", "Расписание: "))
    lines.append(
        f"Интервал: <b>{humanize_duration(s.interval_seconds)}</b> · "
        f"за цикл: <b>{s.posts_per_cycle}</b> · "
        f"догон: {'вкл' if s.catchup_enabled else 'выкл'}"
    )
    lines.append(f"Очередь: <b>{db.backlog()}</b> ID")

    age = db.scheduler_age()
    if age is None:
        lines.append("Планировщик: ⚠️ ещё не отчитывался")
    elif age < 30:
        lines.append(f"Планировщик: ✅ жив ({age:.0f} сек назад)")
    else:
        lines.append(
            f"Планировщик: ❌ молчит {humanize_duration(age)} — перезапустите бота"
        )

    err_text, err_at = db.get_last_error()
    if err_text:
        ago = humanize_duration(time.time() - err_at) if err_at else "—"
        lines.append(f"Последняя ошибка ({ago} назад): <code>{safe_preview(err_text, 200)}</code>")
    recent = db.last_errors(3)
    if recent:
        lines.append("Проблемные посты: " + ", ".join(f"<code>{mid}</code>" for mid, _ in recent))

    if _has_userbot() and dst:
        if _busy():
            lines.append("Права в назначении: ⏳ цикл занят")
        else:
            try:
                async def _probe():
                    me = await _bridge.auth.client.get_me()
                    uname = f"@{me.username}" if me.username else me.first_name
                    from pyrogram import raw

                    # Сначала ResolveUsername — иначе CHANNEL_INVALID на холодной сессии
                    chat_id = dst
                    try:
                        username = dst.lstrip("@")
                        if username and not username.lstrip("-").isdigit():
                            r = await _bridge.auth.client.invoke(
                                raw.functions.contacts.ResolveUsername(username=username)
                            )
                            if hasattr(r.peer, "channel_id"):
                                chat_id = int(f"-100{r.peer.channel_id}")
                    except Exception as e:
                        return (
                            f"❌ канал не найден: <code>{e}</code>\n"
                            f"<i>Проверьте username назначения (не полную ссылку).</i>",
                            False,
                        )
                    try:
                        member = await _bridge.auth.client.get_chat_member(chat_id, me.id)
                        return (
                            f"✅ {uname} (id <code>{me.id}</code>) · <code>{member.status}</code>",
                            True,
                        )
                    except Exception:
                        try:
                            sent = await _bridge.auth.client.send_message(
                                chat_id, "⚙️ probe"
                            )
                            await _bridge.auth.client.delete_messages(chat_id, sent.id)
                            return f"✅ {uname} (id <code>{me.id}</code>) может писать", True
                        except Exception as e2:
                            return (
                                f"❌ нет доступа: <code>{e2}</code>\n"
                                f"<i>Добавьте юзербота {uname} (id <code>{me.id}</code>) "
                                f"админом в {dst} с правом «Публикация сообщений».</i>",
                                False,
                            )

                info, ok = await _bridge.call(_probe(), timeout=20)
                lines.append(f"Права в назначении: {info}")
            except Exception as e:
                lines.append(f"Права в назначении: ⚠️ {e}")

    await message.answer("\n".join(lines), parse_mode="HTML")

    if not _has_userbot():
        await message.answer(
            "Сделайте «🔐 Вход» (api_id / api_hash / телефон / код).",
            reply_markup=main_kb(),
        )
        return
    if _cycle_task and not _cycle_task.done():
        await message.answer("⏳ Уже публикуем в фоне.")
        return

    await message.answer("⏳ Тестовая публикация 1 поста…")

    async def _job():
        try:
            # Разовый лимит вместо правки настроек: сбой не испортит конфиг
            result = await _call_poster("run_cycle", 1, timeout=CYCLE_TIMEOUT, force=True)
            await message.answer(
                _cycle_report(db, result), reply_markup=main_kb(db), parse_mode="HTML"
            )
        except Exception as e:
            await message.answer(f"❌ {e}", reply_markup=main_kb(db))

    _cycle_task = _spawn(_job())


async def _run_rewrite(message: Message, channel: str, limit: Optional[int]) -> None:
    global _rewrite_task
    if _rewrite_task and not _rewrite_task.done():
        await message.answer("⚠️ Rewrite уже идёт")
        return
    if not _has_userbot():
        await message.answer("❌ Нужен юзербот")
        return
    await message.answer(f"⏳ Rewrite <code>{channel}</code>…", parse_mode="HTML")

    async def job():
        try:
            result = await _call_poster(
                "rewrite_captions_in_channel", channel, max_posts=limit, timeout=REWRITE_TIMEOUT
            )
            await message.answer(
                f"{'⏹' if result.get('cancelled') else '✅'} "
                f"scan={result.get('scanned')} upd={result.get('updated')} "
                f"skip={result.get('skipped')} err={result.get('errors')}",
                reply_markup=main_kb(),
            )
        except Exception as e:
            await message.answer(f"❌ {e}")

    _rewrite_task = _spawn(job())


@router.message()
async def on_unknown(message: Message) -> None:
    """Любое непонятое сообщение — не молчание, а меню с подсказкой."""
    if await _deny(message.from_user.id if message.from_user else None, message.answer):
        return
    _remember_admin(message.from_user.id if message.from_user else None)
    db = _require_db()
    await message.answer(
        "Не понял команду. Управление — кнопками ниже.\n"
        "Подсказка: /status — статус, /run_now — цикл сейчас, /test — диагностика, "
        "/ping — проверить, что панель жива.\n\n"
        + status_text(db),
        reply_markup=main_kb(db),
        parse_mode="HTML",
    )


def setup_dispatcher(dp: Dispatcher) -> None:
    dp.include_router(router)

    @dp.error()
    async def _on_error(event) -> bool:  # type: ignore[no-untyped-def]
        logger.exception("Update failed: %s", getattr(event, "exception", event))
        return True
