"""
BotMek fight presets — adapted from https://botmek.ru/share/
(group «Легенда: Наследие Драконов», sort=statisticsdesc).

BotMek macros are keyboard/mouse chains. We keep their *semantic* combat
rotations (stance → buffs → crit arrow → hit zone), not DIK scan codes.

Top downloads (as of scrape):
  лабиринт, Воин Ракхари [18], верка, Обитель видений, верка кадики
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import quote
from urllib.request import Request, urlopen

from dwar_bot.modules.battle_strategy import (
    BOTTOM_ATTACK_ID,
    MIDDLE_ATTACK_ID,
    TOP_ATTACK_ID,
)

logger = logging.getLogger(__name__)

BOTMEK_SHARE_URL = (
    "https://botmek.ru/share/?group="
    + quote("Легенда: Наследие Драконов")
    + "&sort=statisticsdesc"
)
BOTMEK_SAVE_URL = "https://botmek.ru/share/save.php?file={file_id}"

# Bundled snapshot next to package data / cwd
_DEFAULT_CATALOG_PATHS = (
    Path(__file__).resolve().parents[1] / "data" / "botmek_catalog.json",
    Path("dwar_bot/data/botmek_catalog.json"),
)


@dataclass(frozen=True)
class BotmekPreset:
    """One BotMek share, mapped to HTTP/WS actions."""

    file_id: str
    name: str
    downloads: int = 0
    min_level: int = 1
    max_level: int = 99
    # magic | physical | any — BotMek «вход в бой в магической стойке»
    stance: str = "any"
    # Preferred physical hit zone (верка / Обитель: удар вниз)
    hit_zone: int = BOTTOM_ATTACK_ID
    # Fallback hit sequence when fight|conf has no combos
    hit_seq: tuple[int, ...] = (BOTTOM_ATTACK_ID,)
    # Drink these before ATTACK_BOT (HTTP DRINK)
    prebuff_keywords: tuple[str, ...] = ()
    # Semantic burst steps (for future USE_EFFECT / stance switches)
    burst_steps: tuple[str, ...] = ()
    description: str = ""
    source_url: str = ""


# Curated from live BotMek descriptions + key order (statisticsdesc).
BOTMEK_PRESETS: tuple[BotmekPreset, ...] = (
    BotmekPreset(
        file_id="N5ECHpxFMBL4ZvE",
        name="лабиринт",
        downloads=920,
        min_level=10,
        max_level=99,
        stance="any",
        hit_zone=MIDDLE_ATTACK_ID,
        hit_seq=(MIDDLE_ATTACK_ID,),
        burst_steps=("hotbar_1", "hotbar_2", "veil", "hotbar_3", "confirm"),
        description="Инст «Лабиринт» / заслон — цепочка хоткеев.",
        source_url="https://botmek.ru/share/?file=N5ECHpxFMBL4ZvE",
    ),
    BotmekPreset(
        file_id="4RtlDRw6zHhtNiR",
        name="Воин Ракхари [18]",
        downloads=631,
        min_level=16,
        max_level=22,
        stance="magic",
        hit_zone=MIDDLE_ATTACK_ID,
        hit_seq=(MIDDLE_ATTACK_ID,),
        prebuff_keywords=("мощ", "экстракт"),
        burst_steps=(
            "crit_arrow", "hypnotic", "stance_physical", "veil", "power_extract", "hit",
        ),
        description=(
            "Маг. стойка → H крит-стрела → A гипнотика → Tab физ → "
            "T пелена → 1 экстракт мощи → W удар."
        ),
        source_url="https://botmek.ru/share/?file=4RtlDRw6zHhtNiR",
    ),
    BotmekPreset(
        file_id="nZHEK41sianySmg",
        name="верка",
        downloads=271,
        min_level=14,
        max_level=20,
        stance="magic",
        hit_zone=BOTTOM_ATTACK_ID,
        hit_seq=(BOTTOM_ATTACK_ID,),
        prebuff_keywords=("гнев", "ярость", "радуг"),
        burst_steps=(
            "anger_power", "anger_elixir", "stance_from_veil", "veil", "crit_arrow", "hit_down",
        ),
        description=(
            "Сила гнева + эликсир гнева + пелена + крит-стрела + удар вниз "
            "(Ниферто / one-shot)."
        ),
        source_url="https://botmek.ru/share/?file=nZHEK41sianySmg",
    ),
    BotmekPreset(
        file_id="N6PgPrigd5hBwDk",
        name="Обитель видений",
        downloads=267,
        min_level=15,
        max_level=20,
        stance="magic",
        hit_zone=BOTTOM_ATTACK_ID,
        hit_seq=(BOTTOM_ATTACK_ID,),
        prebuff_keywords=("гнев",),
        burst_steps=(
            "anger_power", "stance_physical", "veil", "crit_arrow", "hit_down",
        ),
        description="Фарм теней 16 lvl: сила гнева → физ → пелена → крит → удар.",
        source_url="https://botmek.ru/share/?file=N6PgPrigd5hBwDk",
    ),
    BotmekPreset(
        file_id="jjiswARy9lNkJ4m",
        name="верка кадики",
        downloads=253,
        min_level=11,
        max_level=14,
        stance="magic",
        hit_zone=BOTTOM_ATTACK_ID,
        hit_seq=(BOTTOM_ATTACK_ID,),
        prebuff_keywords=("гнев", "ярость", "радуг"),
        burst_steps=("anger_power", "anger_elixir", "flame_tongue"),
        description="Кадхас one-shot: сила гнева + эликсир гнева + язык пламени.",
        source_url="https://botmek.ru/share/?file=jjiswARy9lNkJ4m",
    ),
    # Low-level physical farm — BotMek-inspired (удар вниз + DwarBOT combo fill)
    BotmekPreset(
        file_id="local-low-phys",
        name="newbie physical (BotMek-inspired)",
        downloads=0,
        min_level=1,
        max_level=10,
        stance="physical",
        hit_zone=BOTTOM_ATTACK_ID,
        # Mix: open down (BotMek), then DwarBOT-like pattern ending down
        hit_seq=(
            BOTTOM_ATTACK_ID,
            MIDDLE_ATTACK_ID,
            BOTTOM_ATTACK_ID,
            TOP_ATTACK_ID,
            BOTTOM_ATTACK_ID,
        ),
        prebuff_keywords=("гнев", "мощ", "сил"),
        burst_steps=("prebuff", "hit_down"),
        description=(
            "Адаптация паттерна «удар вниз» с botmek.ru для низких уровней; "
            "комбо с сервера всё ещё приоритетнее."
        ),
        source_url=BOTMEK_SHARE_URL,
    ),
)


def select_preset(
    *,
    level: int = 1,
    name_hint: str = "",
    prefer_downloads: bool = True,
) -> BotmekPreset:
    """Pick best preset for character level (downloads desc within band)."""
    lvl = max(1, int(level or 1))
    hint = (name_hint or "").lower().strip()
    if hint:
        for p in BOTMEK_PRESETS:
            if hint in p.name.lower() or hint in p.file_id.lower():
                return p
    band = [p for p in BOTMEK_PRESETS if p.min_level <= lvl <= p.max_level]
    if not band:
        band = list(BOTMEK_PRESETS)
    if prefer_downloads:
        band = sorted(band, key=lambda p: (p.downloads, -p.min_level), reverse=True)
    return band[0]


def botmek_fallback_sequence(preset: BotmekPreset) -> list[int]:
    return list(preset.hit_seq) if preset.hit_seq else [int(preset.hit_zone)]


def parse_botmek_share_html(html: str) -> list[dict[str, Any]]:
    """Parse BotMek share listing HTML into catalog rows."""
    rows: list[dict[str, Any]] = []
    for m in re.finditer(
        r'href="/share/\?file=([A-Za-z0-9]+)"[^>]*>'
        r'.*?<div class="box-cell title"[^>]*title="([^"]*)"[^>]*>([^<]*)</div>'
        r'.*?<div class="box-cell title"[^>]*title="Количество загрузок"[^>]*>(\d+)</div>',
        html,
        re.S,
    ):
        file_id, title_attr, name, downloads = m.groups()
        rows.append({
            "file_id": file_id,
            "name": name.strip(),
            "description": title_attr.strip(),
            "downloads": int(downloads),
            "source_url": f"https://botmek.ru/share/?file={file_id}",
        })
    if rows:
        return rows
    # Fallback: simpler pattern
    for m in re.finditer(r'href="/share/\?file=([A-Za-z0-9]+)"[^>]*>', html):
        fid = m.group(1)
        chunk = html[m.start(): m.start() + 1200]
        name_m = re.search(r'class="box-cell title"[^>]*>([^<]{2,60})</div>', chunk)
        dl_m = re.search(r'Количество загрузок"[^>]*>(\d+)<', chunk)
        title_m = re.search(r'title="([^"]{10,})"', chunk)
        rows.append({
            "file_id": fid,
            "name": (name_m.group(1).strip() if name_m else fid),
            "description": (title_m.group(1).strip() if title_m else ""),
            "downloads": int(dl_m.group(1)) if dl_m else 0,
            "source_url": f"https://botmek.ru/share/?file={fid}",
        })
    # de-dupe
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["file_id"] in seen:
            continue
        seen.add(r["file_id"])
        out.append(r)
    out.sort(key=lambda r: int(r.get("downloads") or 0), reverse=True)
    return out


def fetch_botmek_catalog(*, timeout: float = 25.0) -> list[dict[str, Any]]:
    """HTTP fetch of BotMek dwar share list (sorted by downloads)."""
    req = Request(
        BOTMEK_SHARE_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; dwar_bot/1.0)"},
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — curated URL
        html = resp.read().decode("utf-8", errors="replace")
    rows = parse_botmek_share_html(html)
    logger.info("BotMek catalog: %d macros from share list", len(rows))
    return rows


def load_catalog(path: Optional[Path] = None) -> list[dict[str, Any]]:
    paths = [path] if path else list(_DEFAULT_CATALOG_PATHS)
    for p in paths:
        if p and Path(p).is_file():
            try:
                data = json.loads(Path(p).read_text(encoding="utf-8"))
                rows = data.get("macros") if isinstance(data, dict) else data
                if isinstance(rows, list):
                    return rows
            except Exception as exc:
                logger.debug("botmek catalog read %s: %s", p, exc)
    # Embedded fallback from presets
    return [
        {
            "file_id": p.file_id,
            "name": p.name,
            "description": p.description,
            "downloads": p.downloads,
            "source_url": p.source_url,
        }
        for p in BOTMEK_PRESETS
        if not p.file_id.startswith("local-")
    ]


def save_catalog(rows: Sequence[dict[str, Any]], path: Optional[Path] = None) -> Path:
    target = Path(path) if path else _DEFAULT_CATALOG_PATHS[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": BOTMEK_SHARE_URL,
        "fetched_at": time.time(),
        "macros": list(rows),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def refresh_catalog(path: Optional[Path] = None) -> list[dict[str, Any]]:
    rows = fetch_botmek_catalog()
    if rows:
        save_catalog(rows, path=path)
    return rows


@dataclass
class BotmekFightPlan:
    """Resolved plan for one fight (preset + effective hit seq preference)."""

    preset: BotmekPreset
    use_botmek_seq_fallback: bool = True
    prefer_down_finisher: bool = True
    enter_magic_stance: bool = False
    prebuff_keywords: tuple[str, ...] = field(default_factory=tuple)

    def fallback_hit_seq(self) -> list[int]:
        seq = botmek_fallback_sequence(self.preset)
        if self.prefer_down_finisher and seq and seq[-1] != BOTTOM_ATTACK_ID:
            seq = list(seq) + [BOTTOM_ATTACK_ID]
        return seq


def build_fight_plan(
    *,
    level: int = 1,
    enabled: bool = True,
    preset_name: str = "",
) -> Optional[BotmekFightPlan]:
    if not enabled:
        return None
    preset = select_preset(level=level, name_hint=preset_name)
    return BotmekFightPlan(
        preset=preset,
        enter_magic_stance=(preset.stance == "magic" and level >= 11),
        prebuff_keywords=preset.prebuff_keywords,
        prefer_down_finisher=True,
    )


def preset_to_dict(p: BotmekPreset) -> dict[str, Any]:
    d = asdict(p)
    d["hit_seq"] = list(p.hit_seq)
    d["prebuff_keywords"] = list(p.prebuff_keywords)
    d["burst_steps"] = list(p.burst_steps)
    return d
