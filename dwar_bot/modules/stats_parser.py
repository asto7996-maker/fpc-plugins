"""
Stats parser — reads profile, backpack, money, effects and notifications
via the dwar.ru HTTP API (no DOM/Flash required).

Data sources
------------
* ``user.php``                  — ``par`` variable (hp/mp/lvl/nick) + ``art_alt`` inventory JSON
* ``user.php?mode=...&group=N`` — per-tab data (Эффекты / Вещи / Квесты / Элементы)
* ``entry_point.php`` common|dummy — money, area, flags
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from dwar_bot.core.game_client import DwarGameClient, CharStats, GameState

logger = logging.getLogger(__name__)

# user.php tab groups
TAB_EFFECTS = 1
TAB_THINGS = 2
TAB_MISC = 3
TAB_QUESTS = 4
TAB_ELEMENTS = 5
TAB_GIFTS = 6
TAB_EXPIRING = 7


def is_fight_lock_html(html: str) -> bool:
    """
    True when user.php returns the in-fight redirect stub.

    During a fight the server often serves a short page that only boots
    ``fight.php`` (via ``tProcessMenu``) and has no ``var par=…`` character blob.
    Soft session recheck still succeeds because ``common|dummy`` returns state
    (including ``fight_id``).
    """
    text = html or ""
    if "var par=" in text:
        return False
    return "fight.php" in text and "tProcessMenu" in text


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class Artifact:
    """One inventory item (artifact) as returned by the game."""
    art_id: str = ""
    alt_id: str = ""
    title: str = ""
    kind: str = ""              # "Рюкзак", "Оружие", "Отвар", …
    kind_id: str = ""
    quality: str = "0"
    durability: int = 0
    durability_max: int = 0
    level: int = 0
    description: str = ""
    can_drop: bool = False
    can_sell: bool = True
    icon_list: list[str] = field(default_factory=list)
    expire_text: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_broken(self) -> bool:
        return self.durability_max > 0 and self.durability <= 0

    @property
    def durability_percent(self) -> float:
        return (self.durability / self.durability_max * 100) if self.durability_max else 100.0

    @property
    def is_potion(self) -> bool:
        kl = self.kind.lower()
        return "отвар" in kl or "эликсир" in kl or "зелье" in kl

    @property
    def is_equipment(self) -> bool:
        kl = self.kind.lower()
        return any(w in kl for w in ("оружие", "броня", "шлем", "щит", "сапог", "перчат", "пояс", "плащ"))


@dataclass
class Effect:
    """An active buff/debuff on the character."""
    effect_id: str = ""
    title: str = ""
    time_left: str = ""
    is_hidden: bool = False


@dataclass
class Notification:
    text: str = ""
    category: str = "info"


@dataclass
class FullProfile:
    """Complete character snapshot."""
    char: CharStats = field(default_factory=CharStats)
    state: GameState = field(default_factory=GameState)
    inventory: list[Artifact] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)

    @property
    def potions(self) -> list[Artifact]:
        return [a for a in self.inventory if a.is_potion]

    @property
    def equipment(self) -> list[Artifact]:
        return [a for a in self.inventory if a.is_equipment]

    @property
    def broken_items(self) -> list[Artifact]:
        return [a for a in self.inventory if a.is_broken]

    @property
    def total_money(self) -> float:
        return self.state.money


# ---------------------------------------------------------------------------
# StatsParser
# ---------------------------------------------------------------------------

class StatsParser:
    """Reads the complete character profile via HTTP."""

    def __init__(self, client: DwarGameClient) -> None:
        self._client = client
        self._last_profile: Optional[FullProfile] = None

    # ------------------------------------------------------------------
    # Full profile
    # ------------------------------------------------------------------

    async def read_full_profile(self) -> FullProfile:
        """Fetch state, stats, inventory, effects and notifications in one pass."""
        profile = FullProfile()
        try:
            profile.state = await self._client.get_state()
            # One user.php for nick/hp + inventory/effects (avoid double GET)
            html = await self._fetch_user_page()
            profile.char = self._client.parse_char_stats(html)
            if not profile.char.nick and is_fight_lock_html(html):
                logger.info(
                    "user.php fight-lock stub (no var par); fight_id=%s — finish fight first",
                    getattr(profile.state, "fight_id", 0) or 0,
                )
            profile.inventory = self._parse_inventory(html)
            profile.effects = self._parse_effects(html)
            profile.notifications = self._parse_notifications(html)

            self._last_profile = profile
            logger.debug(
                "Profile: %s Lv%d HP=%d/%d items=%d effects=%d",
                profile.char.nick, profile.char.level,
                profile.char.hp, profile.char.hp_max,
                len(profile.inventory), len(profile.effects),
            )
        except Exception as exc:
            logger.warning("read_full_profile failed: %s", exc, exc_info=True)
        return profile

    async def _fetch_user_page(self, group: int = TAB_THINGS) -> str:
        """Download user.php (optionally a specific tab)."""
        try:
            resp = await self._client._get("/user.php")
            return resp.text
        except Exception as exc:
            logger.debug("_fetch_user_page error: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    def _parse_inventory(self, html: str) -> list[Artifact]:
        """
        Parse all ``art_alt["AA_xxx"] = {...};`` assignments into Artifact objects.
        """
        items: list[Artifact] = []
        pattern = re.compile(r'art_alt\["([^"]+)"\]\s*=\s*(\{.*?\});', re.DOTALL)
        for alt_id, json_str in pattern.findall(html):
            try:
                d = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            lev = d.get("lev", {})
            level = 0
            if isinstance(lev, dict):
                try:
                    level = int(lev.get("value", 0))
                except (TypeError, ValueError):
                    level = 0

            exp = d.get("exp", {})
            expire_text = exp.get("value", "") if isinstance(exp, dict) else ""

            items.append(Artifact(
                art_id=str(d.get("id", "")),
                alt_id=alt_id,
                title=d.get("title", ""),
                kind=d.get("kind", ""),
                kind_id=str(d.get("kind_id", "")),
                quality=str(d.get("quality", "0")),
                durability=self._safe_int(d.get("dur")),
                durability_max=self._safe_int(d.get("dur_max")),
                level=level,
                description=d.get("desc", ""),
                can_drop=bool(d.get("drop", False)),
                can_sell="nosell" not in d,
                icon_list=d.get("icon_list", []) or [],
                expire_text=expire_text,
                raw=d,
            ))
        return items

    @staticmethod
    def _safe_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # Effects / buffs
    # ------------------------------------------------------------------

    def _parse_effects(self, html: str) -> list[Effect]:
        """
        Extract active effects. The game embeds these as EFFECT_HIDE/EFFECT_SHOW
        links plus a human-readable title in the surrounding markup.
        """
        effects: list[Effect] = []
        seen: set[str] = set()

        # Effect entries appear as: code=EFFECT_HIDE...&effect_id=NNN  with a title nearby
        for m in re.finditer(
            r'code(?:%3D|=)EFFECT_(HIDE|SHOW)[^"\'<>]*?effect_id(?:%3D|=)(\d+)', html
        ):
            eff_id = m.group(2)
            if eff_id in seen:
                continue
            seen.add(eff_id)
            # Look for a title within 400 chars after the match
            window = html[m.end():m.end() + 400]
            title_m = re.search(r'>([А-ЯЁа-яё][^<>]{3,60})<', window)
            effects.append(Effect(
                effect_id=eff_id,
                title=title_m.group(1).strip() if title_m else f"Эффект #{eff_id}",
                is_hidden=(m.group(1) == "SHOW"),  # SHOW link means it's currently hidden
            ))

        # Fallback: named blessings/buffs mentioned in bonus text
        for m in re.finditer(r'(Благословение[^"<,\']{0,50}|Проклятие[^"<,\']{0,50})', html):
            title = m.group(1).strip()
            if title and title not in [e.title for e in effects]:
                effects.append(Effect(title=title))

        return effects

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _parse_notifications(self, html: str) -> list[Notification]:
        """Extract system messages / bonus texts embedded in the page."""
        notes: list[Notification] = []

        # bonus_text arrays from previous actions
        for m in re.finditer(r'bonus_text["\']?\s*:\s*\[([^\]]*)\]', html):
            for txt in re.findall(r'"([^"]{3,200})"', m.group(1)):
                notes.append(Notification(text=txt, category="bonus"))

        # showError() calls
        for m in re.finditer(r'showError\(["\']([^"\']{3,200})["\']\)', html):
            notes.append(Notification(text=m.group(1), category="error"))

        # System message blocks
        for m in re.finditer(r'class=["\']sys_msg["\'][^>]*>([^<]{3,200})<', html):
            notes.append(Notification(text=m.group(1).strip(), category="system"))

        return notes

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    async def count_potions(self, keyword: str = "") -> int:
        """Count potions in the backpack, optionally filtered by name keyword."""
        profile = self._last_profile or await self.read_full_profile()
        potions = profile.potions
        if keyword:
            potions = [p for p in potions if keyword.lower() in p.title.lower()]
        return len(potions)

    async def find_potion(self, keywords: list[str]) -> Optional[Artifact]:
        """Return the first potion whose title contains any of *keywords*."""
        profile = self._last_profile or await self.read_full_profile()
        for potion in profile.potions:
            title = potion.title.lower()
            if any(kw.lower() in title for kw in keywords):
                return potion
        return None

    async def find_hp_potion(self) -> Optional[Artifact]:
        return await self.find_potion(["здоров", "лечен", "хп", "жизн", "исцел"])

    async def find_mp_potion(self) -> Optional[Artifact]:
        return await self.find_potion(["ман", "магии", "мп", "энерг"])

    async def find_food(self, keywords: list[str]) -> Optional[Artifact]:
        """Find edible item by title keywords (SUIS post-battle food ladder)."""
        profile = self._last_profile or await self.read_full_profile()
        needles = [k.lower() for k in keywords if k]
        for art in profile.inventory:
            title = (art.title or "").lower()
            if title and any(n in title for n in needles):
                return art
        return None

    def get_cached_profile(self) -> Optional[FullProfile]:
        return self._last_profile
