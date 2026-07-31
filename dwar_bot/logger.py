"""
logger.py
=========

Единая подсистема логирования бота.

Возможности:

* цветной (по возможности) вывод в консоль;
* ротируемый файловый лог ``logs/bot.log`` с полным traceback ошибок;
* опциональная отправка важных событий в Telegram — неблокирующая, через
  фоновый поток, с защитой от флуда (rate-limit) и без внешних зависимостей
  (используется ``urllib`` из стандартной библиотеки).

Использование::

    from dwar_bot.logger import get_logger

    log = get_logger(__name__)
    log.info("Бот запущен")
    try:
        risky()
    except Exception:
        log.exception("Упало в risky()")   # traceback уедет в файл и Telegram
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from typing import Final

from .config import settings

# ---------------------------------------------------------------------------
# Формат сообщений.
# ---------------------------------------------------------------------------
_LOG_FORMAT: Final[str] = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s"
)
_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


class _ColorFormatter(logging.Formatter):
    """Форматтер с ANSI-цветами для консоли (безопасен, если цвет не поддержан)."""

    _COLORS: Final[dict[int, str]] = {
        logging.DEBUG: "\033[37m",     # серый
        logging.INFO: "\033[36m",      # голубой
        logging.WARNING: "\033[33m",   # жёлтый
        logging.ERROR: "\033[31m",     # красный
        logging.CRITICAL: "\033[41m",  # красный фон
    }
    _RESET: Final[str] = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        color = self._COLORS.get(record.levelno)
        if color:
            return f"{color}{base}{self._RESET}"
        return base


class TelegramHandler(logging.Handler):
    """
    Неблокирующий обработчик логов, отправляющий сообщения в Telegram.

    Записи складываются в очередь и отправляются фоновым демон-потоком, чтобы
    сетевые задержки Telegram никогда не тормозили основной цикл бота.
    Реализован простейший rate-limit: не чаще одного сообщения в
    ``min_interval`` секунд одинакового содержания.
    """

    _API_URL: Final[str] = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        level: int = logging.ERROR,
        min_interval: float = 3.0,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(level=level)
        self._token = token
        self._chat_id = chat_id
        self._min_interval = min_interval
        self._timeout = timeout
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=1000)
        self._last_sent: dict[str, float] = {}
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="telegram-log-worker", daemon=True
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            # Rate-limit одинаковых сообщений.
            now = time.monotonic()
            last = self._last_sent.get(message)
            if last is not None and (now - last) < self._min_interval:
                return
            self._last_sent[message] = now
            # Ограничиваем размер словаря, чтобы не расти бесконечно.
            if len(self._last_sent) > 512:
                self._last_sent.clear()
            self._queue.put_nowait(message)
        except queue.Full:
            # Очередь переполнена — молча отбрасываем, лог в файл уже ушёл.
            pass
        except Exception:  # noqa: BLE001 - обработчик логов не должен падать
            self.handleError(record)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._send(message)
            except Exception:  # noqa: BLE001 - сеть может падать, это не критично
                pass
            finally:
                self._queue.task_done()

    def _send(self, message: str) -> None:
        # Telegram ограничивает длину сообщения 4096 символами.
        text = message[:4000]
        payload = urllib.parse.urlencode(
            {
                "chat_id": self._chat_id,
                "text": f"\U0001F916 dwar_bot\n{text}",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        url = self._API_URL.format(token=self._token)
        request = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            # Читаем тело, чтобы соединение корректно закрылось.
            raw = response.read()
            try:
                data = json.loads(raw.decode("utf-8"))
                if not data.get("ok", False):
                    raise RuntimeError(f"Telegram API error: {data}")
            except json.JSONDecodeError:
                # Не JSON — просто игнорируем, доставка не критична.
                pass

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._worker.join(timeout=2.0)
        finally:
            super().close()


def _level_from_name(name: str, default: int = logging.INFO) -> int:
    """Преобразовать имя уровня ('INFO') в число logging.INFO."""
    resolved = logging.getLevelName(name.upper())
    return resolved if isinstance(resolved, int) else default


# ---------------------------------------------------------------------------
# Единоразовая настройка корневого логгера пакета.
# ---------------------------------------------------------------------------
_ROOT_NAME: Final[str] = "dwar_bot"
_configured: bool = False
_config_lock = threading.Lock()


def _configure_root() -> logging.Logger:
    """Идемпотентно сконфигурировать корневой логгер пакета."""
    global _configured
    root = logging.getLogger(_ROOT_NAME)
    if _configured:
        return root

    with _config_lock:
        if _configured:  # повторная проверка под локом
            return root

        log_cfg = settings.logging
        root.setLevel(_level_from_name(log_cfg.level, logging.INFO))
        root.propagate = False

        # --- Консольный обработчик ---
        console = logging.StreamHandler()
        console.setLevel(root.level)
        console.setFormatter(_ColorFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(console)

        # --- Файловый обработчик с ротацией ---
        try:
            log_cfg.file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                filename=str(log_cfg.file),
                maxBytes=log_cfg.max_bytes,
                backupCount=log_cfg.backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(root.level)
            file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
            root.addHandler(file_handler)
        except OSError as exc:  # диск/права — не валим весь запуск
            root.warning("Не удалось создать файловый лог %s: %s", log_cfg.file, exc)

        # --- Telegram обработчик (опционально) ---
        if log_cfg.telegram_ready:
            try:
                tg_handler = TelegramHandler(
                    token=log_cfg.telegram_token,
                    chat_id=log_cfg.telegram_chat_id,
                    level=_level_from_name(log_cfg.telegram_level, logging.ERROR),
                )
                tg_handler.setFormatter(
                    logging.Formatter(
                        "%(levelname)s | %(name)s | %(funcName)s:%(lineno)d\n%(message)s"
                    )
                )
                root.addHandler(tg_handler)
                root.debug("Telegram-логирование включено.")
            except Exception as exc:  # noqa: BLE001
                root.warning("Не удалось инициализировать Telegram-логгер: %s", exc)

        _configured = True
    return root


def get_logger(name: str | None = None) -> logging.Logger:
    """
    Вернуть настроенный логгер.

    :param name: обычно ``__name__`` вызывающего модуля. Логгер станет дочерним
        к корневому ``dwar_bot`` и унаследует все обработчики.
    """
    _configure_root()
    if not name or name == _ROOT_NAME:
        return logging.getLogger(_ROOT_NAME)
    if name.startswith(_ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


__all__ = ["get_logger", "TelegramHandler"]
