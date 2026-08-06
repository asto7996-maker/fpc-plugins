"""
main.py — точка входа VK VPN-бота (vkbottle 4.x, async).

Запуск:
    cd vk_vpn_bot
    pip install -r requirements.txt
    cp .env.example .env
    ./run.sh
    # или: python main.py
"""

from __future__ import annotations

import logging
import sys

from vkbottle.bot import Bot

from config import get_settings
from database import close_db, init_db
from handlers import labeler
from services.bedolaga import close_bedolaga_client, get_bedolaga_client
from services.vk_setup import ensure_long_poll

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vpn_bot")


async def on_startup(bot: Bot) -> None:
    """Инициализация БД, Long Poll и проверка Bedolaga API."""
    await init_db()
    settings = get_settings()

    await ensure_long_poll(bot.api, settings.group_id)

    client = get_bedolaga_client()
    if client:
        try:
            health = await client.health()
            logger.info("Bedolaga API OK: %s", health)
        except Exception as exc:  # noqa: BLE001
            logger.error("Bedolaga API недоступен: %s", exc)
    else:
        logger.warning(
            "BEDOLAGA_API_KEY не задан — ключи будут локальными (демо). "
            "Добавьте ключ для выдачи реальных подписок Paskod."
        )

    logger.info(
        "Бот «%s» готов | group_id=%s | trial=%s дн. | cabinet=%s",
        settings.bot_name,
        settings.group_id,
        settings.trial_days,
        settings.cabinet_url,
    )


async def on_shutdown() -> None:
    await close_bedolaga_client()
    await close_db()
    logger.info("Бот остановлен.")


def main() -> None:
    try:
        settings = get_settings()
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    bot = Bot(token=settings.vk_token)
    bot.labeler.load(labeler)

    # Передаём bot в startup через замыкание
    async def _startup() -> None:
        await on_startup(bot)

    bot.on_startup.append(_startup())
    bot.on_shutdown.append(on_shutdown())

    logger.info("Запуск Long Poll… (Ctrl+C для остановки)")
    bot.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
