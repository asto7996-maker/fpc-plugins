"""
Persistent bot settings — autopilot toggles, notifications, reports.

Saved to ``dwar_bot/state.json`` so Telegram UI changes survive restarts.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from dwar_bot.config import STATE_FILE

logger = logging.getLogger(__name__)


@dataclass
class NotifySettings:
    battles: bool = True
    quests: bool = True
    hp_low: bool = True
    token: bool = True
    level_up: bool = True
    money: bool = True
    errors: bool = True
    effects: bool = False
    area: bool = True
    gear: bool = False
    heartbeat: bool = False
    loot: bool = True
    plan: bool = True


@dataclass
class FarmSettings:
    """Autopilot modules — each can be toggled from Telegram."""
    auto_quests: bool = True
    auto_combat: bool = True
    farm_fronts: bool = True
    farm_arena: bool = True
    farm_area: bool = True
    auto_travel: bool = True
    auto_repair: bool = True
    auto_equip: bool = True
    auto_heal: bool = True
    auto_loot: bool = True
    max_farm: bool = True
    idle_pauses: bool = True
    aggressive: bool = False
    hp_retreat: float = 15.0
    hp_heal: float = 40.0
    max_battles_row: int = 20


@dataclass
class ReportSettings:
    enabled: bool = True
    interval_min: int = 30
    include_combat: bool = True
    include_quests: bool = True
    include_inventory: bool = True
    include_timers: bool = True
    include_plan: bool = True


@dataclass
class BotSettings:
    farm: FarmSettings = field(default_factory=FarmSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    report: ReportSettings = field(default_factory=ReportSettings)
    # Runtime counters / meta (also persisted)
    last_report_at: float = 0.0
    total_notifies_sent: int = 0
    updated_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotSettings":
        farm = FarmSettings(**{
            k: v for k, v in (data.get("farm") or {}).items()
            if k in FarmSettings.__dataclass_fields__
        })
        notify = NotifySettings(**{
            k: v for k, v in (data.get("notify") or {}).items()
            if k in NotifySettings.__dataclass_fields__
        })
        report = ReportSettings(**{
            k: v for k, v in (data.get("report") or {}).items()
            if k in ReportSettings.__dataclass_fields__
        })
        return cls(
            farm=farm,
            notify=notify,
            report=report,
            last_report_at=float(data.get("last_report_at", 0) or 0),
            total_notifies_sent=int(data.get("total_notifies_sent", 0) or 0),
            updated_at=float(data.get("updated_at", time.time()) or time.time()),
        )

    def save(self, path: Optional[Path] = None) -> None:
        target = Path(path) if path else STATE_FILE
        self.updated_at = time.time()
        # Merge with any extra keys already in state.json
        existing: dict[str, Any] = {}
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        existing["settings"] = self.to_dict()
        target.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug("Settings saved → %s", target)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "BotSettings":
        target = Path(path) if path else STATE_FILE
        if not target.exists():
            s = cls()
            s.save(target)
            return s
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            raw = data.get("settings") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                return cls.from_dict(raw)
        except Exception as exc:
            logger.warning("Failed to load settings: %s — using defaults.", exc)
        return cls()

    # ------------------------------------------------------------------
    # Toggle helpers used by Telegram UI
    # ------------------------------------------------------------------

    def toggle(self, group: str, key: str) -> Optional[bool]:
        """Flip a bool setting. Returns new value or None if unknown."""
        obj = getattr(self, group, None)
        if obj is None or not hasattr(obj, key):
            return None
        cur = getattr(obj, key)
        if not isinstance(cur, bool):
            return None
        setattr(obj, key, not cur)
        self.save()
        return getattr(obj, key)

    def set_bool(self, group: str, key: str, value: bool) -> bool:
        obj = getattr(self, group, None)
        if obj is None or not hasattr(obj, key):
            return False
        setattr(obj, key, bool(value))
        self.save()
        return True

    def set_number(self, group: str, key: str, value: float) -> bool:
        obj = getattr(self, group, None)
        if obj is None or not hasattr(obj, key):
            return False
        field_type = type(getattr(obj, key))
        try:
            setattr(obj, key, field_type(value))
        except Exception:
            return False
        self.save()
        return True

    def on_off(self, value: bool) -> str:
        return "🟢 ВКЛ" if value else "🔴 ВЫКЛ"

    def farm_summary_lines(self) -> list[str]:
        f = self.farm
        return [
            f"🚀 Макс-фарм: {self.on_off(f.max_farm)}",
            f"📜 Квесты: {self.on_off(f.auto_quests)}",
            f"⚔️ Бои (авто): {self.on_off(f.auto_combat)}",
            f"  ├ Фронты: {self.on_off(f.farm_fronts)}",
            f"  ├ Арена: {self.on_off(f.farm_arena)}",
            f"  └ Точки локации: {self.on_off(f.farm_area)}",
            f"🎁 Лут / награды: {self.on_off(f.auto_loot)}",
            f"🗺 Переходы: {self.on_off(f.auto_travel)}",
            f"🔧 Ремонт: {self.on_off(f.auto_repair)}",
            f"👕 Экипировка: {self.on_off(f.auto_equip)}",
            f"🧪 Лечение: {self.on_off(f.auto_heal)}",
            f"💤 Idle-паузы: {self.on_off(f.idle_pauses)}",
            f"🔥 Агрессивный: {self.on_off(f.aggressive)}",
            f"❤️ Retreat HP: {f.hp_retreat:.0f}%  Heal: {f.hp_heal:.0f}%",
        ]

    def notify_summary_lines(self) -> list[str]:
        n = self.notify
        return [
            f"⚔️ Бои: {self.on_off(n.battles)}",
            f"📜 Квесты: {self.on_off(n.quests)}",
            f"🎁 Лут: {self.on_off(n.loot)}",
            f"🧠 План: {self.on_off(n.plan)}",
            f"❤️ HP низко: {self.on_off(n.hp_low)}",
            f"🔑 Токен: {self.on_off(n.token)}",
            f"⬆️ Уровень: {self.on_off(n.level_up)}",
            f"💰 Деньги: {self.on_off(n.money)}",
            f"❌ Ошибки: {self.on_off(n.errors)}",
            f"✨ Эффекты: {self.on_off(n.effects)}",
            f"🗺 Локация: {self.on_off(n.area)}",
            f"🛡 Снаряжение: {self.on_off(n.gear)}",
            f"💓 Heartbeat: {self.on_off(n.heartbeat)}",
        ]
