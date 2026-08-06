"""
main.py — точка входа VK VPN-бота (vkbottle 4.x, async).

Запуск:
    cd vk_vpn_bot
    pip install -r requirements.txt
    cp .env.example .env   # заполните VK_TOKEN (и при желании GROUP_ID)
    python main.py

В настройках сообщества VK:
  • включите Long Poll API / Bots Long Poll API
  • разрешите сообщения сообщества
  • выдайте ключ доступа с правами messages + manage
"""

from __future__ import annotations

import logging
import sys

from vkbottle.bot import Bot

from config import get_settings
from database import close_db, init_db
from handlers import labeler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vpn_bot")


async def on_startup() -> None:
    """Инициализация БД при старте бота."""
    await init_db()
    settings = get_settings()
    logger.info(
        "БД готова. Бот «%s», group_id=%s, trial=%s дн., тип ключа=%s",
        settings.bot_name,
        settings.group_id,
        settings.trial_days,
        settings.vpn_key_type,
    )


async def on_shutdown() -> None:
    """Закрытие соединения с БД при остановке."""
    await close_db()
    logger.info("Бот остановлен, соединение с БД закрыто.")


def main() -> None:
    """Создаёт бота, подключает обработчики и запускает Long Poll."""
    try:
        settings = get_settings()
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    bot = Bot(token=settings.vk_token)

    # Регистрируем все хендлеры из handlers/
    bot.labeler.load(labeler)

    # Хуки жизненного цикла (актуальный API vkbottle 4.10+)
    bot.on_startup.append(on_startup())
    bot.on_shutdown.append(on_shutdown())

    logger.info("Запуск Long Poll… (Ctrl+C для остановки)")
    bot.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
