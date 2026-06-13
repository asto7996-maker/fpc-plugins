#!/usr/bin/env python3
"""
Starvell Cardinal — точка входа.
Telegram-бот для автоматизации продаж на маркетплейсе Starvell.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from config import LOGS_DIR, VERSION, create_default_settings_file, load_settings
from core.app import Application


def setup_logging(debug: bool = False) -> None:
    """Настраивает логирование в консоль и файл."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOGS_DIR / "cardinal.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


async def main() -> None:
    create_default_settings_file()
    settings = load_settings()
    setup_logging(settings.debug)

    logger = logging.getLogger("starvell.main")
    logger.info("Starvell Cardinal v%s (ULTIMATE) запускается…", VERSION)

    if not settings.bot_token:
        logger.error("BOT_TOKEN не задан! config/settings.json или env BOT_TOKEN")
        sys.exit(1)

    app = Application()
    await app.setup()
    await app.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
