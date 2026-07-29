"""
config.py — конфигурация MangaBuff Autopilot.

Обязателен BOT_TOKEN в .env. Остальное — settings.json / Telegram.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"Переменная {name} должна быть целым числом, получено: {raw!r}"
        ) from exc


@dataclass
class Config:
    bot_token: str
    telegram_admin_id: int
    mangabuff_user_data_dir: Path
    mangabuff_start_url: str
    mangabuff_delay_min_sec: float
    mangabuff_delay_max_sec: float
    mangabuff_email: str
    mangabuff_password: str
    selector_timeout_ms: int
    user_agent: str
    viewport_width: int
    viewport_height: int

    def apply_runtime(self, settings) -> None:
        if settings.telegram_admin_id > 0:
            self.telegram_admin_id = settings.telegram_admin_id
        if settings.selector_timeout_ms >= 1000:
            self.selector_timeout_ms = settings.selector_timeout_ms
        if getattr(settings, "mangabuff_start_url", None):
            self.mangabuff_start_url = settings.mangabuff_start_url.strip()
        if getattr(settings, "mangabuff_delay_min_sec", 0) >= 0.01:
            self.mangabuff_delay_min_sec = float(settings.mangabuff_delay_min_sec)
        if getattr(settings, "mangabuff_delay_max_sec", 0) >= 0.01:
            self.mangabuff_delay_max_sec = float(settings.mangabuff_delay_max_sec)


def load_config() -> Config:
    from settings_store import load_settings

    bot_token = _get_str("BOT_TOKEN")
    if not bot_token or bot_token in {"replace_me", "YOUR_TOKEN", "xxx"}:
        raise ValueError(
            "Не задан BOT_TOKEN. Укажите его в .env или при установке."
        )

    runtime = load_settings()
    admin_id = runtime.telegram_admin_id or _get_int("TELEGRAM_ADMIN_ID", 0)

    mb_raw = _get_str("MANGABUFF_USER_DATA_DIR", "user_data_mangabuff") or "user_data_mangabuff"
    mb_dir = Path(mb_raw)
    if not mb_dir.is_absolute():
        mb_dir = BASE_DIR / mb_dir

    timeout_ms = runtime.selector_timeout_ms or _get_int("SELECTOR_TIMEOUT_MS", 30_000)

    return Config(
        bot_token=bot_token,
        telegram_admin_id=admin_id,
        mangabuff_user_data_dir=mb_dir,
        mangabuff_start_url=runtime.mangabuff_start_url or "https://mangabuff.ru/",
        mangabuff_delay_min_sec=float(runtime.mangabuff_delay_min_sec or 0.20),
        mangabuff_delay_max_sec=float(runtime.mangabuff_delay_max_sec or 0.45),
        mangabuff_email=_get_str("MANGABUFF_EMAIL"),
        mangabuff_password=_get_str("MANGABUFF_PASSWORD"),
        selector_timeout_ms=timeout_ms,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport_width=1920,
        viewport_height=1080,
    )
