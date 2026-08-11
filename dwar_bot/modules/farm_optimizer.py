"""
Max-farm optimizer — area/mob routes for fastest level + gold on dwar.ru.

Routes are derived from SUIS hunt tables + observed village → open-farm path:
  village 930/931/932 → Дымные сопки 192 → spiders 227/226 → Zigred 159 …
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FarmRoute:
    min_level: int
    max_level: int
    area_ids: tuple[str, ...]
    mob_keywords: tuple[str, ...]
    priority: int = 50
    note: str = ""
    gold_bias: float = 1.0
    exp_bias: float = 1.0
    achievement_tags: tuple[str, ...] = ()


# Ordered best → fallback. priority higher = preferred within level band.
FARM_ROUTES: tuple[FarmRoute, ...] = (
    FarmRoute(
        1, 2, ("930", "931", "932"),
        ("крэтс", "cretas", "крыс"),
        priority=10, note="деревня — только до выхода",
        gold_bias=0.05, exp_bias=0.1,
        achievement_tags=("village_start",),
    ),
    FarmRoute(
        2, 4, ("192", "191"),
        ("паук", "spider", "дымн"),
        priority=80, note="Дымные сопки — первый реальный фарм",
        gold_bias=1.2, exp_bias=1.3,
        achievement_tags=("leave_village", "smoke_hills"),
    ),
    FarmRoute(
        3, 6, ("227", "226", "228"),
        ("паук", "spider", "ядохв"),
        priority=90, note="пауки — лучший Exp/Gold ранний",
        gold_bias=1.4, exp_bias=1.6,
        achievement_tags=("spiders", "hunter_1"),
    ),
    FarmRoute(
        3, 7, ("159",),
        ("зигред", "zigred"),
        priority=85, note="Зигред — золото + ачивка охотника",
        gold_bias=1.5, exp_bias=1.2,
        achievement_tags=("zigred", "hunter_1"),
    ),
    FarmRoute(
        5, 10, ("100", "101", "102", "110"),
        ("волк", "кабан", "бандит", "гоблин"),
        priority=70, note="открытый мир mid",
        gold_bias=1.1, exp_bias=1.4,
        achievement_tags=("open_world", "hunter_2"),
    ),
    FarmRoute(
        8, 15, ("200", "210", "220", "230"),
        ("орк", "тролль", "разбойник", "скелет"),
        priority=75, note="mid-high farm fronts",
        gold_bias=1.3, exp_bias=1.5,
        achievement_tags=("hunter_3", "fronts"),
    ),
    FarmRoute(
        12, 25, ("300", "310", "320", "350"),
        ("элемент", "демон", "дракон", "нежить"),
        priority=80, note="high level grind",
        gold_bias=1.4, exp_bias=1.7,
        achievement_tags=("hunter_4", "elite"),
    ),
    FarmRoute(
        20, 99, ("400", "410", "450", "500"),
        ("босс", "элит", "страж", "древн"),
        priority=60, note="endgame / achievement elites",
        gold_bias=1.6, exp_bias=1.2,
        achievement_tags=("endgame", "achievements"),
    ),
)

VILLAGE_AREAS = frozenset({"930", "931", "932"})
POST_VILLAGE = frozenset({"192", "191", "227", "226", "228", "159"})


def routes_for_level(level: int) -> list[FarmRoute]:
    lv = max(1, int(level or 1))
    matched = [r for r in FARM_ROUTES if r.min_level <= lv <= r.max_level]
    matched.sort(key=lambda r: (-r.priority, -r.exp_bias))
    return matched


def best_route(level: int, *, prefer_gold: bool = False) -> Optional[FarmRoute]:
    routes = routes_for_level(level)
    if not routes:
        return None
    if prefer_gold:
        routes = sorted(routes, key=lambda r: (-r.gold_bias * r.priority, -r.exp_bias))
    return routes[0]


def preferred_areas(level: int) -> list[str]:
    out: list[str] = []
    for r in routes_for_level(level):
        for a in r.area_ids:
            if a not in out:
                out.append(a)
    return out


def preferred_mobs(level: int, area_id: str = "") -> list[str]:
    area = str(area_id or "")
    out: list[str] = []
    for r in routes_for_level(level):
        if area and area not in r.area_ids and area not in VILLAGE_AREAS:
            # Still allow mob keywords if area unknown
            pass
        for m in r.mob_keywords:
            if m not in out:
                out.append(m)
    return out


def should_leave_village(level: int, area_id: str, *, zero_reward: bool) -> bool:
    if str(area_id) not in VILLAGE_AREAS:
        return False
    if int(level or 1) >= 2 and zero_reward:
        return True
    return int(level or 1) >= 3


def max_farm_kill_limit(level: int, *, aggressive: bool = True) -> int:
    """SUIS session kill soft-limit for uninterrupted grind."""
    base = 80 if aggressive else 40
    if int(level or 1) >= 10:
        return max(base, 120)
    if int(level or 1) >= 5:
        return max(base, 100)
    return base


def recommended_hp_thresholds(*, aggressive: bool = True, max_farm: bool = True) -> tuple[float, float]:
    """
    Return (hp_retreat, hp_heal).

    Safer than legacy 10/30 — deaths were the #1 farm killer.
    Aggressive still farms hard but drinks earlier.
    """
    if max_farm and aggressive:
        return 28.0, 55.0
    if max_farm:
        return 32.0, 60.0
    if aggressive:
        return 22.0, 45.0
    return 35.0, 65.0
