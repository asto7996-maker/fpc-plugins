"""
Potion / elixir / food manager for dwar.ru.

Handles:
  * wider matching of HP/MP consumables (отвар, эликсир, зелье, снадобье…)
  * mid-fight drink decisions
  * out-of-fight heal ladder (potion → ADD_HP bag → food → regen wait)
  * combat prebuff elixirs (гнев / мощь) already covered by BotMek — here we
    focus on survivability drinks.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

# Titles / kinds that restore HP (instant or tick regen).
HP_TITLE_KW: tuple[str, ...] = (
    "здоров", "лечен", "хп", "жизн", "исцел", "восстанов",
    "хил", "реген", "снадоб", "аптеч", "благ", "эликсир жизни",
    "отвар восстанов", "малое зелье", "большое зелье", "среднее зелье",
    "зелье здоровья", "эликсир здоровья", "фляга",
)

HP_KIND_KW: tuple[str, ...] = (
    "отвар", "эликсир", "зелье", "снадоб", "фляга", "напиток",
)

# Titles that are combat buffs, NOT emergency HP (skip for heal).
HP_EXCLUDE_KW: tuple[str, ...] = (
    "гнев", "мощь", "ярость", "удачи", "опыта", "антиопыт",
    "безумия", "ворона", "грации", "решитель", "тритон",
    "мана", "магии", "энерг", "токсич",
)

MP_TITLE_KW: tuple[str, ...] = (
    "ман", "магии", "мп", "энерг", "мудрост",
)

MP_EXCLUDE_KW: tuple[str, ...] = (
    "здоров", "лечен", "жизн", "гнев", "мощь",
)

FOOD_TITLE_KW: tuple[str, ...] = (
    "яблок", "хлеб", "мяс", "пирог", "сыр", "рыба", "суп",
    "похлеб", "каш", "фрукт", "ягод", "еда", "завтрак",
)


@dataclass
class PotionPick:
    art_id: str
    title: str
    kind: str = ""
    reason: str = ""


@dataclass
class PotionSession:
    drinks: int = 0
    mid_fight_drinks: int = 0
    food_eaten: int = 0
    last_drink_at: float = 0.0
    last_mid_fight_at: float = 0.0
    last_fail_at: float = 0.0
    fails_in_row: int = 0


def _blob(title: str, kind: str = "") -> str:
    return f"{title or ''} {kind or ''}".lower()


def is_hp_consumable(title: str, kind: str = "") -> bool:
    blob = _blob(title, kind)
    if not blob.strip():
        return False
    if any(x in blob for x in HP_EXCLUDE_KW):
        # Still allow explicit "отвар восстановления"
        if "восстанов" in blob and ("отвар" in blob or "зелье" in blob or "эликсир" in blob):
            return True
        return False
    if any(x in blob for x in HP_TITLE_KW):
        return True
    # Generic potion kind without combat-buff name → treat as HP candidate
    if any(k in blob for k in HP_KIND_KW) and not any(x in blob for x in MP_TITLE_KW):
        return True
    return False


def is_mp_consumable(title: str, kind: str = "") -> bool:
    blob = _blob(title, kind)
    if not blob.strip():
        return False
    if any(x in blob for x in MP_EXCLUDE_KW):
        return False
    return any(x in blob for x in MP_TITLE_KW)


def is_food(title: str, kind: str = "") -> bool:
    blob = _blob(title, kind)
    return any(x in blob for x in FOOD_TITLE_KW)


def score_hp_potion(title: str, kind: str = "") -> int:
    """Higher = better emergency heal pick."""
    blob = _blob(title, kind)
    score = 0
    if "восстанов" in blob:
        score += 40
    if "здоров" in blob or "жизн" in blob:
        score += 30
    if "лечен" in blob or "исцел" in blob or "хил" in blob:
        score += 25
    if "большое" in blob or "велик" in blob:
        score += 15
    if "средн" in blob:
        score += 8
    if "малое" in blob or "маленьк" in blob:
        score += 3
    if "отвар" in blob:
        score += 10
    if "эликсир" in blob:
        score += 12
    if "зелье" in blob:
        score += 10
    if "снадоб" in blob:
        score += 8
    return score


class PotionManager:
    """
    Stateless helpers + tiny cooldown state for drink spam control.
    Wire into CombatEngine / FightClient / PureFarm.
    """

    def __init__(self) -> None:
        self.session = PotionSession()
        self._min_drink_gap_s = 2.5
        self._min_mid_fight_gap_s = 4.0

    def reset_session(self) -> None:
        self.session = PotionSession()

    def pick_hp_from_inventory(self, inventory: Sequence[Any]) -> Optional[PotionPick]:
        best: Optional[PotionPick] = None
        best_score = -1
        for art in inventory or []:
            title = str(getattr(art, "title", "") or "")
            kind = str(getattr(art, "kind", "") or "")
            art_id = str(getattr(art, "art_id", "") or getattr(art, "id", "") or "")
            if not art_id:
                continue
            if not is_hp_consumable(title, kind):
                continue
            sc = score_hp_potion(title, kind)
            if sc > best_score:
                best_score = sc
                best = PotionPick(art_id=art_id, title=title, kind=kind, reason=f"score={sc}")
        return best

    def pick_mp_from_inventory(self, inventory: Sequence[Any]) -> Optional[PotionPick]:
        for art in inventory or []:
            title = str(getattr(art, "title", "") or "")
            kind = str(getattr(art, "kind", "") or "")
            art_id = str(getattr(art, "art_id", "") or getattr(art, "id", "") or "")
            if not art_id:
                continue
            if is_mp_consumable(title, kind):
                return PotionPick(art_id=art_id, title=title, kind=kind, reason="mp")
        return None

    def should_drink_out_of_fight(self, hp_percent: float, threshold: float) -> bool:
        if hp_percent >= float(threshold):
            return False
        # Allow near-death drinks (was blocked at ≤0.5% and caused deaths)
        if hp_percent <= 0:
            return False
        now = time.time()
        if now - self.session.last_drink_at < self._min_drink_gap_s:
            return False
        if self.session.fails_in_row >= 5 and now - self.session.last_fail_at < 30:
            return False
        return True

    def should_drink_mid_fight(self, hp_percent: float, threshold: float) -> bool:
        if hp_percent >= float(threshold):
            return False
        if hp_percent <= 0:
            return False
        now = time.time()
        if now - self.session.last_mid_fight_at < self._min_mid_fight_gap_s:
            return False
        if self.session.fails_in_row >= 3 and now - self.session.last_fail_at < 20:
            return False
        return True

    def note_drink(self, *, mid_fight: bool = False, ok: bool = True) -> None:
        now = time.time()
        self.session.last_drink_at = now
        if mid_fight:
            self.session.last_mid_fight_at = now
        if ok:
            self.session.drinks += 1
            if mid_fight:
                self.session.mid_fight_drinks += 1
            self.session.fails_in_row = 0
        else:
            self.session.fails_in_row += 1
            self.session.last_fail_at = now
