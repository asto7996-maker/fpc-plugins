"""
Knowledge adapted from YouGame thread «GameBots - Бот для dwar»
https://yougame.biz/threads/184351/

The thread discusses a cracked map helper «Оповещатор v8» (edward_freedoms)
for Легенда: Наследие Драконов. We do NOT ship cracks or binaries — only the
publicly described UX semantics useful for our HTTP/WS bot:

  • hide / skip occupied resource & hunt targets
  • hide clutter (monsters while gathering — documented for future gather)
  • notify on gather / fail / puzzle / appear events
  • declutter: prefer free targets, log free vs busy counts
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

GAMEBOTS_THREAD = "https://yougame.biz/threads/184351/"
GAMEBOTS_PRODUCT = "Оповещатор v8 (edward_freedoms) / GameBots"
GAMEBOTS_AUTHOR_NOTE = "xCult thread, 28 Jan 2021 — features described by users"


@dataclass(frozen=True)
class GamebotsToggle:
    """UI checkbox from the Оповещатор screenshot in the thread."""
    key: str
    label_ru: str
    default: bool = False
    applies_to: str = "gather"  # gather | hunt | both | notify


# From attachment UI (thread post #1 screenshot)
GAMEBOTS_TOGGLES: tuple[GamebotsToggle, ...] = (
    GamebotsToggle("hide_occupied", "Скрывать занятые", True, "both"),
    GamebotsToggle("hide_monsters", "Скрыть монстров", False, "gather"),
    GamebotsToggle("change_map", "Изменить карту", False, "gather"),
    GamebotsToggle("sound_gather", "Звук при добыче", True, "notify"),
    GamebotsToggle("sound_fail", "Звук при срыве", True, "notify"),
    GamebotsToggle("sound_puzzle", "Звук при пазле", True, "notify"),
    GamebotsToggle("sound_appear", "Звук при появлении", False, "notify"),
)


@dataclass
class GamebotsDefaults:
    """Semantic defaults derived from GameBots / Оповещатор v8."""
    skip_occupied: bool = True
    skip_hidden: bool = True
    # When attack returns «занят» — try next free target this many times
    occupied_retry: int = 2
    # Log free/busy split like a decluttered map view
    log_target_split: bool = True
    # Flash UI starting coords from screenshot (not used by HTTP client)
    ui_start_x: int = 20
    ui_start_y: int = 50


GAMEBOTS_DEFAULTS = GamebotsDefaults()

# Sound → Telegram notify category hints (main.py notify categories)
NOTIFY_EVENT_CATEGORIES: dict[str, str] = {
    "gather": "gather",
    "fail": "gather_fail",
    "puzzle": "puzzle",
    "appear": "world",
    "occupied": "farm",
}


def is_occupied_target(item: dict[str, Any]) -> bool:
    """True if hunt bot / node is already engaged (fight_id != 0)."""
    fid = str(item.get("fight_id", "0") or "0").strip()
    return fid not in ("0", "", "None", "none")


def is_hidden_target(item: dict[str, Any]) -> bool:
    return str(item.get("hidden", "0") or "0").lower() in ("1", "true", "yes")


def filter_hunt_targets(
    bots: Sequence[dict[str, Any]],
    *,
    skip_occupied: bool = True,
    skip_hidden: bool = True,
) -> list[dict[str, Any]]:
    """
    GameBots «Скрывать занятые» (+ skip hidden) for hunt_farm bots.
    Returns free targets only when skip_occupied is True.
    """
    out: list[dict[str, Any]] = []
    for b in bots or []:
        if not isinstance(b, dict):
            continue
        if skip_hidden and is_hidden_target(b):
            continue
        if skip_occupied and is_occupied_target(b):
            continue
        out.append(b)
    return out


def split_free_busy(
    bots: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    free: list[dict[str, Any]] = []
    busy: list[dict[str, Any]] = []
    for b in bots or []:
        if not isinstance(b, dict):
            continue
        if is_hidden_target(b):
            continue
        (busy if is_occupied_target(b) else free).append(b)
    return free, busy


def summarize_targets(bots: Sequence[dict[str, Any]]) -> str:
    free, busy = split_free_busy(bots)
    return f"free={len(free)} busy={len(busy)} total={len(free) + len(busy)}"


def error_looks_occupied(err: str) -> bool:
    """Match server texts that mean the target is already taken."""
    e = (err or "").lower()
    needles = (
        "занят",
        "занята",
        "занято",
        "уже в бою",
        "уже сражается",
        "в бою с другим",
        "недоступен",
        "кто-то другой",
    )
    return any(n in e for n in needles)


def catalog_dict() -> dict[str, Any]:
    return {
        "source": GAMEBOTS_THREAD,
        "product": GAMEBOTS_PRODUCT,
        "note": GAMEBOTS_AUTHOR_NOTE,
        "fetched_at": time.time(),
        "defaults": asdict(GAMEBOTS_DEFAULTS),
        "toggles": [asdict(t) for t in GAMEBOTS_TOGGLES],
        "notify_categories": dict(NOTIFY_EVENT_CATEGORIES),
        "user_described_features": [
            "убрать лишнее с карты",
            "собрать ресурсы визуально в одном месте",
            "убрать уже занятые ресурсы",
            "фильтр монстров на карте сбора",
            "звуки: добыча / срыв / пазл / появление",
        ],
        "integration": {
            "hunt": "filter_hunt_targets + occupied_retry on ATTACK_BOT",
            "gather": "documented for future profession gather module",
            "no_crack": True,
        },
    }


def save_catalog(path: Optional[Path] = None) -> Path:
    target = Path(path) if path else (
        Path(__file__).resolve().parents[1] / "data" / "gamebots_catalog.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(catalog_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def load_catalog(path: Optional[Path] = None) -> dict[str, Any]:
    target = Path(path) if path else (
        Path(__file__).resolve().parents[1] / "data" / "gamebots_catalog.json"
    )
    if target.is_file():
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("gamebots catalog: %s", exc)
    return catalog_dict()
