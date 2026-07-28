"""
config.py — загрузка настроек из переменных окружения (.env).

Все чувствительные данные (токены, api_hash) хранятся только в .env,
а не в коде. Скопируйте .env.example → .env и заполните значения.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Загружаем .env из каталога проекта (рядом с этим файлом)
_BASE_DIR = Path(__file__).resolve().parent
load_dotenv(_BASE_DIR / ".env")


def _require(name: str) -> str:
    """Вернуть обязательную переменную окружения или выбросить понятную ошибку."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения: {name}. "
            f"Скопируйте .env.example в .env и заполните значения."
        )
    return value


def _parse_int_list(raw: str) -> list[int]:
    """Разобрать список целых через запятую: '1, 2, 3' → [1, 2, 3]."""
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            result.append(int(part))
    return result


# --- Telegram Bot (админ-панель, aiogram) ---
BOT_TOKEN: str = _require("BOT_TOKEN")

# --- Telegram Client API (юзербот, Pyrogram) ---
API_ID: int = int(_require("API_ID"))
API_HASH: str = _require("API_HASH")
SESSION_NAME: str = os.getenv("SESSION_NAME", "reposter_userbot").strip()

# --- Каналы ---
# Могут быть @username или числовой ID (-100xxxxxxxxxx)
SOURCE_CHANNEL: str = _require("SOURCE_CHANNEL")
TARGET_CHANNEL: str = _require("TARGET_CHANNEL")

# --- Админы ---
ADMIN_IDS: list[int] = _parse_int_list(os.getenv("ADMIN_IDS", ""))

# --- Пути ---
DATABASE_PATH: Path = Path(
    os.getenv("DATABASE_PATH", str(_BASE_DIR / "data" / "reposter.db"))
)
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = _BASE_DIR / DATABASE_PATH

# --- Дефолты планировщика (переопределяются в SQLite через админку) ---
DEFAULT_INTERVAL_HOURS: float = float(os.getenv("DEFAULT_INTERVAL_HOURS", "6"))
DEFAULT_POSTS_PER_CYCLE: int = int(os.getenv("DEFAULT_POSTS_PER_CYCLE", "5"))
POST_DELAY_MIN: float = float(os.getenv("POST_DELAY_MIN", "3"))
POST_DELAY_MAX: float = float(os.getenv("POST_DELAY_MAX", "5"))

# Режим разметки подписи постов (HTML сохраняет bold/italic/ссылки)
PARSE_MODE: str = "html"
