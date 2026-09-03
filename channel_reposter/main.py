"""
main.py — Channel Reposter на чистом USERBOT.

Admin-бот (aiogram) — только панель.
Публикация — Pyrogram-аккаунт (api_id / api_hash).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from aiogram.fsm.storage.memory import MemoryStorage

import admin_bot
import config
import lobby
import single_instance
import support_inbox
from bridge import BRIDGE
from database import Database
from poster import REASON_ABORTED as POST_REASON_ABORTED
from poster import REASON_ERROR as POST_REASON_ERROR
from poster import REASON_FATAL as POST_REASON_FATAL
from poster import REASON_FLOOD as POST_REASON_FLOOD
from poster import REASON_SOURCE_EMPTY as POST_REASON_SOURCE_EMPTY
from poster import REASON_TIMEOUT as POST_REASON_TIMEOUT
from poster import ChannelPoster, CycleResult
from scheduling import (
    NextRun,
    fair_window_limit,
    humanize_duration,
    plan_next_delay,
    slice_timeout,
)
from userbot_auth import UserbotAuth

# Как часто планировщик просыпается и как быстро реагирует на «Старт»
TICK = 5.0
# Пауза перед первым тиком: даём юзерботу подняться
START_DELAY = 3.0
# Через сколько секунд «занятый» цикл считается зависшим
STUCK_AFTER = max(180.0, config.WINDOW_CYCLE_TIMEOUT + 60.0)
# Как часто проверять живость сессии юзербота
HEALTH_EVERY = 300.0
# Не спамить админа сообщениями про flood
FLOOD_NOTICE_EVERY = 600.0
# Пауза между окнами в одном проходе (один юзербот, без параллельного flood)
WINDOW_GAP = 0.8
# Сколько ждать вход юзербота при старте (панель при этом уже работает)
BOOTSTRAP_TIMEOUT = 120.0
# Отставание event loop, после которого пишем в лог «панель подтормаживает»
LOOP_LAG_ALERT = 3.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


def _norm_channel(val: str) -> str:
    from links import normalize_channel

    return normalize_channel(val)


async def _worker_bootstrap(db: Database, workdir: Path) -> str:
    auth = BRIDGE.auth
    if auth is None:
        auth = UserbotAuth(db=db, workdir=workdir)
        BRIDGE.auth = auth
    BRIDGE.db = db

    ok = await auth.try_start_existing()
    if not ok or auth.client is None:
        BRIDGE.poster = None
        logger.warning("Юзербот не готов — нужен /login (api_id + api_hash)")
        return "need_login"

    BRIDGE.poster = ChannelPoster(client=auth.client, db=db)
    me = await auth.client.get_me()
    logger.info(
        "USERBOT ready: %s (@%s) id=%s",
        me.first_name,
        me.username or "—",
        me.id,
    )
    return "userbot"


async def _bootstrap_userbot_forever(db: Database, workdir: Path) -> None:
    """
    Поднимать юзербота в фоне, не задерживая панель.

    Панель обязана отвечать на команды всегда — даже если сессия юзербота
    битая, а сеть до Telegram лежит. Поэтому вход не блокирует запуск:
    пробуем, при неудаче ждём и пробуем снова.
    """
    # Auth создаём сразу: панели он нужен для «🔐 Вход» и /reconnect
    if BRIDGE.auth is None:
        BRIDGE.auth = UserbotAuth(db=db, workdir=workdir)
    BRIDGE.db = db

    delay = 60.0
    while True:
        if BRIDGE.poster is not None:
            return
        creds = BRIDGE.auth.load_credentials() if BRIDGE.auth else None
        if creds is None:
            logger.info("Нет api_id/api_hash — ждём «🔐 Вход» в панели")
            return
        try:
            mode = await asyncio.wait_for(
                _worker_bootstrap(db, workdir), timeout=BOOTSTRAP_TIMEOUT
            )
            if mode == "userbot":
                logger.info("Юзербот поднят, автопостинг доступен")
                return
        except asyncio.TimeoutError:
            logger.error(
                "Вход юзербота не уложился в %.0f сек — панель работает, повтор через %s",
                BOOTSTRAP_TIMEOUT,
                humanize_duration(delay),
            )
            db.set_last_error("юзербот не отвечает при запуске")
        except Exception:
            logger.exception("Не удалось поднять юзербота")
            db.set_last_error("не удалось поднять юзербота — см. логи")

        await asyncio.sleep(delay)
        delay = min(delay * 2, 600.0)


async def _loop_watchdog() -> None:
    """
    Следить за отзывчивостью панели.

    Если между тиками прошло заметно больше секунды — event loop чем-то
    заблокирован, и пользователь как раз видит «бот не отвечает».
    """
    while True:
        started = time.monotonic()
        await asyncio.sleep(1.0)
        lag = time.monotonic() - started - 1.0
        if lag > LOOP_LAG_ALERT:
            logger.error(
                "Панель подтормаживает: event loop был занят %.1f сек", lag
            )


def _warn_duplicate(db: Database) -> None:
    """Сообщить админу про второй запущенный экземпляр (если можем)."""
    try:
        _notify_admin(
            db,
            "⚠️ Похоже, запущено <b>два процесса</b> бота с одним токеном.\n"
            "Из-за этого команды теряются и панель кажется зависшей.\n"
            "Оставьте один: <code>systemctl restart channel-reposter</code> "
            "или закройте лишний <code>python main.py</code>.",
        )
    except Exception:
        logger.debug("duplicate warning failed", exc_info=True)


def _notify_admin(db: Database, text: str) -> None:
    """Сообщение админу из worker-потока (если он уже писал боту)."""
    raw = db.get("staging_chat_id") or ""
    if not raw.isdigit():
        return
    BRIDGE.notify(int(raw), text, parse_mode="HTML")


def _sync_poster(db: Database, poster: ChannelPoster) -> ChannelPoster:
    """Держать движок на актуальном клиенте юзербота (после повторного входа)."""
    auth = BRIDGE.auth
    client = getattr(auth, "client", None)
    if client is not None and client is not poster.client:
        poster = ChannelPoster(client=client, db=db)
        BRIDGE.poster = poster
        logger.info("Клиент юзербота обновлён — движок пересоздан")
    return poster


async def _run_window_cycle(
    poster: ChannelPoster,
    *,
    limit: int,
    deadline: float,
    timeout: float,
):
    """Цикл одного окна с жёстким потолком времени (остальные окна ждут)."""
    try:
        return await asyncio.wait_for(
            poster.run_cycle(limit=limit, deadline=deadline),
            timeout=max(8.0, float(timeout)),
        )
    except asyncio.TimeoutError:
        logger.warning("таймаут окна %.0fs — прерываю и отдаю очередь дальше", timeout)
        abort = getattr(poster, "request_abort", None)
        if callable(abort):
            abort()
        wait = getattr(poster, "wait_until_idle", None)
        if callable(wait):
            try:
                await wait(timeout=8.0)
            except Exception:
                logger.debug("wait_until_idle after timeout failed", exc_info=True)
        if getattr(poster, "is_busy", False):
            unlock = getattr(poster, "force_unlock", None)
            if callable(unlock):
                unlock()
        return CycleResult(reason=POST_REASON_TIMEOUT, error="таймаут окна")


async def _reconnect_userbot(db: Database) -> bool:
    """Поднять юзербота заново из сохранённой сессии после обрыва связи."""
    auth = BRIDGE.auth
    if auth is None:
        return False
    logger.warning("Проверяю связь с юзерботом после сбоя цикла")
    try:
        ok = await auth.ensure_started()
    except Exception:
        logger.exception("reconnect")
        return False
    if not ok or auth.client is None:
        db.set_last_error("нет связи с юзерботом — нужен «🔐 Вход»")
        return False
    BRIDGE.poster = ChannelPoster(client=auth.client, db=db)
    logger.info("Юзербот снова на связи")
    return True


def _schedule_next(db: Database, plan: NextRun) -> float:
    """Записать время следующего цикла (переживает рестарт бота)."""
    at = time.time() + plan.delay
    db.set_next_run(at, plan.reason)
    logger.info(
        "Следующий цикл через %s → %s",
        plan.describe(),
        datetime.fromtimestamp(at).strftime("%H:%M:%S"),
    )
    return at


async def _finish_scheduled_cycle(
    db: Database,
    poster: ChannelPoster,
    result,
    *,
    idle_streaks: dict[int, int],
    error_streaks: dict[int, int],
    fatal_notified: dict[int, str],
    now: float,
    last_health: float,
    last_flood_notice: float,
) -> tuple[ChannelPoster, float, float]:
    """Учесть итог цикла окна и запланировать следующий. Возвращает poster и таймеры."""
    jid = db.get_settings().job_id
    db.mark_cycle(result.published)
    settings = db.get_settings()
    idle_streak = idle_streaks.get(jid, 0)
    error_streak = error_streaks.get(jid, 0)
    window = settings.pair_label()

    if result.published:
        idle_streak = 0
        error_streak = 0
        db.clear_last_error()
        fatal_notified.pop(jid, None)
        if settings.notify_cycles:
            _notify_admin(
                db,
                f"✅ <b>{window}</b>\n"
                f"Опубликовано: <b>{result.published}</b>\n"
                f"Очередь: <b>{result.backlog}</b>",
            )
    elif result.reason in (POST_REASON_ERROR, POST_REASON_SOURCE_EMPTY):
        error_streak += 1
        db.set_last_error(result.error or result.reason)
        if error_streak in (1, 5) or error_streak % 20 == 0:
            _notify_admin(
                db,
                f"⚠️ <b>{window}</b> — цикл не удался ({error_streak}-й раз):\n"
                f"<code>{(result.error or result.reason)[:300]}</code>",
            )
    elif result.reason == POST_REASON_FLOOD:
        db.set_last_error(
            f"Telegram просит подождать {result.flood_seconds:.0f} сек"
        )
        if now - last_flood_notice > FLOOD_NOTICE_EVERY:
            last_flood_notice = now
            _notify_admin(
                db,
                f"⏳ <b>{window}</b> — Telegram ограничил аккаунт (flood).\n"
                f"Пауза: <b>{humanize_duration(result.flood_seconds)}</b>. "
                "Публикация продолжится сама.",
            )
    elif result.reason == POST_REASON_TIMEOUT:
        error_streak = 0
        if not result.published:
            db.set_last_error("таймаут окна — очередь продолжит следующий проход")
        idle_streak += 1
    elif result.reason == POST_REASON_FATAL:
        db.set_running(False)
        db.set_last_error(result.fatal_text or "критическая ошибка")
        if fatal_notified.get(jid) != result.fatal_text:
            fatal_notified[jid] = result.fatal_text
            _notify_admin(
                db,
                f"🛑 Окно <b>{window}</b> остановлено.\n"
                f"Причина: {result.fatal_text or 'критическая ошибка'}\n\n"
                "Исправьте и нажмите ▶️ Старт. Остальные окна продолжают работу.",
            )
        idle_streaks[jid] = idle_streak
        error_streaks[jid] = error_streak
        logger.info("Окно %s %s: %s", jid, window, result.reason)
        return poster, last_health, last_flood_notice
    else:
        idle_streak += 1
        error_streak = 0
        if result.reason != POST_REASON_ABORTED:
            db.clear_last_error()

    idle_streaks[jid] = idle_streak
    error_streaks[jid] = error_streak

    if (result.needs_reconnect or error_streak >= 2) and (
        now - last_health > HEALTH_EVERY or result.needs_reconnect
    ):
        last_health = now
        if await _reconnect_userbot(db):
            poster = BRIDGE.poster or poster

    plan = plan_next_delay(
        published=result.published,
        interval_seconds=settings.interval_seconds,
        catchup=settings.catchup_enabled,
        catchup_seconds=settings.catchup_seconds,
        backlog=result.backlog,
        idle_streak=idle_streak,
        error_streak=error_streak,
        flood_seconds=result.flood_seconds,
    )
    _schedule_next(db, plan)
    logger.info("Окно %s %s: %s", jid, window, result.reason)
    return poster, last_health, last_flood_notice


async def _worker_scheduler() -> None:
    """
    Единственный источник автопостинга.

    Работает всегда: ждёт юзербота (вход можно сделать позже через панель),
    соблюдает пользовательский интервал, сам чинит залипшие циклы и обрывы
    связи, а время следующего запуска хранит в БД — рестарт не сбивает график.
    """
    logger.info("Планировщик запущен (tick=%ss)", TICK)
    await asyncio.sleep(START_DELAY)

    idle_streaks: dict[int, int] = {}
    error_streaks: dict[int, int] = {}
    last_health = 0.0
    last_flood_notice = 0.0
    warned_no_userbot = False
    fatal_notified: dict[int, str] = {}

    while True:
        try:
            db = BRIDGE.db
            if db is None:
                await asyncio.sleep(TICK)
                continue

            poster = BRIDGE.poster
            now = time.time()
            db.mark_scheduler_tick()
            jobs = db.list_jobs()
            running = [j for j in jobs if j.is_running]

            # Залипший цикл (обрыв сети / вечный await) — принудительно снять
            if poster is not None and poster.busy_seconds > STUCK_AFTER:
                logger.error(
                    "Цикл висит %.0f сек — снимаю блокировку", poster.busy_seconds
                )
                poster.force_unlock()
                for j in running or jobs:
                    with db.job_scope(j.job_id):
                        db.set_last_error("цикл зависал, блокировка снята")
                        db.run_asap()

            if not running:
                idle_streaks.clear()
                error_streaks.clear()
                await asyncio.sleep(TICK)
                continue

            if poster is None:
                if not warned_no_userbot:
                    logger.warning(
                        "Автопост включён, но юзербот не авторизован — нужен «🔐 Вход»"
                    )
                    warned_no_userbot = True
                    with db.job_scope(running[0].job_id):
                        db.set_last_error("юзербот не авторизован — нужен «🔐 Вход»")
                    _notify_admin(
                        db,
                        "⚠️ Автопостинг включён, но юзербот не авторизован.\n"
                        "Нажмите «🔐 Вход» (api_id / api_hash) — публикация "
                        "начнётся сама, перезапуск не нужен.",
                    )
                await asyncio.sleep(TICK)
                continue
            warned_no_userbot = False

            if poster.is_busy:
                await asyncio.sleep(TICK)
                continue

            due = db.due_jobs(now)
            if not due:
                await asyncio.sleep(TICK)
                continue

            # Все просроченные окна — в одном проходе, по очереди (один юзербот).
            # Бюджет времени и публикаций общий, чтобы одно окно не забивало остальные.
            pass_deadline = time.monotonic() + config.PASS_TIMEOUT
            remaining_budget = int(config.PASS_PUBLISH_LIMIT)
            for index, job in enumerate(due):
                poster = BRIDGE.poster
                if poster is None or poster.is_busy:
                    break
                live = db.get_job(job.job_id)
                if live is None or not live.is_running:
                    continue
                pass_left = pass_deadline - time.monotonic()
                slot = slice_timeout(config.WINDOW_CYCLE_TIMEOUT, pass_left)
                windows_left = len(due) - index
                limit = fair_window_limit(
                    live.posts_per_cycle, windows_left, remaining_budget
                )
                if slot <= 0 or limit <= 0:
                    logger.info(
                        "проход окон: лимит (осталось %.0fс / %s публикаций)",
                        pass_left,
                        remaining_budget,
                    )
                    break
                poster = _sync_poster(db, poster)
                if index > 0:
                    await asyncio.sleep(WINDOW_GAP)
                    if poster is None or poster.is_busy:
                        break
                    live = db.get_job(job.job_id)
                    if live is None or not live.is_running:
                        continue
                deadline = time.monotonic() + slot
                with db.job_scope(job.job_id):
                    result = await _run_window_cycle(
                        poster,
                        limit=limit,
                        deadline=deadline,
                        timeout=slot + 8.0,
                    )
                    remaining_budget = max(
                        0, remaining_budget - int(getattr(result, "published", 0) or 0)
                    )
                    poster, last_health, last_flood_notice = await _finish_scheduled_cycle(
                        db,
                        poster,
                        result,
                        idle_streaks=idle_streaks,
                        error_streaks=error_streaks,
                        fatal_notified=fatal_notified,
                        now=time.time(),
                        last_health=last_health,
                        last_flood_notice=last_flood_notice,
                    )
        except Exception:
            logger.exception("Ошибка планировщика")
            try:
                if BRIDGE.db is not None:
                    BRIDGE.db.set_next_run(time.time() + 60, "error")
            except Exception:
                logger.exception("не удалось записать next_run")
        await asyncio.sleep(TICK)


async def _heartbeat(bot: Bot, db: Database) -> None:
    while True:
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=15)
            wh = await asyncio.wait_for(bot.get_webhook_info(), timeout=15)
            jobs = db.list_jobs()
            running_n = sum(1 for j in jobs if j.is_running)
            s = db.get_settings()
            left = max(0.0, db.get_next_run() - time.time())
            logger.info(
                "heartbeat @%s pending=%s | окна=%s/%s автопост=%s юзербот=%s цикл=%s "
                "интервал=%s следующий=%s очередь=%s всего=%s текущее=%s",
                me.username,
                wh.pending_update_count,
                running_n,
                len(jobs),
                "вкл" if s.is_running else "пауза",
                "готов" if BRIDGE.poster is not None else "нет входа",
                "идёт" if (BRIDGE.poster and BRIDGE.poster.is_busy) else "idle",
                humanize_duration(s.interval_seconds),
                humanize_duration(left) if s.is_running else "—",
                db.backlog(),
                db.history_count(),
                s.pair_label(),
            )
            if wh.url:
                await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            logger.exception("heartbeat")
        await asyncio.sleep(60)


async def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("Задайте BOT_TOKEN в .env (только для админ-панели)")

    db = Database(config.DATABASE_PATH)
    db.ensure_defaults(
        caption="<b>Описание</b>",
        interval_seconds=config.DEFAULT_INTERVAL_SECONDS,
        posts_per_cycle=config.DEFAULT_POSTS_PER_CYCLE,
        source_channel=config.SOURCE_CHANNEL or "",
        target_channel=config.TARGET_CHANNEL or "",
        catchup_seconds=config.DEFAULT_CATCHUP_SECONDS,
    )
    s = db.get_settings()
    if s.source_channel:
        db.set_source_channel(_norm_channel(s.source_channel))
    if s.target_channel:
        db.set_target_channel(_norm_channel(s.target_channel))
    # После рестарта не ждём остаток старого интервала дольше, чем сам интервал
    due = db.get_next_run()
    if due > time.time() + s.interval_seconds:
        db.run_asap()

    bot = Bot(
        token=config.BOT_TOKEN,
        session=AiohttpSession(timeout=30.0),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramConflictError:
        logger.error("Другой процесс уже работает с этим токеном")
        _warn_duplicate(db)
        await bot.session.close()
        raise
    except Exception:
        logger.exception("delete_webhook")

    me = await bot.get_me()
    logger.info("Admin panel @%s", me.username)

    # Проверка «а не работает ли уже такой же бот» до старта поллинга:
    # иначе два процесса будут молча делить апдейты между собой
    try:
        await bot.get_updates(offset=-1, limit=1, timeout=0)
    except TelegramConflictError:
        logger.error(
            "С токеном @%s уже работает другой процесс — выхожу, "
            "чтобы не делить апдейты и не «терять» команды",
            me.username,
        )
        _warn_duplicate(db)
        await bot.session.close()
        raise
    except Exception:
        logger.debug("проверка конфликта не удалась", exc_info=True)

    workdir = Path(__file__).resolve().parent
    BRIDGE.start()
    BRIDGE.admin_loop = asyncio.get_running_loop()

    async def _notify(chat_id: int, text: str, **kwargs):
        try:
            await asyncio.wait_for(bot.send_message(chat_id, text, **kwargs), timeout=20)
        except Exception:
            logger.exception("notify")

    BRIDGE.notify_fn = _notify
    BRIDGE.bot_username = me.username or ""

    admin_bot.set_dependencies(
        db=db,
        bot_username=me.username or "",
        bridge=BRIDGE,
        bot=bot,
    )
    lobby.set_dependencies(
        db=db,
        bot=bot,
        bridge=BRIDGE,
        bot_username=me.username or "",
    )

    dp = Dispatcher(storage=MemoryStorage())
    # Лобби раньше панели: иначе catch-all /start и on_unknown съедят диалог
    dp.include_router(lobby.router)
    admin_bot.setup_dispatcher(dp)
    await lobby.setup_bot_menu(bot)

    # Планировщик работает всегда: вход юзербота можно сделать позже
    # через панель, и автопостинг подхватится без перезапуска бота.
    BRIDGE.submit(_worker_scheduler())
    BRIDGE.submit(support_inbox.run_forever(BRIDGE, db))
    # Вход юзербота — в фоне: панель начинает отвечать сразу, даже если
    # сессия битая или Telegram недоступен (иначе бот «висел» на старте)
    BRIDGE.submit(_bootstrap_userbot_forever(db, workdir))
    logger.info("Online https://t.me/%s", me.username)

    hb = asyncio.create_task(_heartbeat(bot, db), name="heartbeat")
    watchdog = asyncio.create_task(_loop_watchdog(), name="watchdog")

    # Свежее меню админу после рестарта — старые кнопки часто «мертвые»
    async def _announce_restart() -> None:
        await asyncio.sleep(2)
        raw = db.get("staging_chat_id") or ""
        if not raw.isdigit():
            return
        try:
            await bot.send_message(
                int(raw),
                "♻️ Бот перезапущен.\nНажмите /start — откроется новое меню "
                "(старые кнопки могут не работать).",
                reply_markup=admin_bot.main_kb(db),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("restart announce")

    announce = asyncio.create_task(_announce_restart(), name="announce")
    try:
        await dp.start_polling(
            bot,
            drop_pending_updates=True,
            polling_timeout=20,
            handle_as_tasks=True,
            allowed_updates=["message", "callback_query"],
        )
    except TelegramConflictError:
        logger.error(
            "Telegram отдал 409 Conflict: с этим токеном работает другой процесс. "
            "Остановите лишний экземпляр — панель не может отвечать, пока их два."
        )
        _warn_duplicate(db)
        raise
    finally:
        announce.cancel()
        hb.cancel()
        watchdog.cancel()
        for task in (hb, announce, watchdog):
            try:
                await task
            except BaseException:
                pass
        try:
            BRIDGE.stop()
        except Exception:
            logger.exception("bridge stop")
        try:
            await bot.session.close()
        except Exception:
            logger.exception("bot session close")


def _lock_path() -> Path:
    return Path(config.DATABASE_PATH).parent / "reposter.lock"


if __name__ == "__main__":
    lock = single_instance.acquire(_lock_path())
    if lock.busy:
        logger.error(
            "Бот уже запущен (PID %s). Второй процесс делил бы апдейты с первым, "
            "и панель казалась бы зависшей. Остановите старый процесс: "
            "kill %s — или используйте systemctl restart.",
            lock.owner_pid or "?",
            lock.owner_pid or "<pid>",
        )
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stop")
    finally:
        single_instance.release(lock)
