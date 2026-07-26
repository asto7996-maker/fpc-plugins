"""
config.py — загрузка конфигурации Remanga Autobattle.

Минимум для старта: BOT_TOKEN в .env (или переменной окружения).
Остальные настройки (admin, URL боёв, интервал) вводятся в Telegram
и хранятся в settings.json.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта (папка, где лежит этот файл)
BASE_DIR = Path(__file__).resolve().parent

# Загружаем .env из корня проекта (если файл существует)
load_dotenv(BASE_DIR / ".env")


def _get_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    """Прочитать целое число из окружения с запасным значением по умолчанию."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


@dataclass
class Config:
    """Контейнер настроек приложения (может обновляться из Telegram)."""

    bot_token: str
    telegram_admin_id: int
    battle_url: str
    auto_battle_interval_sec: int
    user_data_dir: Path
    # Отдельный профиль браузера для MangaBuff
    mangabuff_user_data_dir: Path
    mangabuff_start_url: str
    mangabuff_delay_min_sec: float
    mangabuff_delay_max_sec: float
    mangabuff_email: str
    mangabuff_password: str
    selector_timeout_ms: int

    # Эмуляция «реального» браузера
    user_agent: str
    viewport_width: int
    viewport_height: int

    # Случайная пауза перед кликами (секунды) — Remanga
    human_delay_min_sec: float
    human_delay_max_sec: float

    def apply_runtime(self, settings) -> None:
        """Применить RuntimeSettings поверх текущего Config (in-place)."""
        if settings.telegram_admin_id > 0:
            self.telegram_admin_id = settings.telegram_admin_id
        if settings.battle_url:
            self.battle_url = settings.battle_url.strip()
        if settings.auto_battle_interval_sec >= 5:
            self.auto_battle_interval_sec = settings.auto_battle_interval_sec
        if settings.selector_timeout_ms >= 1000:
            self.selector_timeout_ms = settings.selector_timeout_ms
        if getattr(settings, "mangabuff_start_url", None):
            self.mangabuff_start_url = settings.mangabuff_start_url.strip()
        if getattr(settings, "mangabuff_delay_min_sec", 0) >= 0.05:
            self.mangabuff_delay_min_sec = float(settings.mangabuff_delay_min_sec)
        if getattr(settings, "mangabuff_delay_max_sec", 0) >= 0.05:
            self.mangabuff_delay_max_sec = float(settings.mangabuff_delay_max_sec)


def load_config() -> Config:
    """
    Собрать Config: BOT_TOKEN из .env, остальное из settings.json / дефолтов.

    Raises:
        ValueError: если не задан BOT_TOKEN.
    """
    from settings_store import load_settings

    bot_token = _get_str("BOT_TOKEN")
    if not bot_token or bot_token in {"replace_me", "YOUR_TOKEN", "xxx"}:
        raise ValueError(
            "Не задан BOT_TOKEN. Укажите его в .env или при установке "
            "(install.sh спросит токен сам)."
        )

    runtime = load_settings()

    admin_id = runtime.telegram_admin_id or _get_int("TELEGRAM_ADMIN_ID", 0)

    battle_url = runtime.battle_url or _get_str(
        "BATTLE_URL", "https://remanga.org/murim-cards#/duel"
    )
    interval = runtime.auto_battle_interval_sec or _get_int("AUTO_BATTLE_INTERVAL_SEC", 30)
    if interval < 5:
        interval = 30

    user_data_raw = _get_str("USER_DATA_DIR", "user_data") or "user_data"
    user_data_dir = Path(user_data_raw)
    if not user_data_dir.is_absolute():
        user_data_dir = BASE_DIR / user_data_dir

    mb_raw = _get_str("MANGABUFF_USER_DATA_DIR", "user_data_mangabuff") or "user_data_mangabuff"
    mb_dir = Path(mb_raw)
    if not mb_dir.is_absolute():
        mb_dir = BASE_DIR / mb_dir

    timeout_ms = runtime.selector_timeout_ms or _get_int("SELECTOR_TIMEOUT_MS", 30_000)

    return Config(
        bot_token=bot_token,
        telegram_admin_id=admin_id,
        battle_url=battle_url,
        auto_battle_interval_sec=interval,
        user_data_dir=user_data_dir,
        mangabuff_user_data_dir=mb_dir,
        mangabuff_start_url=runtime.mangabuff_start_url or "https://mangabuff.ru/",
        mangabuff_delay_min_sec=float(runtime.mangabuff_delay_min_sec or 2.8),
        mangabuff_delay_max_sec=float(runtime.mangabuff_delay_max_sec or 5.5),
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
        human_delay_min_sec=3.0,
        human_delay_max_sec=8.0,
    )
