"""Система логирования бота Dwar.

Особенности:
    * единый форматтер с временем, уровнем, модулем и сообщением;
    * ротация файла ``bot.log`` (``RotatingFileHandler``);
    * вывод в консоль;
    * форматирование исключений с полным traceback;
    * опциональная отправка важных событий в Telegram (неблокирующая —
      выполняется в отдельном треде, чтобы не тормозить основной async-цикл).

Логгер настраивается один раз (идемпотентно) при первом вызове
:func:`get_logger`.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import threading
from typing import Optional

from .config import CONFIG

try:  # requests опционален; без него Telegram-хендлер просто не активируется.
    import requests  # type: ignore
except Exception:  # noqa: BLE001
    requests = None  # type: ignore


_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False
_configure_lock = threading.Lock()


class TelegramLogHandler(logging.Handler):
    """Хендлер, отправляющий записи лога в Telegram.

    Отправка выполняется асинхронно через внутреннюю очередь и воркер-тред,
    чтобы сетевые задержки Telegram API не блокировали основной цикл бота.
    Если ``requests`` недоступен или конфиг неполон — хендлер не активен.
    """

    def __init__(self, token: str, chat_id: str, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self._token = token
        self._chat_id = chat_id
        self._api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=1000)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="tg-log-worker", daemon=True
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        if requests is None:
            return
        try:
            message = self.format(record)
            # Ограничение Telegram — 4096 символов.
            if len(message) > 3900:
                message = message[:3900] + "\n… (обрезано)"
            try:
                self._queue.put_nowait(message)
            except queue.Full:
                # Переполнение очереди уведомлений — молча дропаем.
                pass
        except Exception:  # noqa: BLE001 - логгер не должен падать
            self.handleError(record)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if message is None:
                break
            self._send(message)
            self._queue.task_done()

    def _send(self, message: str) -> None:
        if requests is None:
            return
        try:
            requests.post(
                self._api_url,
                json={
                    "chat_id": self._chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception:  # noqa: BLE001 - сетевые сбои Telegram игнорируем
            pass

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        super().close()


def _level_from_name(name: str, default: int = logging.INFO) -> int:
    return getattr(logging, str(name).upper(), default)


def _configure_root() -> None:
    """Однократная настройка корневого логгера пакета."""
    global _configured
    if _configured:
        return
    with _configure_lock:
        if _configured:
            return

        CONFIG.runtime.ensure_dirs()

        root = logging.getLogger("dwar_bot")
        root.setLevel(_level_from_name(CONFIG.runtime.log_level))
        root.propagate = False

        formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

        # --- Файловый хендлер с ротацией --- #
        log_path = CONFIG.runtime.logs_dir / CONFIG.runtime.log_file
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)

        # --- Консольный хендлер --- #
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(_level_from_name(CONFIG.runtime.log_level))
        root.addHandler(console_handler)

        # --- Telegram-хендлер (опционально) --- #
        tg = CONFIG.telegram
        if tg.enabled and tg.bot_token and tg.chat_id and requests is not None:
            tg_handler = TelegramLogHandler(
                token=tg.bot_token,
                chat_id=tg.chat_id,
                level=_level_from_name(tg.min_level, logging.WARNING),
            )
            tg_handler.setFormatter(formatter)
            root.addHandler(tg_handler)
            root.info("Telegram-уведомления включены (min_level=%s)", tg.min_level)
        elif tg.enabled and requests is None:
            root.warning(
                "Telegram включён в конфиге, но пакет 'requests' не установлен — "
                "уведомления отключены."
            )

        _configured = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Возвращает настроенный логгер.

    ``name`` обычно — ``__name__`` вызывающего модуля. Все логгеры являются
    дочерними по отношению к ``dwar_bot`` и наследуют его хендлеры.
    """
    _configure_root()
    if not name or name == "dwar_bot":
        return logging.getLogger("dwar_bot")
    if not name.startswith("dwar_bot"):
        # Приводим сторонние имена под общий корень.
        short = name.split(".")[-1]
        name = f"dwar_bot.{short}"
    return logging.getLogger(name)


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    """Единообразно логирует исключение с полным traceback."""
    logger.error("%s: %s: %s", message, type(exc).__name__, exc, exc_info=exc)


__all__ = ["get_logger", "log_exception", "TelegramLogHandler"]
