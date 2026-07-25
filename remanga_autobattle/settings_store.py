"""
settings_store.py — сохранение настроек, заданных через Telegram-бота.

Файл settings.json лежит рядом со скриптами. BOT_TOKEN по-прежнему в .env
(нужен, чтобы бот вообще запустился); всё остальное можно менять в чате.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional

from config import BASE_DIR

logger = logging.getLogger(__name__)

SETTINGS_PATH = BASE_DIR / "settings.json"


@dataclass
class RuntimeSettings:
    """Настройки, которыми управляют из Telegram."""

    telegram_admin_id: int = 0
    battle_url: str = "https://remanga.org/cards"
    auto_battle_interval_sec: int = 30
    selector_timeout_ms: int = 30_000
    # False = ещё не проходили мастер настройки в Telegram
    setup_completed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeSettings":
        known = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in data.items() if k in known}
        return cls(**cleaned)


def load_settings() -> RuntimeSettings:
    """Загрузить settings.json или вернуть значения по умолчанию."""
    if not SETTINGS_PATH.exists():
        return RuntimeSettings()
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return RuntimeSettings()
        return RuntimeSettings.from_dict(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось прочитать %s: %s", SETTINGS_PATH, exc)
        return RuntimeSettings()


def save_settings(settings: RuntimeSettings) -> None:
    """Атомарно сохранить настройки на диск."""
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(SETTINGS_PATH)
    logger.info("Настройки сохранены в %s", SETTINGS_PATH)


def update_settings(**kwargs: Any) -> RuntimeSettings:
    """Обновить отдельные поля и сохранить."""
    current = load_settings()
    for key, value in kwargs.items():
        if hasattr(current, key):
            setattr(current, key, value)
    save_settings(current)
    return current
