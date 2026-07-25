"""
stats_store.py — постоянная статистика боёв и кэш рейтинга.

Файл stats.json рядом со скриптами. Обновляется после каждого боя.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import BASE_DIR

logger = logging.getLogger(__name__)

STATS_PATH = BASE_DIR / "stats.json"


@dataclass
class RatingInfo:
    """Текущий рейтинг / слава с murim-cards."""

    username: str = ""
    rank: str = ""          # например «БРОНЗА II»
    glory: Optional[int] = None
    glory_to_next: Optional[int] = None
    power: Optional[int] = None
    win_streak: Optional[int] = None
    updated_at: str = ""

    def to_telegram(self) -> str:
        lines = ["<b>📈 Текущий рейтинг</b>", ""]
        if self.username:
            lines.append(f"👤 Игрок: <b>{_esc(self.username)}</b>")
        if self.rank:
            lines.append(f"🏅 Ранг: <b>{_esc(self.rank)}</b>")
        if self.glory is not None:
            if self.glory_to_next:
                lines.append(f"⭐ Слава: <b>{self.glory}</b> / {self.glory_to_next}")
            else:
                lines.append(f"⭐ Слава: <b>{self.glory}</b>")
        if self.power is not None:
            lines.append(f"💪 Сила отряда: <b>{self.power}</b>")
        if self.win_streak is not None:
            lines.append(f"🔥 Серия побед: <b>{self.win_streak}</b>")
        if self.updated_at:
            lines.append(f"🕒 Обновлено: {self.updated_at}")
        if len(lines) <= 2:
            lines.append("Нет данных. Сыграйте бой или нажмите «Обновить рейтинг».")
        return "\n".join(lines)


@dataclass
class BattleStats:
    """Накопленная статистика за всё время работы бота."""

    total_battles: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    skipped: int = 0
    errors: int = 0
    glory_gained: int = 0
    glory_lost: int = 0
    best_win_streak: int = 0
    current_win_streak: int = 0
    current_lose_streak: int = 0
    started_at: str = field(default_factory=lambda: datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
    last_battle_at: str = ""
    last_outcome: str = ""
    # Последние N кратких записей
    recent: List[str] = field(default_factory=list)
    rating: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BattleStats":
        known = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in data.items() if k in known}
        return cls(**cleaned)

    def winrate(self) -> float:
        decided = self.wins + self.losses + self.draws
        if decided <= 0:
            return 0.0
        return 100.0 * self.wins / decided

    def register(
        self,
        outcome: str,
        rating_change: Optional[str] = None,
        summary: str = "",
    ) -> None:
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.last_battle_at = now
        self.last_outcome = outcome

        if outcome == "skipped":
            self.skipped += 1
        elif outcome == "error":
            self.errors += 1
        else:
            self.total_battles += 1
            if outcome == "win":
                self.wins += 1
                self.current_win_streak += 1
                self.current_lose_streak = 0
                self.best_win_streak = max(self.best_win_streak, self.current_win_streak)
            elif outcome == "lose":
                self.losses += 1
                self.current_lose_streak += 1
                self.current_win_streak = 0
            elif outcome == "draw":
                self.draws += 1
                self.current_win_streak = 0
                self.current_lose_streak = 0

        delta = _parse_signed_int(rating_change)
        if delta is not None:
            if delta >= 0:
                self.glory_gained += delta
            else:
                self.glory_lost += abs(delta)

        if summary:
            self.recent.insert(0, f"{now} · {summary}")
            self.recent = self.recent[:15]

    def to_telegram(self, session_extra: str = "") -> str:
        net = self.glory_gained - self.glory_lost
        net_s = f"+{net}" if net >= 0 else str(net)
        lines = [
            "<b>📊 Статистика</b>",
            "",
            f"⚔️ Боёв: <b>{self.total_battles}</b>",
            f"🏆 Победы: <b>{self.wins}</b>",
            f"💀 Поражения: <b>{self.losses}</b>",
            f"🤝 Ничьи: <b>{self.draws}</b>",
            f"📈 Винрейт: <b>{self.winrate():.1f}%</b>",
            f"⏸ Пропуски: {self.skipped}",
            f"⚠️ Ошибки: {self.errors}",
            "",
            f"⭐ Слава: получено <b>+{self.glory_gained}</b> / потеряно <b>−{self.glory_lost}</b>",
            f"📐 Итого слава за сессии бота: <b>{net_s}</b>",
            f"🔥 Текущая серия побед: <b>{self.current_win_streak}</b>",
            f"💎 Лучшая серия: <b>{self.best_win_streak}</b>",
            f"❄️ Серия поражений: <b>{self.current_lose_streak}</b>",
        ]
        if self.started_at:
            lines.append(f"🗓 Учёт с: {self.started_at}")
        if self.last_battle_at:
            lines.append(f"🕒 Последний бой: {self.last_battle_at} ({self.last_outcome})")
        if session_extra:
            lines.append("")
            lines.append(session_extra)
        if self.recent:
            lines.append("")
            lines.append("<b>Последние бои:</b>")
            for item in self.recent[:8]:
                lines.append(f"• <code>{_esc(item)}</code>")
        return "\n".join(lines)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _parse_signed_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    import re

    m = re.search(r"([+\-−–]?)\s*(\d+)", value)
    if not m:
        return None
    sign = m.group(1)
    num = int(m.group(2))
    if sign in {"-", "−", "–"}:
        return -num
    return num


def load_stats() -> BattleStats:
    if not STATS_PATH.exists():
        return BattleStats()
    try:
        raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return BattleStats()
        return BattleStats.from_dict(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось прочитать stats.json: %s", exc)
        return BattleStats()


def save_stats(stats: BattleStats) -> None:
    tmp = STATS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(stats.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STATS_PATH)


def update_stats_from_result(
    outcome: str,
    rating_change: Optional[str] = None,
    summary: str = "",
    rating: Optional[RatingInfo] = None,
) -> BattleStats:
    stats = load_stats()
    stats.register(outcome, rating_change=rating_change, summary=summary)
    if rating is not None:
        stats.rating = asdict(rating)
    save_stats(stats)
    return stats


def get_cached_rating() -> RatingInfo:
    stats = load_stats()
    if not stats.rating:
        return RatingInfo()
    try:
        return RatingInfo(**{
            k: v for k, v in stats.rating.items()
            if k in {f.name for f in fields(RatingInfo)}
        })
    except Exception:  # noqa: BLE001
        return RatingInfo()
