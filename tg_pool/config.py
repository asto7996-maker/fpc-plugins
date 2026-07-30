"""Runtime configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
load_dotenv(PACKAGE_DIR / ".env", override=False)
load_dotenv(PACKAGE_DIR.parent / ".env", override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Core
    database_url: str
    redis_url: str
    admin_bot_token: str
    admin_ids: tuple[int, ...]
    log_level: str

    # Default Telegram API credentials (can be overridden per-account later)
    telegram_api_id: int
    telegram_api_hash: str

    # Action limits / jitter
    daily_action_limit: int
    jitter_min_sec: float
    jitter_max_sec: float
    flood_alert_threshold_sec: int  # alert admin if FloodWait longer than this

    # SpamBot probe
    spambot_username: str
    spambot_timeout_sec: float


def get_settings() -> Settings:
    admin_ids = tuple(
        int(x.strip())
        for x in os.getenv("ADMIN_IDS", "").split(",")
        if x.strip().isdigit()
    )
    return Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://tg_pool:tg_pool@localhost:5432/tg_pool",
        ),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        admin_bot_token=os.getenv("ADMIN_BOT_TOKEN", "").strip(),
        admin_ids=admin_ids,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        telegram_api_id=int(os.getenv("TELEGRAM_API_ID", "0") or "0"),
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", "").strip(),
        daily_action_limit=int(os.getenv("DAILY_ACTION_LIMIT", "20")),
        jitter_min_sec=float(os.getenv("JITTER_MIN_SEC", "3.0")),
        jitter_max_sec=float(os.getenv("JITTER_MAX_SEC", "12.0")),
        flood_alert_threshold_sec=int(os.getenv("FLOOD_ALERT_THRESHOLD_SEC", "300")),
        spambot_username=os.getenv("SPAMBOT_USERNAME", "SpamBot"),
        spambot_timeout_sec=float(os.getenv("SPAMBOT_TIMEOUT_SEC", "30")),
    )
