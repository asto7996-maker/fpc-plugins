"""
Централизованное логирование бота Dwar.

- Консоль + RotatingFileHandler → bot.log
- Уровни INFO / WARNING / ERROR / CRITICAL
- Опциональная асинхронная отправка CRITICAL/важных событий в Telegram
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

import aiohttp

from dwar_bot.config import BotConfig, config

# Имена стандартных таймеров/событий, которые шлём в Telegram при CRITICAL
TELEGRAM_EVENT_KEYWORDS = (
    "captcha",
    "капч",
    "death",
    "смерть",
    "погиб",
    "session",
    "auth",
    "critical",
)


class TelegramLogHandler(logging.Handler):
    """
    Асинхронная отправка логов в Telegram через очередь.

    Не блокирует поток логирования: сообщения кладутся в asyncio.Queue,
    а фоновая задача отправляет их через aiohttp.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        level: int = logging.ERROR,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__(level=level)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._loop = loop
        self._queue: Optional[asyncio.Queue[str]] = None
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._closed = False
        self._api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def start(self) -> None:
        if self._queue is not None:
            return
        self._queue = asyncio.Queue(maxsize=200)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        self._worker_task = asyncio.create_task(
            self._worker(), name="telegram-log-worker"
        )

    async def stop(self) -> None:
        self._closed = True
        if self._queue is not None:
            try:
                self._queue.put_nowait("")  # sentinel
            except asyncio.QueueFull:
                pass
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker_task.cancel()
            self._worker_task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._queue = None

    def emit(self, record: logging.LogRecord) -> None:
        if self._closed or not self._bot_token or not self._chat_id:
            return
        try:
            message = self.format(record)
            if record.levelno < logging.CRITICAL:
                # ERROR — только если есть ключевые слова события
                low = message.lower()
                if not any(k in low for k in TELEGRAM_EVENT_KEYWORDS):
                    if record.levelno < logging.ERROR:
                        return
            self._enqueue(message)
        except Exception:
            self.handleError(record)

    def _enqueue(self, message: str) -> None:
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
                self._loop = loop
            except RuntimeError:
                return

        if self._queue is None:
            # Ленивый старт воркера из sync-контекста
            if loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.start(), loop=loop)
                )
            return

        text = message[:3500]
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(self._queue.put_nowait, text)
            else:
                self._queue.put_nowait(text)
        except asyncio.QueueFull:
            pass
        except Exception:
            pass

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            message = await self._queue.get()
            if message == "" and self._closed:
                break
            if not message:
                continue
            await self._send(message)
            await asyncio.sleep(0.35)  # анти-флуд Telegram

    async def _send(self, text: str) -> None:
        if self._session is None or self._session.closed:
            return
        payload = {
            "chat_id": self._chat_id,
            "text": f"🐉 DwarBot\n{text}",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(self._api_url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    # Не логируем через root — избегаем рекурсии
                    sys.stderr.write(
                        f"Telegram notify failed HTTP {resp.status}: {body[:200]}\n"
                    )
        except Exception as exc:
            sys.stderr.write(f"Telegram notify error: {exc}\n")

    async def notify(self, text: str, *, critical: bool = False) -> None:
        """Прямая отправка уведомления (капча, смерть и т.п.)."""
        if not self._bot_token or not self._chat_id:
            return
        if self._queue is None:
            await self.start()
        prefix = "🚨 CRITICAL\n" if critical else ""
        if self._queue is not None:
            try:
                self._queue.put_nowait(f"{prefix}{text}"[:3500])
            except asyncio.QueueFull:
                await self._send(f"{prefix}{text}"[:3500])


_telegram_handler: Optional[TelegramLogHandler] = None
_configured = False


def setup_logging(bot_config: Optional[BotConfig] = None) -> logging.Logger:
    """
    Настраивает root/dwar_bot логгер: консоль + ротация bot.log + Telegram.
    """
    global _configured, _telegram_handler

    cfg = bot_config or config
    log_cfg = cfg.logging
    log_cfg.log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    level_name = (log_cfg.level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Сброс старых хендлеров при повторном setup
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    if log_cfg.console:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(formatter)
        root.addHandler(console)

    file_handler = RotatingFileHandler(
        filename=str(log_cfg.log_file),
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    tg = cfg.telegram
    if tg.enabled and tg.bot_token and tg.chat_id:
        tg_level = logging.ERROR if tg.notify_on_error else logging.CRITICAL
        _telegram_handler = TelegramLogHandler(
            tg.bot_token,
            tg.chat_id,
            level=tg_level,
        )
        _telegram_handler.setFormatter(formatter)
        root.addHandler(_telegram_handler)
    else:
        _telegram_handler = None

    _configured = True
    logger = logging.getLogger("dwar_bot")
    logger.info(
        "Логирование инициализировано: file=%s level=%s telegram=%s",
        log_cfg.log_file,
        level_name,
        bool(_telegram_handler),
    )
    return logger


async def start_telegram_notifier() -> None:
    """Запускает фонового воркера Telegram (вызывать из asyncio loop)."""
    if _telegram_handler is not None:
        _telegram_handler.set_loop(asyncio.get_running_loop())
        await _telegram_handler.start()


async def stop_telegram_notifier() -> None:
    if _telegram_handler is not None:
        await _telegram_handler.stop()


async def notify_telegram(text: str, *, critical: bool = False) -> None:
    """Публичный хелпер уведомлений (капча, смерть персонажа и т.д.)."""
    if _telegram_handler is None:
        logging.getLogger("dwar_bot").warning(
            "Telegram не настроен, уведомление пропущено: %s", text
        )
        return
    await _telegram_handler.notify(text, critical=critical)


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    """Логирует исключение с полным traceback."""
    logger.error(
        "%s: %s\n%s",
        message,
        exc,
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )


def get_logger(name: str = "dwar_bot") -> logging.Logger:
    if not _configured:
        setup_logging()
    return logging.getLogger(name)
