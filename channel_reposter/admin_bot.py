"""
admin_bot.py — админ-панель на aiogram 3.x.

Команды и inline-меню позволяют:
  • менять шаблон описания (HTML);
  • настраивать интервал и лимит постов за цикл;
  • задавать стартовую ссылку на пост;
  • запускать / ставить на паузу автопостинг;
  • смотреть статус.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from aiogram import Bot, Dispatcher, F, Router
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
from links import parse_post_link
from poster import ChannelPoster

logger = logging.getLogger(__name__)

router = Router()


# ---------------------------------------------------------------------------
# FSM-состояния для пошагового ввода настроек
# ---------------------------------------------------------------------------

class AdminStates(StatesGroup):
    waiting_caption = State()
    waiting_interval = State()
    waiting_limit = State()
    waiting_link = State()


# Зависимости внедряются из main.py через set_dependencies()
_db: Optional[Database] = None
_poster: Optional[ChannelPoster] = None
_trigger_cycle: Optional[Callable] = None


def set_dependencies(
    db: Database,
    poster: ChannelPoster,
    trigger_cycle: Optional[Callable] = None,
) -> None:
    """Привязать Database / Poster к хендлерам админки."""
    global _db, _poster, _trigger_cycle
    _db = db
    _poster = poster
    _trigger_cycle = trigger_cycle


def _require_db() -> Database:
    if _db is None:
        raise RuntimeError("Database не инициализирована")
    return _db


def _require_poster() -> ChannelPoster:
    if _poster is None:
        raise RuntimeError("Poster не инициализирован")
    return _poster


def is_admin(user_id: Optional[int]) -> bool:
    """Проверка, что пользователь есть в ADMIN_IDS."""
    if user_id is None:
        return False
    # Если список админов пуст — разрешаем всем (удобно при первом запуске)
    if not config.ADMIN_IDS:
        return True
    return user_id in config.ADMIN_IDS


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
                InlineKeyboardButton(
                    text="✏️ Текст описания", callback_data="admin:caption"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏱ Интервал (часы)", callback_data="admin:interval"
                ),
                InlineKeyboardButton(
                    text="📦 Постов за цикл", callback_data="admin:limit"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Стартовая ссылка", callback_data="admin:link"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚡ Запустить цикл сейчас", callback_data="admin:run_now"
                ),
            ],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")]
        ]
    )


def _status_text(db: Database) -> str:
    s = db.get_settings()
    state = "🟢 Работает" if s.is_running else "🔴 На паузе"
    caption_preview = s.caption_template.strip()
    if len(caption_preview) > 200:
        caption_preview = caption_preview[:200] + "…"
    if not caption_preview:
        caption_preview = "<i>(пусто)</i>"

    return (
        "<b>📊 Статус Channel Reposter</b>\n\n"
        f"Состояние: <b>{state}</b>\n"
        f"Источник: <code>{s.source_channel or config.SOURCE_CHANNEL}</code>\n"
        f"Назначение: <code>{s.target_channel or config.TARGET_CHANNEL}</code>\n"
        f"Progress ID (последний обработанный): <code>{s.progress_id}</code>\n"
        f"Следующий пост: <code>{s.progress_id + 1 if s.progress_id else '—'}</code>\n"
        f"Интервал: <b>{s.interval_hours}</b> ч.\n"
        f"Постов за цикл: <b>{s.posts_per_cycle}</b>\n"
        f"Успешно переслано: <b>{db.history_count()}</b>\n"
        f"Стартовая ссылка: <code>{s.start_link or 'не задана'}</code>\n\n"
        f"<b>Шаблон описания:</b>\n{caption_preview}"
    )


# ---------------------------------------------------------------------------
# Фильтр админа
# ---------------------------------------------------------------------------

async def _deny_if_not_admin(event_user_id: Optional[int], reply) -> bool:
    """Вернуть True, если доступ запрещён (и отправить ответ)."""
    if is_admin(event_user_id):
        return False
    await reply("⛔ Доступ только для администраторов.")
    return True


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@router.message(Command("start", "menu", "help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    await state.clear()
    db = _require_db()
    s = db.get_settings()
    await message.answer(
        "<b>Channel Reposter — админ-панель</b>\n\n"
        "Бот перезаливает медиа из канала-источника в канал-назначение "
        "с единым HTML-описанием.\n\n"
        "Используйте меню ниже или команды:\n"
        "/status — статус\n"
        "/set_caption — изменить описание\n"
        "/set_interval &lt;часы&gt;\n"
        "/set_limit &lt;число&gt;\n"
        "/set_link &lt;ссылка на пост&gt;\n"
        "/run — старт автопостинга\n"
        "/pause — пауза\n"
        "/run_now — выполнить цикл прямо сейчас",
        reply_markup=main_menu_kb(s.is_running),
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    await message.answer(
        _status_text(db),
        reply_markup=main_menu_kb(db.get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(Command("run"))
async def cmd_run(message: Message) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    db = _require_db()
    s = db.get_settings()
    if s.progress_id <= 0:
        await message.answer(
            "⚠️ Сначала задайте стартовую ссылку (/set_link или кнопка «Стартовая ссылка»).",
            parse_mode="HTML",
        )
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
    db = _require_db()
    db.set_running(False)
    await message.answer(
        "⏸ Автопостинг <b>на паузе</b>.",
        reply_markup=main_menu_kb(False),
        parse_mode="HTML",
    )


@router.message(Command("set_caption"))
async def cmd_set_caption(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    # Можно передать текст сразу: /set_caption <b>текст</b>
    if command.args and command.args.strip():
        _require_db().set_caption(command.args.strip())
        await message.answer("✅ Шаблон описания обновлён.", parse_mode="HTML")
        return
    await state.set_state(AdminStates.waiting_caption)
    await message.answer(
        "✏️ Пришлите новый шаблон описания.\n"
        "Поддерживается <b>HTML</b>: "
        "<code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
        "<code>&lt;i&gt;курсив&lt;/i&gt;</code>, "
        "<code>&lt;a href=\"https://...\"&gt;ссылка&lt;/a&gt;</code>.",
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
            await message.answer(
                f"✅ Интервал установлен: <b>{hours}</b> ч.",
                parse_mode="HTML",
            )
        except ValueError as e:
            await message.answer(f"❌ {e}")
        return
    await state.set_state(AdminStates.waiting_interval)
    await message.answer(
        "⏱ Введите интервал между циклами в <b>часах</b> (например <code>6</code> или <code>0.5</code>):",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("set_limit"))
async def cmd_set_limit(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        try:
            count = int(command.args.strip())
            _require_db().set_posts_per_cycle(count)
            await message.answer(
                f"✅ Постов за цикл: <b>{count}</b>",
                parse_mode="HTML",
            )
        except ValueError as e:
            await message.answer(f"❌ {e}")
        return
    await state.set_state(AdminStates.waiting_limit)
    await message.answer(
        "📦 Введите количество постов за один цикл (целое число ≥ 1):",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("set_link"))
async def cmd_set_link(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    if command.args and command.args.strip():
        await _apply_link(message, command.args.strip())
        return
    await state.set_state(AdminStates.waiting_link)
    await message.answer(
        "🔗 Пришлите ссылку на пост в канале-источнике.\n"
        "Публикация начнётся со <b>следующего</b> поста после указанного.\n\n"
        "Примеры:\n"
        "<code>https://t.me/c/123456789/500</code>\n"
        "<code>https://t.me/channel_username/500</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(Command("run_now"))
async def cmd_run_now(message: Message) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    await _do_run_now(message.answer)


# ---------------------------------------------------------------------------
# Callback-кнопки меню
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin:status")
async def cb_status(callback: CallbackQuery) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    db = _require_db()
    await callback.message.edit_text(  # type: ignore[union-attr]
        _status_text(db),
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
        await callback.answer("Сначала задайте стартовую ссылку", show_alert=True)
        return
    db.set_running(True)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "▶️ Автопостинг <b>запущен</b>.\n\n" + _status_text(db),
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
        "⏸ Автопостинг <b>на паузе</b>.\n\n" + _status_text(db),
        reply_markup=main_menu_kb(False),
        parse_mode="HTML",
    )
    await callback.answer("На паузе")


@router.callback_query(F.data == "admin:caption")
async def cb_caption(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_caption)
    await callback.message.answer(  # type: ignore[union-attr]
        "✏️ Пришлите новый шаблон описания (HTML).\n"
        "Пример:\n"
        "<code>&lt;b&gt;Каталог&lt;/b&gt; — &lt;a href=\"https://t.me/shop\"&gt;открыть&lt;/a&gt;</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:interval")
async def cb_interval(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_interval)
    await callback.message.answer(  # type: ignore[union-attr]
        "⏱ Введите интервал в часах:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:limit")
async def cb_limit(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_limit)
    await callback.message.answer(  # type: ignore[union-attr]
        "📦 Введите число постов за цикл:",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:link")
async def cb_link(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await state.set_state(AdminStates.waiting_link)
    await callback.message.answer(  # type: ignore[union-attr]
        "🔗 Пришлите ссылку на пост (старт со следующего):\n"
        "<code>https://t.me/c/123456789/500</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:run_now")
async def cb_run_now(callback: CallbackQuery) -> None:
    if await _deny_if_not_admin(callback.from_user.id, callback.answer):
        return
    await callback.answer("Запускаю цикл…")
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
# FSM: приём значений
# ---------------------------------------------------------------------------

@router.message(AdminStates.waiting_caption)
async def on_caption(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    # Берём HTML-текст, если клиент прислал форматирование
    text = message.html_text or message.text or message.caption or ""
    text = text.strip()
    if not text:
        await message.answer("❌ Пустой текст. Пришлите описание или /cancel.")
        return
    _require_db().set_caption(text)
    await state.clear()
    await message.answer(
        "✅ Шаблон описания сохранён. Он будет подставляться под все следующие посты.",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_interval)
async def on_interval(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        hours = float(raw)
        _require_db().set_interval_hours(hours)
    except ValueError as e:
        await message.answer(f"❌ {e}\nВведите число, например <code>6</code>.", parse_mode="HTML")
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
        f"✅ Постов за цикл: <b>{count}</b>",
        reply_markup=main_menu_kb(_require_db().get_settings().is_running),
        parse_mode="HTML",
    )


@router.message(AdminStates.waiting_link)
async def on_link(message: Message, state: FSMContext) -> None:
    if await _deny_if_not_admin(message.from_user.id if message.from_user else None, message.answer):
        return
    link = (message.text or "").strip()
    await state.clear()
    await _apply_link(message, link)


# ---------------------------------------------------------------------------
# Вспомогательные действия
# ---------------------------------------------------------------------------

async def _apply_link(message: Message, link: str) -> None:
    poster = _require_poster()
    db = _require_db()
    try:
        # Быстрая валидация формата
        parse_post_link(link)
        chat_ref, msg_id = await poster.apply_start_link(link)
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    except Exception as e:
        logger.exception("Ошибка применения ссылки")
        await message.answer(f"❌ Не удалось применить ссылку: {e}")
        return

    await message.answer(
        "✅ Стартовая точка сохранена.\n"
        f"Чат: <code>{chat_ref}</code>\n"
        f"Указанный пост ID: <code>{msg_id}</code> <i>(не публикуется)</i>\n"
        f"Следующий к публикации: <code>{msg_id + 1}</code>",
        reply_markup=main_menu_kb(db.get_settings().is_running),
        parse_mode="HTML",
    )


async def _do_run_now(answer) -> None:
    db = _require_db()
    poster = _require_poster()
    if db.get_progress_id() <= 0:
        await answer(
            "⚠️ Сначала задайте стартовую ссылку.",
            parse_mode="HTML",
        )
        return

    # Временно включим флаг running на время цикла
    was_running = db.get_settings().is_running
    db.set_running(True)
    await answer("⚡ Запускаю цикл публикации…")
    try:
        if _trigger_cycle is not None:
            count = await _trigger_cycle()
        else:
            count = await poster.run_cycle()
        await answer(
            f"✅ Цикл завершён. Опубликовано: <b>{count}</b>",
            reply_markup=main_menu_kb(db.get_settings().is_running),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("Ошибка ручного цикла")
        await answer(f"❌ Ошибка цикла: {e}")
    finally:
        # Если до этого был на паузе — вернём паузу
        if not was_running:
            db.set_running(False)


def setup_dispatcher(dp: Dispatcher) -> None:
    """Подключить роутер админки к диспетчеру aiogram."""
    dp.include_router(router)
