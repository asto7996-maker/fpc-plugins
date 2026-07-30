"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BRAND_MONITOR_DATA_DIR", BASE_DIR / "data"))
DEFAULT_DB_PATH = DATA_DIR / "brand_monitor.db"


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the brand monitoring system."""

    database_path: Path = Path(os.getenv("DATABASE_PATH", str(DEFAULT_DB_PATH)))
    admin_bot_token: str = os.getenv("ADMIN_BOT_TOKEN", "")
    admin_ids: tuple[int, ...] = tuple(
        int(x.strip())
        for x in os.getenv("ADMIN_IDS", "").split(",")
        if x.strip().isdigit()
    )
    typing_delay_min: float = float(os.getenv("TYPING_DELAY_MIN", "2.0"))
    typing_delay_max: float = float(os.getenv("TYPING_DELAY_MAX", "6.0"))
    backoff_base: float = float(os.getenv("BACKOFF_BASE", "1.0"))
    backoff_max: float = float(os.getenv("BACKOFF_MAX", "60.0"))
    backoff_max_retries: int = int(os.getenv("BACKOFF_MAX_RETRIES", "8"))
    reconnect_max_attempts: int = int(os.getenv("RECONNECT_MAX_ATTEMPTS", "5"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


def get_settings() -> Settings:
    """Return settings instance and ensure data directory exists."""
    settings = Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
