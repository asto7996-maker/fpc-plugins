"""Entry point: start userbot pool + admin bot together."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot

from brand_monitor.admin.bot import build_admin_dispatcher, make_admin_notifier
from brand_monitor.config import get_settings
from brand_monitor.core.userbot_manager import UserbotManager
from brand_monitor.database.repository import Database


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


def _try_install_uvloop() -> None:
    """Use uvloop on Linux for a faster event loop when available."""
    if sys.platform == "win32":
        return
    try:
        import uvloop

        uvloop.install()
        logging.getLogger("brand_monitor").info("uvloop installed as event loop policy")
    except ImportError:
        logging.getLogger("brand_monitor").info("uvloop not available — using asyncio default")


async def amain() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    logger = logging.getLogger("brand_monitor")

    db = Database(settings.database_path)
    await db.connect()
    await db.seed_defaults()

    bot: Bot | None = None
    manager = UserbotManager(db=db, settings=settings)
    stop_event = asyncio.Event()

    if settings.admin_bot_token:
        bot = Bot(token=settings.admin_bot_token)
        manager.admin_notifier = await make_admin_notifier(bot, settings)
    else:
        logger.warning("ADMIN_BOT_TOKEN is empty — admin panel disabled")

    loop = asyncio.get_running_loop()

    def _request_shutdown(signame: str) -> None:
        logger.info("Received %s — graceful shutdown…", signame)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            # Windows / limited environments
            signal.signal(sig, lambda *_: _request_shutdown(sig.name))

    await manager.start()
    polling_task: asyncio.Task | None = None

    try:
        if bot is not None:
            dp = build_admin_dispatcher(db, manager, settings)
            logger.info("Admin bot polling started")
            polling_task = asyncio.create_task(
                dp.start_polling(bot),
                name="admin-polling",
            )
            stop_wait = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait(
                {polling_task, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_wait not in done and polling_task in done:
                # polling crashed — surface exception
                await polling_task
        else:
            logger.info("Running userbot pool only (signal to stop)")
            await stop_event.wait()
    finally:
        logger.info("Shutting down — disconnecting Telethon sessions…")
        if polling_task is not None and not polling_task.done():
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("Error stopping admin polling")
        await manager.stop()
        if bot is not None:
            await bot.session.close()
        await db.close()
        logger.info("Shutdown complete")


def main() -> None:
    _try_install_uvloop()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
