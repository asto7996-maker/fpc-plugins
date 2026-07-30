"""
Application entrypoint.

Starts:
* DB schema init
* Redis task worker + APScheduler
* Gemini Draft Engine listeners (Telethon)
* aiogram admin bot polling (+ command menu, invite access)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from aiogram import Bot
from redis.asyncio import from_url as redis_from_url

from tg_pool.admin.bot import build_dispatcher, setup_bot_commands
from tg_pool.config import get_settings
from tg_pool.db.session import create_all, dispose_engine, init_engine, session_scope
from tg_pool.taskqueue.broker import RedisTaskBroker
from tg_pool.taskqueue.scheduler import PoolScheduler
from tg_pool.services.access_service import AccessService
from tg_pool.services.alerts import AlertService
from tg_pool.services.listener_manager import ListenerManager
from tg_pool.services.task_router import TaskRouter


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def _send_bootstrap(bot: Bot, creator_id: int) -> None:
    from tg_pool.admin.keyboards import main_menu_kb, reply_menu_kb
    from tg_pool.admin.texts import main_menu_text

    under_watchdog = bool(os.environ.get("TG_POOL_HEARTBEAT_FILE"))
    title = (
        "✅ <b>Панель онлайн</b> <i>(watchdog)</i>\n\n"
        if under_watchdog
        else "✅ <b>Панель запущена</b>\n\n"
    )
    await bot.send_message(
        creator_id,
        title + main_menu_text(),
        parse_mode="HTML",
        reply_markup=reply_menu_kb(is_creator=True),
    )
    await bot.send_message(
        creator_id,
        "Выберите раздел:",
        parse_mode="HTML",
        reply_markup=main_menu_kb(is_creator=True),
    )


async def _run_polling_forever(dp, bot: Bot, stop_event: asyncio.Event) -> None:
    """Restart aiogram polling if it dies (conflict / network blip)."""
    logger = logging.getLogger("tg_pool.polling")
    backoff = 1.0
    while not stop_event.is_set():
        try:
            # handle_signals=False — main() owns SIGINT/SIGTERM
            await dp.start_polling(bot, handle_signals=False)
            if stop_event.is_set():
                break
            logger.warning("Polling ended unexpectedly — restarting in %.1fs", backoff)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Polling crashed: %s — restart in %.1fs", exc, backoff)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            break
        except asyncio.TimeoutError:
            pass
        backoff = min(30.0, backoff * 1.5)


def _touch_heartbeat() -> None:
    path = Path(os.environ.get("TG_POOL_HEARTBEAT_FILE", "/tmp/tg_pool_heartbeat"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(asyncio.get_running_loop().time()), encoding="utf-8")
    except Exception:  # noqa: BLE001
        logging.getLogger("tg_pool.heartbeat").debug(
            "heartbeat touch failed", exc_info=True
        )


async def _heartbeat(stop_event: asyncio.Event) -> None:
    """
    Touch an on-disk heartbeat every few seconds.

    The external watchdog kills/restarts the process if this file goes stale —
    that recovers from event-loop freezes that normal exception handlers miss.
    """
    logger = logging.getLogger("tg_pool.heartbeat")
    interval = float(os.environ.get("TG_POOL_HEARTBEAT_INTERVAL_SEC", "15"))
    _touch_heartbeat()
    while not stop_event.is_set():
        _touch_heartbeat()
        logger.info("alive")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            continue


async def amain() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    logger = logging.getLogger("tg_pool")

    init_engine(settings)
    await create_all()
    logger.info("Database schema ready")

    # Separate Redis clients so a blocking queue read cannot starve other I/O
    redis = redis_from_url(settings.redis_url, decode_responses=False, max_connections=20)
    redis_broker = redis_from_url(
        settings.redis_url, decode_responses=False, max_connections=10
    )
    broker = RedisTaskBroker(redis_broker, settings=settings)
    alerts = AlertService(settings=settings)
    TaskRouter(broker, alerts, settings=settings)

    listeners = ListenerManager(settings, redis, alert_service=alerts)

    scheduler = PoolScheduler(broker)
    scheduler.start()
    await broker.start_worker()

    bot: Bot | None = None
    polling_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None
    stop_event = asyncio.Event()

    if settings.admin_bot_token:
        bot = Bot(token=settings.admin_bot_token)
        alerts.bind_bot(bot)
        listeners.bind_bot(bot)
        async with session_scope() as session:
            await AccessService(session, creator_id=settings.creator_id).ensure_user(
                settings.creator_id,
                username=None,
                full_name="Creator",
            )
        # Ensure no webhook steals updates from polling
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_webhook failed: %s", exc)
        await setup_bot_commands(bot)
        dp = build_dispatcher(
            settings,
            broker,
            bot=bot,
            draft_engine=listeners.engine,
            listeners=listeners,
        )
        polling_task = asyncio.create_task(
            _run_polling_forever(dp, bot, stop_event),
            name="admin-polling",
        )
        heartbeat_task = asyncio.create_task(_heartbeat(stop_event), name="heartbeat")
        logger.info("Admin bot polling started (creator_id=%s)", settings.creator_id)
    else:
        logger.warning("ADMIN_BOT_TOKEN empty — admin UI disabled, worker-only mode")

    try:
        await listeners.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("ListenerManager start failed (admin UI still up): %s", exc)

    if bot is not None:
        try:
            await _send_bootstrap(bot, settings.creator_id)
            logger.info("Bootstrap menu sent to creator %s", settings.creator_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not push bootstrap menu: %s", exc)

    loop = asyncio.get_running_loop()

    def _shutdown(sig: str) -> None:
        logger.info("Signal %s — shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown, sig.name)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _shutdown(sig.name))

    try:
        if polling_task is not None:
            stop_wait = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait(
                {polling_task, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if polling_task in done and stop_wait not in done:
                exc = polling_task.exception() if not polling_task.cancelled() else None
                if exc:
                    raise exc
        else:
            await stop_event.wait()
    finally:
        logger.info("Graceful shutdown…")
        stop_event.set()
        for task in (polling_task, heartbeat_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                pass
        await listeners.stop()
        scheduler.shutdown()
        await broker.stop_worker()
        await redis.aclose()
        await redis_broker.aclose()
        await dispose_engine()
        logger.info("Shutdown complete")


def main() -> None:
    # Prefer stdlib asyncio for admin-bot reliability (uvloop optional later)
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
