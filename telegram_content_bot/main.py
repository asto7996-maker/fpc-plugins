"""
Главный модуль Content Bot.
Инициализирует все компоненты, запускает Telethon-клиент,
мониторинг каналов, публикатор и обработчик команд.
"""

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import Config, ConfigError
from database import Database
from filters import ContentFilters
from media_processor import MediaProcessor
from monitor import ChannelMonitor
from publisher import Publisher
from bot_commands import BotCommands

logger = logging.getLogger("content_bot")


def setup_logging(config: Config) -> None:
    """Настраивает систему логирования."""
    log_level = config.get("logging.level", "INFO").upper()
    log_file = config.get("logging.file", "bot.log")
    max_bytes = config.get("logging.max_bytes", 10_000_000)
    backup_count = config.get("logging.backup_count", 5)
    console_output = config.get("logging.console_output", True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


class ContentBot:
    """
    Главный класс бота.
    Координирует работу всех подсистем: мониторинга, публикации, фильтров, БД.
    """

    def __init__(self, config_path: str = "config.yaml"):
        self._config_path = config_path
        self._config: Optional[Config] = None
        self._client: Optional[TelegramClient] = None
        self._db: Optional[Database] = None
        self._filters: Optional[ContentFilters] = None
        self._media_processor: Optional[MediaProcessor] = None
        self._monitor: Optional[ChannelMonitor] = None
        self._publisher: Optional[Publisher] = None
        self._commands: Optional[BotCommands] = None
        self._start_time: float = 0
        self._shutdown_event = asyncio.Event()
        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def config(self) -> Config:
        return self._config

    @property
    def client(self) -> TelegramClient:
        return self._client

    @property
    def db(self) -> Database:
        return self._db

    @property
    def monitor(self) -> Optional[ChannelMonitor]:
        return self._monitor

    @property
    def publisher(self) -> Optional[Publisher]:
        return self._publisher

    @property
    def media_processor(self) -> MediaProcessor:
        return self._media_processor

    @property
    def start_time(self) -> float:
        return self._start_time

    async def run(self) -> None:
        """Основной метод запуска бота."""
        try:
            self._init_config()
            setup_logging(self._config)
            logger.info("=" * 60)
            logger.info("Content Bot запускается...")
            logger.info("=" * 60)

            self._init_components()
            await self._init_client()

            self._commands = BotCommands(self)
            self._commands.register_handlers()

            self._start_time = time.time()

            if self._config.monitoring.get("enabled", True):
                await self._start_monitoring()
            else:
                logger.info("Мониторинг отключён в конфигурации")

            await self._start_publishing()

            self._cleanup_task = asyncio.create_task(
                self._periodic_cleanup(), name="periodic_cleanup"
            )

            logger.info("=" * 60)
            logger.info("Content Bot успешно запущен!")
            logger.info("=" * 60)

            await self._run_until_shutdown()

        except ConfigError as e:
            logger.error("Ошибка конфигурации: %s", e)
            print(f"\n[ОШИБКА КОНФИГУРАЦИИ] {e}")
            sys.exit(1)
        except KeyboardInterrupt:
            logger.info("Получен сигнал прерывания")
        except Exception as e:
            logger.critical("Критическая ошибка: %s", e, exc_info=True)
            sys.exit(1)
        finally:
            await self._shutdown()

    def _init_config(self) -> None:
        """Инициализирует конфигурацию."""
        self._config = Config(self._config_path)
        self._config.load()

    def _init_components(self) -> None:
        """Инициализирует вспомогательные компоненты."""
        self._db = Database(self._config.database_path)
        self._filters = ContentFilters(self._config.filters_config)
        self._media_processor = MediaProcessor(self._config.media_config)
        logger.info("Компоненты инициализированы")

    async def _init_client(self) -> None:
        """Инициализирует и подключает Telethon-клиент."""
        proxy = self._config.proxy_settings

        proxy_kwargs = {}
        if proxy:
            proxy_kwargs["proxy"] = (
                proxy["proxy_type"],
                proxy["addr"],
                proxy["port"],
                True,
                proxy.get("username"),
                proxy.get("password"),
            )
            logger.info(
                "Прокси: %s://%s:%d",
                proxy["proxy_type"],
                proxy["addr"],
                proxy["port"],
            )

        session_path = self._config.session_name
        self._client = TelegramClient(
            session_path,
            self._config.api_id,
            self._config.api_hash,
            **proxy_kwargs,
        )

        await self._client.start(
            phone=self._config.phone or None,
            bot_token=self._config.bot_token,
        )

        me = await self._client.get_me()
        if me:
            name = getattr(me, "first_name", "") or getattr(me, "title", "") or "Bot"
            user_id = me.id
            logger.info("Авторизован как: %s (ID: %d)", name, user_id)
        else:
            logger.warning("Не удалось получить информацию об аккаунте")

    async def _start_monitoring(self) -> None:
        """Запускает мониторинг каналов-доноров."""
        donors = self._config.donors
        if not donors:
            logger.warning("Список доноров пуст, мониторинг не запущен")
            return

        self._monitor = ChannelMonitor(
            client=self._client,
            db=self._db,
            filters=self._filters,
            media_processor=self._media_processor,
            config=self._config.monitoring,
            posting_config=self._config.posting,
        )

        await self._monitor.start(donors)

    async def _start_publishing(self) -> None:
        """Запускает публикатор."""
        target = self._config.target_channel
        if not target:
            logger.warning("Целевой канал не указан, публикатор не запущен")
            return

        self._publisher = Publisher(
            client=self._client,
            db=self._db,
            media_processor=self._media_processor,
            config=self._config.posting,
            target_channel=target,
        )

        try:
            await self._publisher.start()
        except Exception as e:
            logger.error("Не удалось запустить публикатор: %s", e)

    async def _periodic_cleanup(self) -> None:
        """Периодическая очистка старых данных (раз в 6 часов)."""
        cleanup_interval = 6 * 3600
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=cleanup_interval
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                days = self._config.get("database.cleanup_days", 30)
                count = await self._db.cleanup_old_records(days)
                if count > 0:
                    logger.info("Автоочистка: удалено %d записей", count)
            except Exception as e:
                logger.error("Ошибка при автоочистке: %s", e)

    def _apply_config_updates(self) -> None:
        """Применяет обновлённую конфигурацию к компонентам."""
        if self._filters:
            self._filters.update_config(self._config.filters_config)
        if self._media_processor:
            self._media_processor.update_config(self._config.media_config)
        if self._publisher:
            self._publisher.update_config(self._config.posting)

    async def _run_until_shutdown(self) -> None:
        """Работает до получения сигнала завершения."""
        loop = asyncio.get_event_loop()

        def _signal_handler():
            logger.info("Получен сигнал завершения")
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass

        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass

    async def _shutdown(self) -> None:
        """Корректное завершение работы бота."""
        logger.info("Завершение работы...")

        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._monitor:
            try:
                await self._monitor.stop()
            except Exception as e:
                logger.error("Ошибка при остановке мониторинга: %s", e)

        if self._publisher:
            try:
                await self._publisher.stop()
            except Exception as e:
                logger.error("Ошибка при остановке публикатора: %s", e)

        if self._client:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.error("Ошибка при отключении клиента: %s", e)

        uptime = time.time() - self._start_time if self._start_time else 0
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        logger.info("Бот завершил работу. Аптайм: %dч %dм", hours, minutes)


def main():
    """Точка входа."""
    config_path = "config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    if not Path(config_path).exists():
        config = Config(config_path)
        try:
            config.load()
        except ConfigError as e:
            print(f"[INFO] {e}")
            print(f"\nОтредактируйте файл {config_path} и перезапустите бота.")
            sys.exit(0)

    bot = ContentBot(config_path)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
