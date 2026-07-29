"""
config.py — настройки из .env (только Bot API).
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
        raise RuntimeError(f"Задайте {name} в .env")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _parse_int_list(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


BOT_TOKEN: str = _require("BOT_TOKEN")
SOURCE_CHANNEL: str = _optional("SOURCE_CHANNEL")
TARGET_CHANNEL: str = _optional("TARGET_CHANNEL")
ADMIN_IDS: list[int] = _parse_int_list(_optional("ADMIN_IDS"))

# Юзербот (сессия уже может быть сохранена в SQLite / .session)
_API_ID_RAW = _optional("API_ID")
API_ID: int | None = int(_API_ID_RAW) if _API_ID_RAW.isdigit() else None
API_HASH: str = _optional("API_HASH")
PHONE: str = _optional("PHONE")
PASSWORD: str = _optional("PASSWORD")
SESSION_NAME: str = _optional("SESSION_NAME", "reposter_userbot") or "reposter_userbot"

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
