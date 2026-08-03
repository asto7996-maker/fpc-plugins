"""
Battle strategy — adapted from DwarBOT-v.1.0 physical combat logic.

DwarBOT (screen/OCR) did:
  * hit sequence for combo / «суперудар» (forward, down, down, up, forward)
  * block when HP below threshold; exit block before the finisher
  * elixir when HP critically low
  * win / defeat handling + post-battle refresh

Here the same decisions drive the HTML5 fight WebSocket
(FS_SCCL_ATTACK zones + FS_SCCL_CHANGE_MODE for block), not mouse clicks.

Zone IDs (from canvas.app.battle.Const):
  TOP / up = 1, MIDDLE / forward = 2, BOTTOM / down = 3
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

# Hit zones (canvas Const)
TOP_ATTACK_ID = 1
MIDDLE_ATTACK_ID = 2
BOTTOM_ATTACK_ID = 3

ZONE_BY_NAME: dict[str, int] = {
    "up": TOP_ATTACK_ID,
    "top": TOP_ATTACK_ID,
    "1": TOP_ATTACK_ID,
    "forward": MIDDLE_ATTACK_ID,
    "mid": MIDDLE_ATTACK_ID,
    "middle": MIDDLE_ATTACK_ID,
    "2": MIDDLE_ATTACK_ID,
    "down": BOTTOM_ATTACK_ID,
    "bot": BOTTOM_ATTACK_ID,
    "bottom": BOTTOM_ATTACK_ID,
    "3": BOTTOM_ATTACK_ID,
}

ZONE_NAME: dict[int, str] = {
    TOP_ATTACK_ID: "up",
    MIDDLE_ATTACK_ID: "forward",
    BOTTOM_ATTACK_ID: "down",
}

# DwarBOT config.ini default: forward, down, down, up, forward
DEFAULT_HIT_SEQ: tuple[int, ...] = (
    MIDDLE_ATTACK_ID,
    BOTTOM_ATTACK_ID,
    BOTTOM_ATTACK_ID,
    TOP_ATTACK_ID,
    MIDDLE_ATTACK_ID,
)

# Block toggle (canvas handlerBlockSwitch → changeMode)
FS_SCCL_CHANGE_MODE = 109
TO_FS_PF_DEFENDED = 0
FS_PF_DEFENDED = 256


def parse_hit_list(raw: str | Sequence[str | int] | None) -> list[int]:
    """Parse DwarBOT-style ``forward, down, down, up, forward`` or ``2,3,3,1,2``."""
    if raw is None:
        return list(DEFAULT_HIT_SEQ)
    if isinstance(raw, (list, tuple)):
        parts = [str(x).strip() for x in raw if str(x).strip()]
    else:
        parts = [p.strip() for p in re.split(r"[,;\s]+", str(raw)) if p.strip()]
    out: list[int] = []
    for p in parts:
        key = p.lower()
        if key in ZONE_BY_NAME:
            out.append(ZONE_BY_NAME[key])
            continue
        try:
            z = int(p)
        except ValueError:
            logger.warning("Unknown hit zone %r — skip", p)
            continue
        if z in (TOP_ATTACK_ID, MIDDLE_ATTACK_ID, BOTTOM_ATTACK_ID):
            out.append(z)
    return out or list(DEFAULT_HIT_SEQ)


def extract_combo_sequences(conf: dict[str, Any] | None) -> list[tuple[int, str, list[int]]]:
    """
    Pull combo sequences from fight|conf / swf_fight_vars.

    Returns list of (combo_id, title, zone_ids), longest first preferred by caller.
    """
    if not conf:
        return []
    blobs: list[str] = []
    for key in ("combos", "combo", "combos_xml"):
        v = conf.get(key)
        if isinstance(v, str) and "<combo" in v.lower():
            blobs.append(v)
    vars_ = conf.get("swf_fight_vars") or {}
    if isinstance(vars_, dict):
        for key in ("combos", "combo"):
            v = vars_.get(key)
            if isinstance(v, str) and "<combo" in v.lower():
                blobs.append(v)
    # Nested fight_user
    fu = conf.get("fight_user") or vars_.get("fight_user") if isinstance(vars_, dict) else None
    if isinstance(fu, dict):
        v = fu.get("combos") or fu.get("combo")
        if isinstance(v, str) and "<combo" in v.lower():
            blobs.append(v)

    found: list[tuple[int, str, list[int]]] = []
    for blob in blobs:
        try:
            root = ET.fromstring(f"<root>{blob}</root>")
        except ET.ParseError:
            continue
        for node in root.findall(".//combo"):
            seq = (node.attrib.get("seq") or "").strip()
            if not seq:
                continue
            zones = parse_hit_list(list(seq))  # chars "123" → zones
            # seq is usually "23121" without separators
            if len(zones) != len(seq):
                zones = []
                for ch in seq:
                    if ch in ZONE_BY_NAME:
                        zones.append(ZONE_BY_NAME[ch])
            if not zones:
                continue
            try:
                cid = int(node.attrib.get("id") or 0)
            except ValueError:
                cid = 0
            title = str(node.attrib.get("title") or node.attrib.get("description") or cid)
            found.append((cid, title, zones))
    return found


def pick_hit_sequence(
    conf: dict[str, Any] | None = None,
    *,
    configured: str | Sequence[str | int] | None = None,
) -> list[int]:
    """
    Prefer longest combo from fight conf (like canvas ComboView.getLongestCombo),
    else configured / DwarBOT default sequence.
    """
    combos = extract_combo_sequences(conf)
    if combos:
        best = max(combos, key=lambda c: len(c[2]))
        logger.info(
            "Battle strategy: combo id=%s '%s' seq=%s",
            best[0], best[1], [ZONE_NAME.get(z, z) for z in best[2]],
        )
        return list(best[2])
    seq = parse_hit_list(configured)
    logger.info(
        "Battle strategy: hit seq=%s (DwarBOT-adapted)",
        [ZONE_NAME.get(z, z) for z in seq],
    )
    return seq


@dataclass
class TurnDecision:
    """What to do on ATTACKNOW."""
    hit_zone: int
    set_block: Optional[bool] = None  # True=on, False=off, None=unchanged
    is_finisher: bool = False
    zone_name: str = ""


@dataclass
class FightBrain:
    """
    Stateful fight controller (DwarBOT fight() loop → WS decisions).
    """
    hit_seq: list[int] = field(default_factory=lambda: list(DEFAULT_HIT_SEQ))
    # HP % below which we enter block (DwarBOT max_hp_without_block analogue, %)
    block_hp_percent: float = 45.0
    # HP % above which we leave block
    unblock_hp_percent: float = 60.0
    # Exit block before the last hit of the current sequence cycle
    unblock_before_finisher: bool = True
    # Block toggle cooldown (canvas uses 5s)
    block_cooldown_s: float = 5.0

    # Runtime
    _step: int = 0
    block_active: bool = False
    _last_block_toggle_at: float = 0.0
    pers_id: int = 0
    pers_team: Optional[int] = None
    hp: int = 0
    hp_max: int = 0
    damage_dealt: int = 0
    damage_taken: int = 0
    hits_sent: int = 0
    blocks_toggled: int = 0

    @property
    def hp_percent(self) -> float:
        if self.hp_max <= 0:
            return 100.0
        return max(0.0, min(100.0, 100.0 * self.hp / self.hp_max))

    def seed_hp(self, hp: int, hp_max: int) -> None:
        if hp_max > 0:
            self.hp = max(0, int(hp))
            self.hp_max = int(hp_max)

    def note_state_hp(self, hp: int, hp_max: int) -> None:
        if hp_max > 0:
            self.hp = int(hp)
            self.hp_max = int(hp_max)

    def note_damage(self, target_pers_id: int, amount: int) -> None:
        dmg = abs(int(amount or 0))
        if dmg <= 0:
            return
        if self.pers_id and int(target_pers_id) == int(self.pers_id):
            self.damage_taken += dmg
            if self.hp > 0:
                self.hp = max(0, self.hp - dmg)
        else:
            self.damage_dealt += dmg

    def _want_block(self) -> bool:
        return self.hp_percent < self.block_hp_percent

    def _want_unblock_by_hp(self) -> bool:
        return self.hp_percent >= self.unblock_hp_percent

    def decide_turn(self, *, now: float) -> TurnDecision:
        """
        Pick next hit zone and optional block change (DwarBOT fight loop).

        Finisher (last in cycle): force leave block so the combo lands hard.
        Other hits: enter/leave block from HP thresholds.
        """
        n = len(self.hit_seq) or 1
        pos = self._step % n
        zone = int(self.hit_seq[pos])
        is_finisher = pos == (n - 1)
        self._step += 1
        self.hits_sent += 1

        set_block: Optional[bool] = None
        can_toggle = (now - self._last_block_toggle_at) >= self.block_cooldown_s

        if is_finisher and self.unblock_before_finisher and self.block_active:
            if can_toggle:
                set_block = False
        elif not is_finisher:
            if self._want_block() and not self.block_active and can_toggle:
                set_block = True
            elif self._want_unblock_by_hp() and self.block_active and can_toggle:
                set_block = False

        return TurnDecision(
            hit_zone=zone,
            set_block=set_block,
            is_finisher=is_finisher,
            zone_name=ZONE_NAME.get(zone, str(zone)),
        )

    def apply_block_result(self, enabled: bool, *, now: float) -> None:
        self.block_active = bool(enabled)
        self._last_block_toggle_at = now
        self.blocks_toggled += 1
