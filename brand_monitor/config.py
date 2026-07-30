"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parent
DATA_DIR_DEFAULT = BASE_DIR / "data"


def _load_env_files() -> None:
    """Load local secrets without committing them (.env is gitignored)."""
    load_dotenv(PACKAGE_DIR / ".env", override=False)
    load_dotenv(BASE_DIR / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the brand monitoring system."""

    database_path: Path
    admin_bot_token: str
    admin_ids: tuple[int, ...]
    typing_delay_min: float
    typing_delay_max: float
    backoff_base: float
    backoff_max: float
    backoff_max_retries: int
    reconnect_max_attempts: int
    log_level: str


def get_settings() -> Settings:
    """Return settings instance and ensure data directory exists."""
    _load_env_files()

    data_dir = Path(os.getenv("BRAND_MONITOR_DATA_DIR", str(DATA_DIR_DEFAULT)))
    database_path = Path(os.getenv("DATABASE_PATH", str(data_dir / "brand_monitor.db")))
    database_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        database_path=database_path,
        admin_bot_token=os.getenv("ADMIN_BOT_TOKEN", "").strip(),
        admin_ids=tuple(
            int(x.strip())
            for x in os.getenv("ADMIN_IDS", "").split(",")
            if x.strip().isdigit()
        ),
        typing_delay_min=float(os.getenv("TYPING_DELAY_MIN", "2.0")),
        typing_delay_max=float(os.getenv("TYPING_DELAY_MAX", "6.0")),
        backoff_base=float(os.getenv("BACKOFF_BASE", "1.0")),
        backoff_max=float(os.getenv("BACKOFF_MAX", "60.0")),
        backoff_max_retries=int(os.getenv("BACKOFF_MAX_RETRIES", "8")),
        reconnect_max_attempts=int(os.getenv("RECONNECT_MAX_ATTEMPTS", "5")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
