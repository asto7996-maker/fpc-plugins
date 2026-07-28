"""
config.py — загрузка настроек из переменных окружения (.env).

Обязателен только BOT_TOKEN.
API_ID / API_HASH нужны лишь для режима юзербота (Pyrogram).
Без них бот работает через Bot API (нужны права админа в обоих каналах).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent
load_dotenv(_BASE_DIR / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения: {name}. "
            f"Скопируйте .env.example в .env и заполните значения."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _parse_int_list(raw: str) -> list[int]:
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            result.append(int(part))
    return result


# --- Telegram Bot (админ-панель + Bot API poster) ---
BOT_TOKEN: str = _require("BOT_TOKEN")

# --- Telegram Client API (опционально, для юзербота Pyrogram) ---
_API_ID_RAW = _optional("API_ID")
API_ID: int | None = int(_API_ID_RAW) if _API_ID_RAW and _API_ID_RAW.isdigit() else None
API_HASH: str = _optional("API_HASH")
SESSION_NAME: str = _optional("SESSION_NAME", "reposter_userbot") or "reposter_userbot"
# 1 = принудительно юзербот; 0 = Bot API; auto = юзербот если есть API_ID+HASH+сессия
USERBOT_MODE: str = (_optional("USERBOT_MODE", "auto") or "auto").lower()

# --- Каналы (можно задать позже через админку) ---
SOURCE_CHANNEL: str = _optional("SOURCE_CHANNEL")
TARGET_CHANNEL: str = _optional("TARGET_CHANNEL")

# --- Админы (пустой список = доступен всем, удобно при первом запуске) ---
ADMIN_IDS: list[int] = _parse_int_list(_optional("ADMIN_IDS"))

# --- Пути ---
DATABASE_PATH: Path = Path(
    _optional("DATABASE_PATH", str(_BASE_DIR / "data" / "reposter.db"))
    or str(_BASE_DIR / "data" / "reposter.db")
)
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = _BASE_DIR / DATABASE_PATH

DEFAULT_INTERVAL_HOURS: float = float(_optional("DEFAULT_INTERVAL_HOURS", "6") or "6")
DEFAULT_POSTS_PER_CYCLE: int = int(_optional("DEFAULT_POSTS_PER_CYCLE", "5") or "5")
POST_DELAY_MIN: float = float(_optional("POST_DELAY_MIN", "3") or "3")
POST_DELAY_MAX: float = float(_optional("POST_DELAY_MAX", "5") or "5")

PARSE_MODE: str = "html"


def userbot_enabled() -> bool:
    """Нужно ли поднимать Pyrogram-юзербот."""
    if USERBOT_MODE in {"0", "false", "no", "botapi"}:
        return False
    if USERBOT_MODE in {"1", "true", "yes", "userbot"}:
        return bool(API_ID and API_HASH)
    # auto
    return bool(API_ID and API_HASH)
