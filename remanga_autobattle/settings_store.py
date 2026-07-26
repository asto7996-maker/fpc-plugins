"""
settings_store.py — настройки MangaBuff из Telegram (settings.json).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict

from config import BASE_DIR

logger = logging.getLogger(__name__)

SETTINGS_PATH = BASE_DIR / "settings.json"


@dataclass
class RuntimeSettings:
    telegram_admin_id: int = 0
    selector_timeout_ms: int = 30_000
    setup_completed: bool = False

    mangabuff_start_url: str = "https://mangabuff.ru/"
    mangabuff_delay_min_sec: float = 0.20
    mangabuff_delay_max_sec: float = 0.45
    mangabuff_setup_done: bool = False
    mangabuff_speed_preset: str = "lively"
    mangabuff_milestone_every: int = 10
    mangabuff_notify_milestones: bool = True
    mangabuff_notify_cards: bool = True
    mangabuff_farm_enabled: bool = False
    mangabuff_events_farm_enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeSettings":
        known = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in data.items() if k in known}
        return cls(**cleaned)


def load_settings() -> RuntimeSettings:
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
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(SETTINGS_PATH)
    logger.info("Настройки сохранены в %s", SETTINGS_PATH)


def update_settings(**kwargs: Any) -> RuntimeSettings:
    current = load_settings()
    for key, value in kwargs.items():
        if hasattr(current, key):
            setattr(current, key, value)
    save_settings(current)
    return current
