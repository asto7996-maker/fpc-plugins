"""
config.py — настройки Channel Reposter (Userbot + тонкий admin-бот).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent
load_dotenv(_BASE_DIR / ".env")


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _parse_int_list(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


# Admin-бот только для панели управления (не публикует контент)
BOT_TOKEN: str = _optional("BOT_TOKEN")
ADMIN_IDS: list[int] = _parse_int_list(_optional("ADMIN_IDS"))

# Юзербот (основной движок)
_API_ID_RAW = _optional("API_ID")
API_ID: int | None = int(_API_ID_RAW) if _API_ID_RAW.isdigit() else None
API_HASH: str = _optional("API_HASH")
PHONE: str = _optional("PHONE")
PASSWORD: str = _optional("PASSWORD")
SESSION_NAME: str = _optional("SESSION_NAME", "reposter_userbot") or "reposter_userbot"

SOURCE_CHANNEL: str = _optional("SOURCE_CHANNEL")
TARGET_CHANNEL: str = _optional("TARGET_CHANNEL")

DATABASE_PATH: Path = Path(
    _optional("DATABASE_PATH", str(_BASE_DIR / "data" / "reposter.db"))
    or str(_BASE_DIR / "data" / "reposter.db")
)
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = _BASE_DIR / DATABASE_PATH

def _float(name: str, default: float) -> float:
    try:
        raw = _optional(name)
        return float(raw) if raw else float(default)
    except ValueError:
        return float(default)


# Интервал между циклами по умолчанию (можно в секундах или часах)
DEFAULT_INTERVAL_SECONDS: float = (
    _float("DEFAULT_INTERVAL_SECONDS", 0.0)
    or _float("DEFAULT_INTERVAL_HOURS", 0.0) * 3600.0
    or 3600.0
)
# Интервал в режиме «догон», когда накопилась очередь непереложенных постов
DEFAULT_CATCHUP_SECONDS: float = _float("DEFAULT_CATCHUP_SECONDS", 60.0)
DEFAULT_POSTS_PER_CYCLE: int = int(_optional("DEFAULT_POSTS_PER_CYCLE", "3") or "3")
POST_DELAY_MIN: float = _float("POST_DELAY_MIN", 2.0)
POST_DELAY_MAX: float = max(POST_DELAY_MIN, _float("POST_DELAY_MAX", 4.0))

# Оконный залив: один юзербот, несколько пар. Чтобы одно окно не забивало остальные.
WINDOW_CYCLE_TIMEOUT: float = max(15.0, _float("WINDOW_CYCLE_TIMEOUT", 90.0))
PASS_TIMEOUT: float = max(WINDOW_CYCLE_TIMEOUT, _float("PASS_TIMEOUT", 180.0))
SHOP_BOT_USERNAME: str = _optional("SHOP_BOT_USERNAME", "sweetshopxxx_bot") or "sweetshopxxx_bot"
# Юзербот опрашивает лички поддержки и сам ведёт компенсацию
SUPPORT_INBOX_SECONDS: float = max(8.0, _float("SUPPORT_INBOX_SECONDS", 12.0))
SUPPORT_CATCHUP_HOURS: float = max(1.0, _float("SUPPORT_CATCHUP_HOURS", 36.0))
