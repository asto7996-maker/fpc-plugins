"""
Knowledge & combat defaults adapted from SUIS «Бот для Легенды»
(https://dwar.browsergamebots.com/ — screenshots + feature list).

Not a reverse-engineer of the closed binary: values come from the public
settings UI shown on the site (Боевая сессия / Сбор ресурсов / …).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from dwar_bot.modules.battle_strategy import (
    BOTTOM_ATTACK_ID,
    MIDDLE_ATTACK_ID,
    TOP_ATTACK_ID,
    ZONE_NAME,
    parse_hit_list,
)

logger = logging.getLogger(__name__)

SUIS_HOME = "https://dwar.browsergamebots.com/"
SUIS_SCREENSHOTS = "https://dwar.browsergamebots.com/screenshots/"

# ---------------------------------------------------------------------------
# Mob catalog (from «Боевая сессия → Основные настройки» screenshot)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SuisMob:
    level: int
    name: str
    hunt_default: bool = False  # in default hunt list on screenshot


# Visible rows from «Боевая сессия → Основные настройки» screenshot (4.png)
SUIS_MOBS: tuple[SuisMob, ...] = (
    SuisMob(1, "Крэтс", hunt_default=True),
    SuisMob(1, "Шальной пес", hunt_default=True),
    SuisMob(2, "Бешеный пес", hunt_default=True),
    SuisMob(2, "Дряхлый скелет"),
    SuisMob(2, "Зигред", hunt_default=True),
    SuisMob(2, "Крэтс-лидер"),
    SuisMob(2, "Огненный паук"),
    SuisMob(3, "Зигред-воин", hunt_default=True),
    SuisMob(3, "Крэтс-вожак", hunt_default=True),
    SuisMob(3, "Неистовый пес", hunt_default=True),
    SuisMob(3, "Огненная паучиха"),
    SuisMob(3, "Пепельный паук"),
    SuisMob(3, "Пес-демон"),
    SuisMob(3, "Скелет-воин"),
)

# ---------------------------------------------------------------------------
# Gathering resources (from «Сбор ресурсов» screenshot — Геолог)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SuisResource:
    skill: int
    name: str
    profession: str = "geologist"  # geologist | herbalist | fisher


SUIS_RESOURCES: tuple[SuisResource, ...] = (
    SuisResource(0, "Агат"),
    SuisResource(0, "Бирюза"),
    SuisResource(0, "Аквамарин"),
    SuisResource(0, "Огневик"),
    SuisResource(30, "Аметист"),
    SuisResource(30, "Обсидиан"),
    SuisResource(30, "Сапфир"),
    SuisResource(60, "Топаз"),
    SuisResource(60, "Изумруд"),
    SuisResource(60, "Рубин"),
    SuisResource(90, "Алмаз"),
    SuisResource(120, "Эльдорилл"),
    SuisResource(120, "Веридий"),
    SuisResource(120, "Огненный сердолик"),
    SuisResource(120, "Янтарь"),
    SuisResource(150, "Драконова кровь"),
)

# Post-battle food ladder (Еда после боя screenshot)
SUIS_FOOD_LADDER: tuple[tuple[float, str, int], ...] = (
    # (min_hp_percent_to_use, food_name, approx_heal)
    (75.0, "Сдобная булочка", 60),
    (50.0, "Груша", 110),
    (25.0, "Фелинойская вобла", 140),
    (1.0, "Огненный лещ", 210),
)


@dataclass
class SuisCombatDefaults:
    """
    Defaults from «Дополнительные настройки» / session limits.

    Mapped onto our CombatConfig / FightBrain where applicable.
    """
    # Elixirs / scrolls from belt
    hp_elixir_percent: float = 20.0
    hp_scroll_percent: float = 40.0
    # Skip heal if mob nearly dead
    skip_heal_mob_hp_percent: float = 5.0
    # Block thresholds when «Использовать Блок» enabled
    hp_block_enter: float = 30.0
    hp_block_exit: float = 60.0
    # Wait for HP after fight before next pull
    post_battle_hp_wait: float = 100.0
    # Don't eat if already above this
    food_skip_above_hp: float = 75.0
    # Session limits
    session_minutes: int = 60
    session_kill_limit: int = 50
    # Error budget (Системные → Разное)
    max_error_ops: int = 25
    max_failed_ops: int = 100
    # Human-like hit delay (seconds)
    hit_delay_min: float = 0.5
    hit_delay_max: float = 5.0
    # Fight load stall refresh
    fight_load_refresh_sec: float = 10.0
    # Summon pet if attackers > N (DwarBOT had similar)
    pet_if_enemies_gt: int = 1
    # After elixir/scroll — pause N turns (approx → seconds in our client)
    pause_turns_after_elixir: int = 3
    pause_turns_after_scroll: int = 6


SUIS_DEFAULTS = SuisCombatDefaults()


@dataclass
class SuisGatherDefaults:
    """«Сбор ресурсов → Разное» + splinter reaction defaults."""
    session_minutes: int = 60
    session_resource_limit: int = 50
    gather_process_timeout: int = 40
    max_people_on_node: int = 1
    strategy: str = "round_robin"  # priority | round_robin | random
    zones: tuple[str, ...] = ("N", "S", "W", "E")
    # заноза
    splinter_chat_interval_min: int = 5
    splinter_retries_per_loc: int = 3
    splinter_max_requests: int = 15


SUIS_GATHER = SuisGatherDefaults()


@dataclass
class SuisOperatorDefaults:
    """«Эмуляция оператора → Сбор ресурсов и бои»."""
    pause_every_min_minutes: int = 10
    pause_every_max_minutes: int = 20
    pause_for_min_minutes: int = 3
    pause_for_max_minutes: int = 5
    pause_every_min_ops: int = 30
    pause_every_max_ops: int = 50
    pause_ops_for_min_minutes: int = 3
    pause_ops_for_max_minutes: int = 5
    human_see_delay: bool = True


SUIS_OPERATOR = SuisOperatorDefaults()

# Example formulas from the UI
SUIS_EXAMPLE_SIMPLE = "Б+ГНБ-Г"
SUIS_EXAMPLE_ADVANCED = "ГН2Т"
# Newbie-friendly physical cycle without forced block (Т/Г/Н)
SUIS_DEFAULT_PHYSICAL = "ГНТНГ"


def default_suis_sequence(level: int = 1) -> str:
    """
    Auto formula when config.suis_sequence is empty.
    Low levels: physical head-legs-body cycle.
    Higher: strip block markers from the simple UI example → ГНГ.
    """
    lvl = int(level or 1)
    if lvl <= 10:
        return SUIS_DEFAULT_PHYSICAL
    stripped = (
        SUIS_EXAMPLE_SIMPLE.replace("Б+", "").replace("Б-", "").replace("б+", "").replace("б-", "")
    )
    return stripped or SUIS_DEFAULT_PHYSICAL


# ---------------------------------------------------------------------------
# SUIS fight formula parser (Т/Г/Н/Р/Б+/Б-/1-6/П1-П9)
# ---------------------------------------------------------------------------

_ZONE = {
    "Т": MIDDLE_ATTACK_ID,  # тело
    "т": MIDDLE_ATTACK_ID,
    "Г": TOP_ATTACK_ID,     # голова
    "г": TOP_ATTACK_ID,
    "Н": BOTTOM_ATTACK_ID,  # ноги
    "н": BOTTOM_ATTACK_ID,
}


@dataclass
class SuisStep:
    kind: str  # hit | block_on | block_off | slot | pause | random_hit
    zone: int = MIDDLE_ATTACK_ID
    slot: int = 0
    pause_sec: float = 0.0


def parse_suis_sequence(raw: str) -> list[SuisStep]:
    """
    Parse SUIS battle string.

    Codes (from screenshots «Ведение боя»):
      Т тело, Г голова, Н ноги, Р случайный удар,
      Б+ вход в блок, Б- выход из блока,
      1..6 слот пояса, П1..П9 пауза в секундах.
    Spaces ignored. Example: «Б+ГНБ-Г», «ГН2Т».
    """
    s = re.sub(r"\s+", "", str(raw or ""))
    if not s:
        return []
    steps: list[SuisStep] = []
    i = 0
    upper = s  # keep case for Б+/Б-
    while i < len(upper):
        ch = upper[i]
        # Б+ / Б-
        if ch in ("Б", "б") and i + 1 < len(upper) and upper[i + 1] in ("+", "-"):
            steps.append(SuisStep(
                kind="block_on" if upper[i + 1] == "+" else "block_off",
            ))
            i += 2
            continue
        # П1..П9
        if ch in ("П", "п") and i + 1 < len(upper) and upper[i + 1].isdigit():
            sec = int(upper[i + 1])
            steps.append(SuisStep(kind="pause", pause_sec=float(sec)))
            i += 2
            continue
        # belt slot 1..6
        if ch.isdigit() and ch != "0":
            steps.append(SuisStep(kind="slot", slot=int(ch)))
            i += 1
            continue
        # zones / random
        if ch in ("Р", "р"):
            steps.append(SuisStep(kind="random_hit"))
            i += 1
            continue
        if ch in _ZONE:
            steps.append(SuisStep(kind="hit", zone=_ZONE[ch]))
            i += 1
            continue
        logger.debug("SUIS seq: skip unknown %r at %d in %r", ch, i, raw)
        i += 1
    return steps


def suis_sequence_to_hit_list(raw: str) -> list[int]:
    """Extract only hit zones (for FightBrain.hit_seq), expanding Р randomly once."""
    zones: list[int] = []
    for step in parse_suis_sequence(raw):
        if step.kind == "hit":
            zones.append(int(step.zone))
        elif step.kind == "random_hit":
            zones.append(random.choice(
                [TOP_ATTACK_ID, MIDDLE_ATTACK_ID, BOTTOM_ATTACK_ID]
            ))
    return zones or parse_hit_list(None)


def expand_suis_cycle(raw: str, *, hits_needed: int = 12) -> list[int]:
    """Repeat SUIS hit pattern until we have enough zones for a long fight."""
    base = suis_sequence_to_hit_list(raw)
    if not base:
        return parse_hit_list(None)
    out: list[int] = []
    while len(out) < hits_needed:
        out.extend(base)
    return out[:hits_needed]


# ---------------------------------------------------------------------------
# Helpers for hunt / knowledge
# ---------------------------------------------------------------------------

def hunt_names_for_level(level: int, *, pad: int = 1) -> list[str]:
    """
    Mobs for hunt priority at *level*.

    Order: exact-level hunt_default → exact-level others → near band (±pad)
    by distance, then hunt_default, then name. So Lv3 prefers Зигред-воин /
    Крэтс-вожак over leftover Крэтс from the village.
    """
    lvl = max(1, int(level or 1))
    band = [m for m in SUIS_MOBS if abs(m.level - lvl) <= pad]
    if not band:
        band = list(SUIS_MOBS)
    band.sort(
        key=lambda m: (
            0 if m.level == lvl else 1,
            abs(m.level - lvl),
            0 if m.hunt_default else 1,
            m.level,
            m.name,
        )
    )
    return [m.name for m in band]


def default_hunt_mob(level: int = 1) -> str:
    """Best single mob name pin for the current level (SUIS-aware)."""
    names = hunt_names_for_level(level, pad=0)
    if names:
        return names[0]
    names = hunt_names_for_level(level, pad=1)
    return names[0] if names else "Крэтс"


def default_hunt_list() -> list[str]:
    return [m.name for m in SUIS_MOBS if m.hunt_default] or ["Крэтс"]


def flash_farm_open_for_level(level: int, *, min_level: int = 3) -> bool:
    """
    After Lv3+, keep flash heal as a soft side-goal: allow open farm / travel
    instead of locking the village forever on HTTP-impossible medicine.
    """
    return int(level or 1) >= int(min_level)


def is_farm_open(
    world_objective: Optional[dict] = None,
    *,
    level: int = 1,
    min_level: int = 3,
) -> bool:
    """
    Unified farm-open gate for Flash side-quests.

    True when persisted ``farm_open`` is set, or flash_only + level ≥ min_level.
    """
    wo = world_objective or {}
    if not wo:
        return False
    if wo.get("farm_open"):
        return True
    if wo.get("flash_only") and flash_farm_open_for_level(level, min_level=min_level):
        return True
    return False


def resources_for_skill(skill: int, profession: str = "geologist") -> list[str]:
    return [
        r.name for r in SUIS_RESOURCES
        if r.profession == profession and r.skill <= int(skill or 0)
    ]


def food_choice_for_hp(
    hp_percent: float,
    *,
    skip_above: float = 75.0,
) -> Optional[str]:
    """
    SUIS «Еда после боя» ladder (8.png).

    Global: do not eat if HP > skip_above (default 75%).
    Then first matching condition wins (light → heavy):
      HP>75 → Сдобная булочка (only when skip disabled / skip_above>=100)
      HP>50 → Груша
      HP>25 → Фелинойская вобла
      HP>1  → Огненный лещ
    If the chosen food is missing from bag, caller should try next conditions.
    """
    pct = float(hp_percent)
    if pct <= 0:
        return None
    if skip_above > 0 and pct > float(skip_above):
        return None
    for min_hp, name, _heal in SUIS_FOOD_LADDER:
        if pct > min_hp:
            return name
    return SUIS_FOOD_LADDER[-1][1]


def food_ladder_candidates(hp_percent: float, *, skip_above: float = 75.0) -> list[str]:
    """All foods whose condition matches, light→heavy (for bag fallback)."""
    pct = float(hp_percent)
    if pct <= 0:
        return []
    if skip_above > 0 and pct > float(skip_above):
        return []
    names: list[str] = []
    for min_hp, name, _heal in SUIS_FOOD_LADDER:
        if pct > min_hp:
            names.append(name)
    if not names and pct > 0:
        names.append(SUIS_FOOD_LADDER[-1][1])
    return names


def apply_suis_defaults_to_combat_dict() -> dict[str, Any]:
    """Values to merge into CombatConfig / FarmSettings."""
    d = SUIS_DEFAULTS
    return {
        "hp_elixir_threshold": d.hp_elixir_percent,
        "hp_block_threshold": d.hp_block_enter,
        "hp_unblock_threshold": d.hp_block_exit,
        "post_battle_heal": True,
        # keep retreat below elixir
        "hp_retreat_threshold": max(10.0, d.hp_elixir_percent - 5.0),
    }


def catalog_dict() -> dict[str, Any]:
    return {
        "source": SUIS_HOME,
        "screenshots": SUIS_SCREENSHOTS,
        "version_noted": "5.12",
        "fetched_at": time.time(),
        "combat_defaults": asdict(SUIS_DEFAULTS),
        "gather_defaults": asdict(SUIS_GATHER),
        "operator_defaults": asdict(SUIS_OPERATOR),
        "mobs": [asdict(m) for m in SUIS_MOBS],
        "resources": [asdict(r) for r in SUIS_RESOURCES],
        "food_ladder": [
            {"min_hp_percent": a, "name": b, "heal": c}
            for a, b, c in SUIS_FOOD_LADDER
        ],
        "food_skip_above_hp": SUIS_DEFAULTS.food_skip_above_hp,
        "formula_examples": {
            "simple": SUIS_EXAMPLE_SIMPLE,
            "advanced": SUIS_EXAMPLE_ADVANCED,
            "default_physical": SUIS_DEFAULT_PHYSICAL,
            "codes": {
                "Т": "тело/middle",
                "Г": "голова/top",
                "Н": "ноги/bottom",
                "Р": "random",
                "Б+": "block on",
                "Б-": "block off",
                "1-6": "belt slot",
                "П1-П9": "pause seconds",
            },
        },
        "supported_servers_from_site": [
            "official",
            "dwar.top",
            "w.pathofwar.su",
            "dwar.cc",
            "dwar-game.com",
            "dwar2020.com",
            "oldwar24.ru",
            "dwar24.ru",
            "asteriagame.ru",
        ],
        "features_from_site": [
            "auto fights + HP control + elixirs/scrolls",
            "super-hit combinations",
            "gathering geologist/herbalist/fisher",
            "traveler auto-path",
            "operator emulation pauses",
            "post-battle food",
            "splinter (заноза) help requests",
            "resurrection",
            "ICQ / private message alerts",
        ],
    }


def save_catalog(path: Optional[Path] = None) -> Path:
    target = Path(path) if path else (
        Path(__file__).resolve().parents[1] / "data" / "suis_catalog.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(catalog_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def load_catalog(path: Optional[Path] = None) -> dict[str, Any]:
    target = Path(path) if path else (
        Path(__file__).resolve().parents[1] / "data" / "suis_catalog.json"
    )
    if target.is_file():
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("suis catalog: %s", exc)
    return catalog_dict()
