"""
config.py — загрузка и валидация конфигурации проекта Remanga Autobattle.

Все секреты и настройки читаются из файла `.env` (или переменных окружения).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта (папка, где лежит этот файл)
BASE_DIR = Path(__file__).resolve().parent

# Загружаем .env из корня проекта (если файл существует)
load_dotenv(BASE_DIR / ".env")


def _require_str(name: str) -> str:
    """Вернуть обязательную строковую переменную или выбросить понятную ошибку."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(
            f"Не задана обязательная переменная окружения: {name}. "
            f"Скопируйте .env.example в .env и заполните значения."
        )
    return value


def _get_int(name: str, default: int) -> int:
    """Прочитать целое число из окружения с запасным значением по умолчанию."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    """Неизменяемый контейнер настроек приложения."""

    bot_token: str
    telegram_admin_id: int
    battle_url: str
    auto_battle_interval_sec: int
    user_data_dir: Path
    selector_timeout_ms: int

    # Эмуляция «реального» браузера
    user_agent: str
    viewport_width: int
    viewport_height: int

    # Случайная пауза перед кликами (секунды)
    human_delay_min_sec: float
    human_delay_max_sec: float


def load_config() -> Config:
    """
    Собрать Config из переменных окружения.

    Raises:
        ValueError: если обязательные поля отсутствуют или некорректны.
    """
    bot_token = _require_str("BOT_TOKEN")
    admin_id = _get_int("TELEGRAM_ADMIN_ID", 0)
    if admin_id <= 0:
        raise ValueError(
            "TELEGRAM_ADMIN_ID должен быть положительным числом "
            "(ваш Telegram user id, например из @userinfobot)."
        )

    battle_url = os.getenv("BATTLE_URL", "https://remanga.org/cards").strip()
    # По умолчанию 30 сек; автобой крутится бесконечно до ручной остановки
    interval = _get_int("AUTO_BATTLE_INTERVAL_SEC", 30)
    if interval < 5:
        raise ValueError("AUTO_BATTLE_INTERVAL_SEC не должен быть меньше 5 секунд.")

    user_data_raw = os.getenv("USER_DATA_DIR", "user_data").strip() or "user_data"
    user_data_dir = Path(user_data_raw)
    if not user_data_dir.is_absolute():
        user_data_dir = BASE_DIR / user_data_dir

    # Таймаут ожидания элементов — 30 секунд по умолчанию
    timeout_ms = _get_int("SELECTOR_TIMEOUT_MS", 30_000)

    return Config(
        bot_token=bot_token,
        telegram_admin_id=admin_id,
        battle_url=battle_url,
        auto_battle_interval_sec=interval,
        user_data_dir=user_data_dir,
        selector_timeout_ms=timeout_ms,
        # Реалистичный Chrome UA под Windows + Full HD
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport_width=1920,
        viewport_height=1080,
        human_delay_min_sec=3.0,
        human_delay_max_sec=8.0,
    )
