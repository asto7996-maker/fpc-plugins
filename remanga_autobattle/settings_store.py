"""
settings_store.py — сохранение настроек, заданных через Telegram-бота.

Файл settings.json лежит рядом со скриптами. BOT_TOKEN по-прежнему в .env
(нужен, чтобы бот вообще запустился); всё остальное можно менять в чате.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

from config import BASE_DIR

logger = logging.getLogger(__name__)

SETTINGS_PATH = BASE_DIR / "settings.json"


@dataclass
class NotifySettings:
    """Какие события слать в Telegram-чат."""

    # Отчёты о боях
    notify_wins: bool = True
    notify_losses: bool = True
    notify_draws: bool = True
    notify_skipped: bool = False
    notify_errors: bool = True
    # Служебные
    notify_autobattle_start_stop: bool = True
    # Краткая сводка каждые N боёв (0 = выкл)
    notify_summary_every: int = 0
    # Если True — в чат только сводки/старт-стоп, без поединичных отчётов
    quiet_mode: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NotifySettings":
        known = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**cleaned)

    def allows_outcome(self, outcome: str) -> bool:
        """Можно ли слать отчёт по исходу боя."""
        if self.quiet_mode:
            return False
        mapping = {
            "win": self.notify_wins,
            "lose": self.notify_losses,
            "draw": self.notify_draws,
            "skipped": self.notify_skipped,
            "error": self.notify_errors,
            "unknown": self.notify_errors,
        }
        return bool(mapping.get(outcome, True))

    def to_telegram(self) -> str:
        def mark(on: bool) -> str:
            return "✅ Вкл" if on else "❌ Выкл"

        summary = (
            f"каждые {self.notify_summary_every} боёв"
            if self.notify_summary_every > 0
            else "выкл"
        )
        return (
            "<b>🔔 Уведомления</b>\n\n"
            f"🏆 Победы: {mark(self.notify_wins)}\n"
            f"💀 Поражения: {mark(self.notify_losses)}\n"
            f"🤝 Ничьи: {mark(self.notify_draws)}\n"
            f"⏸ Пропуски: {mark(self.notify_skipped)}\n"
            f"⚠️ Ошибки: {mark(self.notify_errors)}\n"
            f"▶️ Старт/стоп автобоя: {mark(self.notify_autobattle_start_stop)}\n"
            f"📋 Сводка: <b>{summary}</b>\n"
            f"🤫 Тихий режим: {mark(self.quiet_mode)}\n\n"
            "<i>Тихий режим отключает отчёты по каждому бою "
            "(сводка и старт/стоп работают отдельно).</i>"
        )


@dataclass
class RuntimeSettings:
    """Настройки, которыми управляют из Telegram."""

    telegram_admin_id: int = 0
    battle_url: str = "https://remanga.org/murim-cards#/duel"
    auto_battle_interval_sec: int = 30
    selector_timeout_ms: int = 30_000
    # False = ещё не проходили мастер настройки в Telegram
    setup_completed: bool = False
    # Управление уведомлениями Remanga
    notify: Optional[Dict[str, Any]] = field(default=None)
    # Автобой Remanga: переживает рестарт/обновление
    remanga_autobattle_enabled: bool = False

    # ---- MangaBuff ----
    mangabuff_start_url: str = "https://mangabuff.ru/"
    mangabuff_delay_min_sec: float = 0.20
    mangabuff_delay_max_sec: float = 0.45
    mangabuff_setup_done: bool = False
    mangabuff_speed_preset: str = "lively"
    # Вехи в Telegram каждые N глав (0 = выкл)
    mangabuff_milestone_every: int = 10
    mangabuff_notify_milestones: bool = True
    # Фарм MangaBuff: переживает рестарт/обновление
    mangabuff_farm_enabled: bool = False

    def __post_init__(self) -> None:
        if self.notify is None:
            self.notify = NotifySettings().to_dict()

    def notify_settings(self) -> NotifySettings:
        return NotifySettings.from_dict(self.notify or {})

    def set_notify(self, ns: NotifySettings) -> None:
        self.notify = ns.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeSettings":
        known = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in data.items() if k in known}
        obj = cls(**cleaned)
        if obj.notify is None:
            obj.notify = NotifySettings().to_dict()
        return obj


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


def update_notify(**kwargs: Any) -> NotifySettings:
    """Обновить флаги уведомлений и сохранить."""
    current = load_settings()
    ns = current.notify_settings()
    for key, value in kwargs.items():
        if hasattr(ns, key):
            setattr(ns, key, value)
    current.set_notify(ns)
    save_settings(current)
    return ns


def toggle_notify(key: str) -> NotifySettings:
    """Переключить булев флаг уведомлений."""
    current = load_settings()
    ns = current.notify_settings()
    if not hasattr(ns, key):
        return ns
    val = getattr(ns, key)
    if isinstance(val, bool):
        setattr(ns, key, not val)
    current.set_notify(ns)
    save_settings(current)
    return ns
