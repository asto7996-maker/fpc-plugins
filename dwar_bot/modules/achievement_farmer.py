"""
Achievement / medal farmer for «Легенда: Наследие Драконов».

Tracks known achievement families and nudges the planner toward the right
areas, mobs, quests and kill counts. Persists progress in state.json under
``achievements``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AchievementDef:
    key: str
    title: str
    category: str  # hunt | quest | explore | craft | social | elite
    min_level: int = 1
    target_kills: int = 0
    mob_keywords: tuple[str, ...] = ()
    area_ids: tuple[str, ...] = ()
    npc_keywords: tuple[str, ...] = ()
    quest_keywords: tuple[str, ...] = ()
    priority: int = 50
    note: str = ""


# Curated starter set — enough to drive early/mid MaxFarm toward medals.
ACHIEVEMENT_CATALOG: tuple[AchievementDef, ...] = (
    AchievementDef(
        "first_blood", "Первая кровь", "hunt",
        min_level=1, target_kills=1,
        mob_keywords=("крэтс", "паук", "зигред"),
        area_ids=("930", "931", "932", "192", "227"),
        priority=90, note="первый убитый моб",
    ),
    AchievementDef(
        "leave_village", "За порог деревни", "explore",
        min_level=2, target_kills=0,
        area_ids=("192", "191"),
        npc_keywords=("торгор", "вожд"),
        quest_keywords=("ранен", "ополчен", "сопк"),
        priority=95, note="выйти в Дымные сопки",
    ),
    AchievementDef(
        "spider_hunter_10", "Охотник на пауков I", "hunt",
        min_level=3, target_kills=10,
        mob_keywords=("паук", "spider", "ядохв"),
        area_ids=("227", "226", "228", "192"),
        priority=80,
    ),
    AchievementDef(
        "spider_hunter_50", "Охотник на пауков II", "hunt",
        min_level=3, target_kills=50,
        mob_keywords=("паук", "spider"),
        area_ids=("227", "226", "228"),
        priority=75,
    ),
    AchievementDef(
        "spider_hunter_200", "Охотник на пауков III", "hunt",
        min_level=4, target_kills=200,
        mob_keywords=("паук", "spider"),
        area_ids=("227", "226", "228"),
        priority=70,
    ),
    AchievementDef(
        "zigred_slayer_10", "Гроза Зигреда I", "hunt",
        min_level=3, target_kills=10,
        mob_keywords=("зигред", "zigred"),
        area_ids=("159",),
        priority=78,
    ),
    AchievementDef(
        "zigred_slayer_50", "Гроза Зигреда II", "hunt",
        min_level=4, target_kills=50,
        mob_keywords=("зигред",),
        area_ids=("159",),
        priority=72,
    ),
    AchievementDef(
        "cretas_no_more", "Долой Крэтсов", "hunt",
        min_level=2, target_kills=25,
        mob_keywords=("крэтс",),
        area_ids=("930", "931", "932"),
        priority=40, note="не фармить после выхода",
    ),
    AchievementDef(
        "loot_bags_10", "Расхититель сумок I", "craft",
        min_level=1, target_kills=0,
        quest_keywords=("набор", "сундук", "поручен"),
        priority=60, note="открывать наборы/пленённых",
    ),
    AchievementDef(
        "quest_story_flash", "Сюжет: раненые", "quest",
        min_level=2, target_kills=0,
        npc_keywords=("торгор", "вожд", "флэш", "flash"),
        quest_keywords=("ранен", "излечен", "снадоб"),
        area_ids=("932", "930", "931"),
        priority=88,
    ),
    AchievementDef(
        "arena_rookie", "Арена: новичок", "hunt",
        min_level=5, target_kills=5,
        area_ids=(),
        quest_keywords=("арен",),
        priority=55,
    ),
    AchievementDef(
        "front_fighter_10", "Фронтовик I", "hunt",
        min_level=6, target_kills=10,
        quest_keywords=("фронт",),
        priority=58,
    ),
    AchievementDef(
        "gold_hoarder_100", "Копилка I", "explore",
        min_level=3, target_kills=0,
        priority=45, note="накопить 100 зол.",
    ),
    AchievementDef(
        "gold_hoarder_500", "Копилка II", "explore",
        min_level=5, target_kills=0,
        priority=42, note="накопить 500 зол.",
    ),
    AchievementDef(
        "level_5", "Пятый уровень", "explore",
        min_level=1, target_kills=0,
        priority=85, note="достичь Lv5",
    ),
    AchievementDef(
        "level_10", "Десятый уровень", "explore",
        min_level=1, target_kills=0,
        priority=80, note="достичь Lv10",
    ),
    AchievementDef(
        "level_20", "Двадцатый уровень", "explore",
        min_level=1, target_kills=0,
        priority=70, note="достичь Lv20",
    ),
    AchievementDef(
        "potion_user_25", "Алхимик-практик", "craft",
        min_level=1, target_kills=0,
        priority=50, note="выпить 25 зелий",
    ),
    AchievementDef(
        "unbroken_streak_20", "Серия побед", "hunt",
        min_level=3, target_kills=20,
        priority=65, note="20 побед подряд без смерти",
    ),
)


@dataclass
class AchievementProgress:
    key: str
    kills: int = 0
    done: bool = False
    done_at: float = 0.0
    note: str = ""


@dataclass
class AchievementState:
    progress: dict[str, AchievementProgress] = field(default_factory=dict)
    total_kills: int = 0
    potions_drunk: int = 0
    bag_opens: int = 0
    max_win_streak: int = 0
    current_win_streak: int = 0
    max_gold_seen: float = 0.0
    areas_visited: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "progress": {k: asdict(v) for k, v in self.progress.items()},
            "total_kills": self.total_kills,
            "potions_drunk": self.potions_drunk,
            "bag_opens": self.bag_opens,
            "max_win_streak": self.max_win_streak,
            "current_win_streak": self.current_win_streak,
            "max_gold_seen": self.max_gold_seen,
            "areas_visited": list(self.areas_visited),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AchievementState":
        prog: dict[str, AchievementProgress] = {}
        raw = data.get("progress") or {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    prog[str(k)] = AchievementProgress(
                        key=str(v.get("key") or k),
                        kills=int(v.get("kills") or 0),
                        done=bool(v.get("done")),
                        done_at=float(v.get("done_at") or 0),
                        note=str(v.get("note") or ""),
                    )
        return cls(
            progress=prog,
            total_kills=int(data.get("total_kills") or 0),
            potions_drunk=int(data.get("potions_drunk") or 0),
            bag_opens=int(data.get("bag_opens") or 0),
            max_win_streak=int(data.get("max_win_streak") or 0),
            current_win_streak=int(data.get("current_win_streak") or 0),
            max_gold_seen=float(data.get("max_gold_seen") or 0),
            areas_visited=[str(x) for x in (data.get("areas_visited") or [])],
            updated_at=float(data.get("updated_at") or time.time()),
        )


@dataclass
class AchievementGoal:
    key: str
    title: str
    priority: int
    mob_keywords: tuple[str, ...] = ()
    area_ids: tuple[str, ...] = ()
    npc_keywords: tuple[str, ...] = ()
    quest_keywords: tuple[str, ...] = ()
    remaining_kills: int = 0
    reason: str = ""


class AchievementFarmer:
    """Tracks medals and suggests next farm goal."""

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._path = Path(state_path) if state_path else None
        self.state = AchievementState()
        if self._path:
            self.load(self._path)

    def load(self, path: Optional[Path] = None) -> None:
        target = Path(path) if path else self._path
        if not target or not target.exists():
            return
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            raw = data.get("achievements") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                self.state = AchievementState.from_dict(raw)
                self._path = target
        except Exception as exc:
            logger.debug("AchievementFarmer load: %s", exc)

    def save(self, path: Optional[Path] = None) -> None:
        target = Path(path) if path else self._path
        if not target:
            return
        try:
            existing: dict[str, Any] = {}
            if target.exists():
                existing = json.loads(target.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            self.state.updated_at = time.time()
            existing["achievements"] = self.state.to_dict()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("AchievementFarmer save: %s", exc)

    def _prog(self, key: str) -> AchievementProgress:
        if key not in self.state.progress:
            self.state.progress[key] = AchievementProgress(key=key)
        return self.state.progress[key]

    def _mark_done(self, key: str, note: str = "") -> None:
        p = self._prog(key)
        if p.done:
            return
        p.done = True
        p.done_at = time.time()
        p.note = note or p.note
        logger.info("Achievement DONE: %s (%s)", key, note)

    def note_kill(self, mob: str = "", area_id: str = "", level: int = 1) -> list[str]:
        """Record a win; return newly completed achievement keys."""
        self.state.total_kills += 1
        self.state.current_win_streak += 1
        self.state.max_win_streak = max(
            self.state.max_win_streak, self.state.current_win_streak
        )
        area = str(area_id or "")
        if area and area not in self.state.areas_visited:
            self.state.areas_visited.append(area)

        done_now: list[str] = []
        mob_l = (mob or "").lower()
        for ad in ACHIEVEMENT_CATALOG:
            if ad.category != "hunt" and ad.target_kills <= 0:
                continue
            if int(level or 1) < ad.min_level:
                continue
            p = self._prog(ad.key)
            if p.done:
                continue
            match = False
            if ad.mob_keywords:
                match = any(k in mob_l for k in ad.mob_keywords) if mob_l else False
                if not match and not mob_l and ad.area_ids and area in ad.area_ids:
                    match = True
            elif ad.area_ids:
                match = area in ad.area_ids
            else:
                match = ad.target_kills > 0  # generic kill counter
            if not match and ad.key == "unbroken_streak_20":
                p.kills = self.state.current_win_streak
                if p.kills >= ad.target_kills:
                    self._mark_done(ad.key, f"streak={p.kills}")
                    done_now.append(ad.key)
                continue
            if match:
                p.kills += 1
                if ad.target_kills and p.kills >= ad.target_kills:
                    self._mark_done(ad.key, f"kills={p.kills} mob={mob}")
                    done_now.append(ad.key)
        self.save()
        return done_now

    def note_death(self) -> None:
        self.state.current_win_streak = 0
        self.save()

    def note_potion(self) -> None:
        self.state.potions_drunk += 1
        p = self._prog("potion_user_25")
        if not p.done and self.state.potions_drunk >= 25:
            self._mark_done("potion_user_25", f"drunk={self.state.potions_drunk}")
        self.save()

    def note_bag_open(self, n: int = 1) -> None:
        self.state.bag_opens += int(n or 0)
        p = self._prog("loot_bags_10")
        if not p.done and self.state.bag_opens >= 10:
            self._mark_done("loot_bags_10", f"opens={self.state.bag_opens}")
        self.save()

    def note_area(self, area_id: str) -> None:
        area = str(area_id or "")
        if not area:
            return
        if area not in self.state.areas_visited:
            self.state.areas_visited.append(area)
        if area in {"192", "191", "227", "226", "159"}:
            self._mark_done("leave_village", f"area={area}")
        self.save()

    def note_money_level(self, money: float, level: int) -> list[str]:
        done_now: list[str] = []
        self.state.max_gold_seen = max(self.state.max_gold_seen, float(money or 0))
        checks = (
            ("gold_hoarder_100", 100.0),
            ("gold_hoarder_500", 500.0),
            ("level_5", 5),
            ("level_10", 10),
            ("level_20", 20),
        )
        for key, need in checks:
            p = self._prog(key)
            if p.done:
                continue
            if key.startswith("gold_") and self.state.max_gold_seen >= float(need):
                self._mark_done(key, f"gold={self.state.max_gold_seen:.2f}")
                done_now.append(key)
            elif key.startswith("level_") and int(level or 0) >= int(need):
                self._mark_done(key, f"level={level}")
                done_now.append(key)
        if done_now:
            self.save()
        return done_now

    def next_goals(
        self,
        *,
        level: int,
        area_id: str = "",
        limit: int = 5,
    ) -> list[AchievementGoal]:
        goals: list[AchievementGoal] = []
        lv = int(level or 1)
        for ad in ACHIEVEMENT_CATALOG:
            if lv < ad.min_level:
                continue
            p = self._prog(ad.key)
            if p.done:
                continue
            # Skip village Cretas grind once left
            if ad.key == "cretas_no_more" and str(area_id) not in {"930", "931", "932"}:
                continue
            remaining = max(0, int(ad.target_kills) - int(p.kills))
            goals.append(
                AchievementGoal(
                    key=ad.key,
                    title=ad.title,
                    priority=ad.priority,
                    mob_keywords=ad.mob_keywords,
                    area_ids=ad.area_ids,
                    npc_keywords=ad.npc_keywords,
                    quest_keywords=ad.quest_keywords,
                    remaining_kills=remaining,
                    reason=ad.note or ad.category,
                )
            )
        goals.sort(key=lambda g: (-g.priority, g.remaining_kills))
        return goals[: max(1, int(limit))]

    def preferred_mob_for_level(self, level: int, area_id: str = "") -> str:
        goals = self.next_goals(level=level, area_id=area_id, limit=8)
        area = str(area_id or "")
        for g in goals:
            if g.mob_keywords:
                if not g.area_ids or not area or area in g.area_ids:
                    return g.mob_keywords[0]
        return ""

    def preferred_area_for_level(self, level: int) -> str:
        goals = self.next_goals(level=level, limit=8)
        for g in goals:
            if g.area_ids:
                return g.area_ids[0]
        return ""

    def summary_lines(self, *, level: int = 1, limit: int = 8) -> list[str]:
        done = sum(1 for p in self.state.progress.values() if p.done)
        total = len(ACHIEVEMENT_CATALOG)
        lines = [
            f"🏅 Ачивки: <b>{done}/{total}</b> · убийств {self.state.total_kills} · "
            f"серия {self.state.current_win_streak} (макс {self.state.max_win_streak})",
            f"🧪 Зелий: {self.state.potions_drunk} · 🎁 сумок: {self.state.bag_opens}",
        ]
        goals = self.next_goals(level=level, limit=limit)
        if goals:
            lines.append("<b>Следующие цели:</b>")
            for g in goals:
                rem = f" · осталось {g.remaining_kills}" if g.remaining_kills else ""
                lines.append(f"• {g.title}{rem} ({g.reason or g.key})")
        return lines

    def telegram_html(self, *, level: int = 1) -> str:
        return "\n".join(self.summary_lines(level=level))
