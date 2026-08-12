"""
Progression Brain — goal-oriented autopilot for becoming stronger.

Analyses the live game snapshot and produces:
* **now**     — what the character is doing / should do this tick
* **plan**    — ordered upcoming goals
* **options** — everything currently available (NPCs, fights, travel, loot…)
* **focus**   — the single best auto-selected action

Priority ladder (power growth)
------------------------------
1. Survive (HP critical / heal / flee)
2. Claim loot / awards / ready quest turn-ins
3. Advance story NPC dialogues (unlocks the world)
4. Maintain gear (repair / equip)
5. Farm combat (area points → arena → fronts)
6. Explore / travel when gates open
7. Buffs / idle regen while nothing else pays off
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

from dwar_bot.modules.bot_settings import BotSettings
from dwar_bot.modules.stats_parser import FullProfile
from dwar_bot.core.game_client import AreaInfo, AreaItem, GameState, CharStats

logger = logging.getLogger(__name__)


class GoalKind(Enum):
    SURVIVE = auto()
    LOOT = auto()
    QUEST = auto()
    GEAR = auto()
    COMBAT = auto()
    TRAVEL = auto()
    BUFF = auto()
    EVENT = auto()
    IDLE = auto()


class ActionType(Enum):
    HEAL = "heal"
    WAIT_REGEN = "wait_regen"
    REPAIR = "repair"
    EQUIP = "equip"
    QUEST_NPC = "quest_npc"
    QUEST_TURNIN = "quest_turnin"
    COMBAT_AREA = "combat_area"
    COMBAT_ARENA = "combat_arena"
    COMBAT_FRONT = "combat_front"
    HUNT_MOB = "hunt_mob"
    TRAVEL = "travel"
    AREA_ACTION = "area_action"
    BUFF = "buff"
    USE_ITEM = "use_item"
    IDLE = "idle"


@dataclass
class GameOption:
    """Something the character *could* do right now."""
    action: ActionType
    title: str
    score: float = 0.0
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    goal: GoalKind = GoalKind.IDLE

    def short(self) -> str:
        return f"{self.title}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class PlanStep:
    goal: GoalKind
    title: str
    why: str = ""
    eta: str = ""


@dataclass
class ProgressSnapshot:
    """Full brain output for one decision cycle."""
    now: str = "Инициализация…"
    now_action: Optional[GameOption] = None
    plan: list[PlanStep] = field(default_factory=list)
    options: list[GameOption] = field(default_factory=list)
    focus: Optional[GameOption] = None
    power_score: float = 0.0
    bottlenecks: list[str] = field(default_factory=list)
    tips: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def report_html(self) -> str:
        lines = [
            "<b>🧠 Мозг прогрессии</b>",
            f"<b>Сейчас:</b> {self._esc(self.now)}",
        ]
        if self.focus:
            lines.append(
                f"<b>Выбрано:</b> {self._esc(self.focus.title)}"
                + (f" <i>({self._esc(self.focus.detail)})</i>" if self.focus.detail else "")
            )
        lines.append(f"<b>Сила (оценка):</b> {self.power_score:.0f}/100")
        if self.plan:
            lines.append("\n<b>План:</b>")
            for i, step in enumerate(self.plan[:6], 1):
                lines.append(f"{i}. {self._esc(step.title)}"
                             + (f" — <i>{self._esc(step.why)}</i>" if step.why else ""))
        if self.options:
            lines.append("\n<b>Можно сделать:</b>")
            for opt in sorted(self.options, key=lambda o: -o.score)[:8]:
                mark = "👉" if self.focus and opt is self.focus else "•"
                lines.append(f"{mark} {self._esc(opt.short())} <code>{opt.score:.0f}</code>")
        if self.bottlenecks:
            lines.append("\n<b>Узкие места:</b>")
            for b in self.bottlenecks[:4]:
                lines.append(f"⚠️ {self._esc(b)}")
        if self.tips:
            lines.append("\n<b>Чтобы стать сильнее:</b>")
            for t in self.tips[:4]:
                lines.append(f"💡 {self._esc(t)}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "now": self.now,
            "focus": (
                {
                    "action": self.focus.action.value,
                    "title": self.focus.title,
                    "detail": self.focus.detail,
                    "score": self.focus.score,
                    "goal": self.focus.goal.name,
                }
                if self.focus else None
            ),
            "plan": [
                {"goal": s.goal.name, "title": s.title, "why": s.why}
                for s in self.plan[:8]
            ],
            "options": [
                {
                    "action": o.action.value,
                    "title": o.title,
                    "detail": o.detail,
                    "score": o.score,
                    "goal": o.goal.name,
                }
                for o in sorted(self.options, key=lambda x: -x.score)[:12]
            ],
            "power_score": self.power_score,
            "bottlenecks": self.bottlenecks[:6],
            "tips": self.tips[:5],
            "updated_at": self.updated_at,
        }

    @staticmethod
    def _esc(text: Any) -> str:
        return (
            str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )


class ProgressionBrain:
    """
    Stateless analyser + sticky 'now' description for the UI.
    Call ``analyze()`` every tick with fresh world data.
    """

    # Arena / event NPCs with long timers are not farm — skip until ready
    ARENA_READY_SEC = 90
    # After this many empty hotspot clicks, prefer travel/fronts hard
    EMPTY_FARM_ESCALATE = 2

    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings
        self.last = ProgressSnapshot()
        self._action_history: list[str] = []
        self._loot_seen: set[str] = set()
        # action_key → consecutive empty/no-progress results
        self._stale: dict[str, int] = {}
        # hotspot name → unix ts when usable again (client-side CD)
        self._cooldowns: dict[str, float] = {}
        # Farm-first mode until a fight/loot succeeds (stuck newbie village)
        self.farm_push_until: float = 0.0
        # Travel blocked by war chief → must advance local quest NPC
        self.need_quest_unlock: bool = False
        # Village exit gated by Военачальник (unix ts) — don't retry every 5–10 min
        self.village_exit_blocked_until: float = 0.0
        # Quest type=2 kill target (e.g. Крэтс) — prefer hunt_farm attack
        self.pending_hunt_mob: str = ""
        # After a quest-required kill: talk to NPC before hunting again
        self.awaiting_quest_turnin: bool = False
        # Consecutive casual hunts without quest/loot progress
        self._hunt_streak: int = 0

    def mark_cooldown(self, name: str, seconds: float) -> None:
        if seconds <= 0:
            return
        until = time.time() + float(seconds)
        prev = self._cooldowns.get(name, 0)
        self._cooldowns[name] = max(prev, until)
        logger.info("Brain CD: '%s' for %.0fs", name, seconds)

    def mark_village_exit_blocked(self, seconds: float = 7200.0) -> None:
        """
        War-chief gate: leave village is impossible until story advances.
        Long ban so planner stops bouncing Hunt ↔ Дымные сопки.
        """
        until = time.time() + max(60.0, float(seconds))
        self.village_exit_blocked_until = max(self.village_exit_blocked_until, until)
        for title in list(self._cooldowns.keys()):
            if "переход" in title.lower() and any(
                kw in title.lower() for kw in ("сопк", "дымн", "охот")
            ):
                self._cooldowns[title] = max(self._cooldowns[title], until)
        # Also seed common titles even if not yet seen
        for name in ("Переход: В Дымные сопки", "Переход: Дымные сопки"):
            prev = self._cooldowns.get(name, 0)
            self._cooldowns[name] = max(prev, until)
        logger.info(
            "Brain: village exit blocked for %.0fs (farm in village)",
            seconds,
        )

    def village_exit_blocked(self) -> bool:
        return time.time() < float(self.village_exit_blocked_until or 0.0)

    def clear_cooldowns(self) -> None:
        self._cooldowns.clear()
        self.village_exit_blocked_until = 0.0

    def push_farm(self, seconds: float = 600.0) -> None:
        """Temporarily prefer travel/fronts/hotspots over stuck quests."""
        self.farm_push_until = max(self.farm_push_until, time.time() + seconds)
        logger.info("Brain: farm-push ON for %.0fs", seconds)

    def farm_push_active(self) -> bool:
        return time.time() < self.farm_push_until

    def mark_hunt_for_quest(self, mob: str) -> None:
        """Quest type=2 needs a live kill — hunt once, then turn in."""
        self.pending_hunt_mob = mob or self.pending_hunt_mob or "Крэтс"
        self.need_quest_unlock = True
        self.awaiting_quest_turnin = False
        self._hunt_streak = 0

    def mark_hunt_kill_done(self, *, quest_gate: bool = False) -> None:
        """
        After a hunt win.

        Only arm NPC turn-in when caller passes quest_gate=True (real type=2).
        Soft open-farm pins must NOT set pending_hunt_mob / quest_gate.
        """
        gated = bool(quest_gate)
        self._hunt_streak = 0
        if not gated:
            return
        self.awaiting_quest_turnin = True
        self.farm_push_until = 0.0

    def clear_hunt_gate(self) -> None:
        """Quest kill gate cleared (dialogue advanced, world objective, or travel unlocked)."""
        self.pending_hunt_mob = ""
        self.awaiting_quest_turnin = False
        self.need_quest_unlock = False
        self._hunt_streak = 0

    def empty_streak(self, name: str) -> int:
        """How many consecutive empty results for a combat hotspot title."""
        return max(
            self._stale.get(f"{ActionType.COMBAT_AREA.value}:Точка: {name}", 0),
            self._stale.get(f"{ActionType.AREA_ACTION.value}:Точка: {name}", 0),
        )

    def note_result(self, option: Optional[GameOption], *, progressed: bool) -> None:
        """Feed back whether the chosen action produced progress (loot/fight/dialog)."""
        if not option:
            return
        key = f"{option.action.value}:{option.title}"
        if progressed:
            self._stale.pop(key, None)
            if option.action in (
                ActionType.COMBAT_AREA, ActionType.COMBAT_FRONT,
                ActionType.COMBAT_ARENA, ActionType.TRAVEL,
            ):
                self.farm_push_until = 0.0
            if option.action == ActionType.HUNT_MOB:
                self.mark_hunt_kill_done(quest_gate=bool(self.pending_hunt_mob))
            elif option.action == ActionType.QUEST_NPC:
                self.awaiting_quest_turnin = False
                self._hunt_streak = 0
            elif option.action == ActionType.TRAVEL:
                self.need_quest_unlock = False
                self.clear_hunt_gate()
        else:
            self._stale[key] = self._stale.get(key, 0) + 1
            if option.action == ActionType.HUNT_MOB:
                self._hunt_streak += 1
            if option.action in (ActionType.COMBAT_AREA, ActionType.AREA_ACTION):
                if self._stale[key] >= self.EMPTY_FARM_ESCALATE:
                    self.push_farm(480.0)

    def _stale_penalty(self, option: GameOption) -> float:
        key = f"{option.action.value}:{option.title}"
        n = self._stale.get(key, 0)
        if n <= 0:
            return 0.0
        # Empty hotspots / failed arena drop fast so travel/fronts win
        if option.action in (ActionType.COMBAT_AREA, ActionType.AREA_ACTION, ActionType.COMBAT_ARENA):
            return min(700.0, 120.0 * n)
        if option.action == ActionType.QUEST_NPC:
            return min(500.0, 100.0 * n)
        return min(350.0, 80.0 * n)

    # ------------------------------------------------------------------
    def analyze(
        self,
        *,
        profile: FullProfile,
        area: AreaInfo,
        npcs: list[dict],
        story_npc: Optional[dict] = None,
        local_npcs: Optional[list] = None,
        in_battle: bool = False,
        event_timers: Optional[dict[str, int]] = None,
        exhausted_npcs: Optional[set[str]] = None,
    ) -> ProgressSnapshot:
        farm = self.settings.farm
        char = profile.char
        state = profile.state
        options: list[GameOption] = []
        exhausted = exhausted_npcs or set()
        empty_bag = not profile.inventory
        max_farm = bool(farm.max_farm)

        power = self._power_score(profile, area)
        bottlenecks = self._bottlenecks(profile, area)
        tips = self._tips(profile, area, bottlenecks)

        # --- Survive ---
        if char.hp_max and char.hp_percent < farm.hp_retreat:
            options.append(GameOption(
                ActionType.WAIT_REGEN,
                "Восстановить HP",
                score=1000,
                detail=f"HP {char.hp_percent:.0f}% < {farm.hp_retreat:.0f}%",
                goal=GoalKind.SURVIVE,
            ))
            if farm.auto_heal and profile.potions:
                options.append(GameOption(
                    ActionType.HEAL,
                    "Выпить зелье",
                    score=1100,
                    detail=profile.potions[0].title,
                    payload={"artifact_id": profile.potions[0].art_id},
                    goal=GoalKind.SURVIVE,
                ))

        if in_battle:
            options.append(GameOption(
                ActionType.HUNT_MOB,
                "Доиграть бой (WS)",
                score=1200,
                detail="уже в схватке — finish via wsproxy",
                payload={"finish_only": True},
                goal=GoalKind.COMBAT,
            ))

        # --- Gear ---
        if farm.auto_repair and profile.broken_items:
            options.append(GameOption(
                ActionType.REPAIR,
                f"Ремонт ×{len(profile.broken_items)}",
                score=900,
                detail=", ".join(i.title for i in profile.broken_items[:3]),
                goal=GoalKind.GEAR,
            ))
        unequipped = [
            i for i in profile.equipment
            if not i.is_broken and i.icon_list and "info" in i.icon_list and len(i.icon_list) == 1
        ]
        if farm.auto_equip and unequipped:
            options.append(GameOption(
                ActionType.EQUIP,
                f"Надеть снаряжение ×{len(unequipped)}",
                score=920,
                detail=unequipped[0].title,
                goal=GoalKind.GEAR,
            ))

        # --- Story / quests ---
        # Junk global NPCs (seasonal chronicle / long-CD arena) — never "story"
        # Plus post-village flavor (Сугор / Лука) that stalls MaxFarm planner.
        _JUNK_NPC_IDS = {"816", "817", "121", "132"}
        _JUNK_TITLE_KW = (
            "летопис", "сезонн", "арену - зной", "адский зверинец",
            "сугор", "лука", "сиротск", "дом сугора",
        )

        def _is_junk_npc(npc_id: str = "", title: str = "") -> bool:
            if str(npc_id or "") in _JUNK_NPC_IDS:
                return True
            tl = (title or "").lower()
            return any(k in tl for k in _JUNK_TITLE_KW)

        if farm.auto_quests and story_npc and story_npc.get("npc_id"):
            sid = str(story_npc["npc_id"])
            if sid not in exhausted and not _is_junk_npc(sid, str(story_npc.get("title") or "")):
                raw_g = story_npc.get("global_npc", 1)
                is_global = int(0 if raw_g in (0, "0", False) else (raw_g or 1)) == 1
                # Global seasonal NPCs must not outrank village story
                score = 520 if is_global else 980
                if self.awaiting_quest_turnin and not is_global:
                    score = 1250  # turn in kill BEFORE next hunt
                elif self.need_quest_unlock and not is_global:
                    score = 1050
                options.append(GameOption(
                    ActionType.QUEST_NPC,
                    f"Сюжетный NPC #{sid}",
                    score=score,
                    detail=(
                        "глобальный указатель" if is_global
                        else "сервер указал следующего NPC"
                    ),
                    payload={
                        "npc_id": sid,
                        "global_npc": 1 if is_global else 0,
                        "link_id": str(story_npc.get("link_id") or "0"),
                        "f_id": str(story_npc.get("f_id") or "0"),
                        "area_id": str(story_npc.get("area_id") or area.area_id or "0"),
                        "href": str(story_npc.get("url") or ""),
                    },
                    goal=GoalKind.EVENT if is_global else GoalKind.QUEST,
                ))

        for npc in (local_npcs or []):
            if not farm.auto_quests:
                break
            if str(npc.npc_id) in exhausted:
                continue
            if _is_junk_npc(str(npc.npc_id), str(npc.title or "")):
                continue
            # Level-up / village story NPCs always beat casual hunt/arena
            qscore = 950 if not npc.is_global else 520
            # Newbie village: story dialogue (Вождь / Военачальник) > Крэтс farm
            area_now = str(getattr(state, "area_id", "") or "")
            if not npc.is_global and area_now in {"930", "931", "932"}:
                qscore = max(qscore, 1100)
            if self.awaiting_quest_turnin and not npc.is_global:
                qscore = 1250
            elif self.need_quest_unlock and not npc.is_global:
                qscore = 1180
            # Long arena CD — do not offer as quest
            if npc.is_global and int(getattr(npc, "time_left", 0) or 0) > 120:
                continue
            options.append(GameOption(
                ActionType.QUEST_NPC,
                f"NPC: {npc.title}",
                score=qscore,
                detail=f"id={npc.npc_id}",
                payload={
                    "npc_id": str(npc.npc_id),
                    "global_npc": 1 if npc.is_global else 0,
                    "link_id": str(npc.link_id or "0"),
                    "f_id": str(npc.f_id or "0"),
                    "area_id": str(npc.area_id or area.area_id or "0"),
                    "href": str(npc.url or ""),
                },
                goal=GoalKind.QUEST if not npc.is_global else GoalKind.EVENT,
            ))

        # --- Area items: combat hotspots, travel, actions ---
        for item in area.items:
            itype = (item.item_type or "").lower()
            name = item.name or f"#{item.item_id}"

            if itype == "npc" and item.npc_id and farm.auto_quests:
                if str(item.npc_id) in exhausted:
                    continue
                area_now = str(getattr(state, "area_id", "") or area.area_id or "")
                nscore = 1250 if self.awaiting_quest_turnin else (
                    1180 if self.need_quest_unlock else 960
                )
                if area_now in {"930", "931", "932"}:
                    nscore = max(nscore, 1100)
                # Post-village farm fronts: flavor locals (Сугор/Лука) must not
                # outrank spider hunts when no WO / turn-in is pending.
                post_farm = area_now in {"192", "227", "226", "159", "228"}
                flavor_kw = ("сугор", "лука", "сиротск", "дом сугора")
                if (
                    post_farm
                    and not self.awaiting_quest_turnin
                    and not self.need_quest_unlock
                    and (
                        str(item.npc_id) in {"121", "132"}
                        or any(kw in name.lower() for kw in flavor_kw)
                    )
                ):
                    nscore = 40
                options.append(GameOption(
                    ActionType.QUEST_NPC,
                    f"NPC: {name}",
                    score=nscore,
                    detail=f"локальный id={item.npc_id}",
                    payload={
                        "npc_id": str(item.npc_id),
                        "global_npc": 0,
                        "link_id": str(item.link_id or "0"),
                        "f_id": str(item.f_id or "0"),
                        "area_id": str(area.area_id or "0"),
                        "href": str(item.href or ""),
                    },
                    goal=GoalKind.QUEST,
                ))
                continue

            if item.code == "COME_IN" and item.area_id and farm.auto_travel:
                travel_title = f"Переход: {name}"
                low = name.lower()
                village_exit = (
                    str(item.area_id) in {"192", "100"}
                    or any(kw in low for kw in ("сопк", "дымн"))
                )
                if village_exit and self.village_exit_blocked():
                    left = max(0, int(self.village_exit_blocked_until - time.time()))
                    options.append(GameOption(
                        ActionType.IDLE,
                        f"КД: {travel_title}",
                        score=5,
                        detail=f"военачальник · через {left}с",
                        goal=GoalKind.IDLE,
                    ))
                    continue
                cd_until = self._cooldowns.get(travel_title, 0)
                if cd_until and time.time() < cd_until:
                    left = max(0, int(cd_until - time.time()))
                    options.append(GameOption(
                        ActionType.IDLE,
                        f"КД: {travel_title}",
                        score=5,
                        detail=f"через {left}с",
                        goal=GoalKind.IDLE,
                    ))
                    continue
                # Travel wins when local farm is empty / farm-push active
                score = 450 if max_farm else 400
                if self.farm_push_active() or empty_bag:
                    score = 680 if max_farm else 620
                # Prefer named farm exits (Дымные сопки и т.п.) — but not when gated
                if any(kw in low for kw in ("сопк", "дымн", "охот", "лес", "поле", "дорог")):
                    score += 40
                if village_exit and self.village_exit_blocked():
                    score = 5
                options.append(GameOption(
                    ActionType.TRAVEL,
                    travel_title,
                    score=score,
                    detail=f"→ area {item.area_id}",
                    payload={"area_id": item.area_id, "code": item.code, "name": name},
                    goal=GoalKind.TRAVEL,
                ))
                continue

            if item.action_id and (farm.farm_area or farm.auto_loot):
                # Respect server cooldown (dtime) + client-side CD after empty clicks
                cd_until = self._cooldowns.get(name, 0)
                if item.on_cooldown or (cd_until and time.time() < cd_until):
                    left = item.cooldown_left or max(0, int(cd_until - time.time()))
                    options.append(GameOption(
                        ActionType.IDLE,
                        f"КД: {name}",
                        score=5,
                        detail=f"через {left}с",
                        goal=GoalKind.IDLE,
                    ))
                    continue
                # Hotspots like Расселина — XP + loot + quest progress
                combatish = any(
                    kw in name.lower()
                    for kw in ("расселин", "охот", "пещер", "тренир", "бой", "арена", "логово")
                )
                streak = self.empty_streak(name)
                # Empty bag → loot/farm points outrank stuck dialogues
                if empty_bag and farm.auto_loot:
                    score = 795 if combatish else 740
                elif combatish:
                    score = 720 if max_farm else 700
                else:
                    score = 580 if farm.auto_loot else 550
                # After repeated empties, stop picking this hotspot
                if streak >= self.EMPTY_FARM_ESCALATE:
                    score = 80
                options.append(GameOption(
                    ActionType.COMBAT_AREA if combatish else ActionType.AREA_ACTION,
                    f"Точка: {name}",
                    score=score,
                    detail=f"action_id={item.action_id}",
                    payload={
                        "object_id": item.object_id or area.area_id,
                        "action_id": item.action_id,
                        "link_id": item.link_id,
                        "object_class": item.object_class or "AREA",
                        "name": name,
                        "link_href": item.link_href,
                        "ltime": item.ltime,
                        "dtime": item.dtime,
                    },
                    goal=GoalKind.LOOT if empty_bag else (
                        GoalKind.COMBAT if combatish else GoalKind.LOOT
                    ),
                ))

        # --- Arena / fronts / events ---
        if farm.auto_combat and farm.farm_arena:
            for n in npcs or []:
                title = str(n.get("title", ""))
                if "арен" not in title.lower():
                    continue
                time_left = int(n.get("time_left", 0) or 0)
                if time_left > self.ARENA_READY_SEC:
                    # Do NOT select arena with 40–50 min CD — that was the stuck loop
                    options.append(GameOption(
                        ActionType.IDLE,
                        f"КД арены: {title}",
                        score=5,
                        detail=f"⏱{time_left}с",
                        payload={"npc_id": n.get("npc_id"), "time_left": time_left},
                        goal=GoalKind.IDLE,
                    ))
                    continue
                options.append(GameOption(
                    ActionType.COMBAT_ARENA,
                    f"Арена: {title}",
                    score=500 if max_farm else 480,
                    detail=f"⏱{time_left}с",
                    payload={
                        "npc_id": n.get("npc_id"),
                        "url": n.get("url", ""),
                        "time_left": time_left,
                    },
                    goal=GoalKind.EVENT,
                ))

        # Hunt farm — ONLY when quest type=2 needs a kill, or light XP between story beats.
        # Endless Крэтс farming is NOT the goal — story → kill objective → turn-in → loot → gear.
        if farm.auto_combat and farm.farm_area:
            lvl = int(getattr(char, "level", 1) or 1)
            default_mob = ""
            try:
                from dwar_bot.modules.suis_knowledge import (
                    default_hunt_mob,
                    village_hunt_mob,
                )
                area_id_str = str(getattr(state, "area_id", "") or "")
                if area_id_str in {"930", "931", "932"}:
                    default_mob = village_hunt_mob(lvl)
                else:
                    default_mob = default_hunt_mob(lvl)
            except Exception:
                default_mob = "Крэтс"
            mob = self.pending_hunt_mob or (
                default_mob if self.need_quest_unlock else ""
            )
            # Open farm (max_farm / farm_push / empty bag): pin level-appropriate mob
            if not mob and (self.farm_push_active() or empty_bag or max_farm):
                mob = default_mob
            if self.awaiting_quest_turnin:
                # Already killed — do NOT hunt again until NPC dialogue advances
                hunt_score = 80
            elif self.need_quest_unlock and mob:
                hunt_score = 980  # one required kill for «Проба сил» etc.
            elif mob and self.pending_hunt_mob:
                hunt_score = 700
            elif self.farm_push_active() or empty_bag:
                hunt_score = 420  # light farm, never above story NPC (~800+)
            elif max_farm and lvl >= 3:
                hunt_score = 480  # Lv3+ intentional open farm
            else:
                hunt_score = 320
            # Cap spam: after several hunts without turn-in, demote hard
            if self._hunt_streak >= 2 and not self.need_quest_unlock:
                hunt_score = min(hunt_score, 200 if lvl < 3 else 360)
            if self._hunt_streak >= 1 and self.awaiting_quest_turnin:
                hunt_score = 40
            options.append(GameOption(
                ActionType.HUNT_MOB,
                f"Охота: {mob or 'любой моб'}",
                score=hunt_score,
                detail=(
                    "квестовое убийство (type=2)"
                    if (
                        self.pending_hunt_mob
                        and not self.awaiting_quest_turnin
                    )
                    else f"hunt_farm lv{lvl} — {mob or 'any'}"
                ),
                payload={"name": mob, "mob_name": mob, "area_id": str(state.area_id or "")},
                goal=GoalKind.COMBAT,
            ))

        if farm.auto_combat and farm.farm_fronts:
            front_score = 440 if max_farm else 420
            if self.farm_push_active() or empty_bag:
                front_score = 560 if max_farm else 500
            if self.need_quest_unlock or self.awaiting_quest_turnin:
                front_score = 40
            options.append(GameOption(
                ActionType.COMBAT_FRONT,
                "Искать фронт / PvP",
                score=front_score,
                detail="front|locations",
                goal=GoalKind.COMBAT,
            ))

        # --- Buff ---
        options.append(GameOption(
            ActionType.BUFF,
            "Показать эффекты / бафф новичка",
            score=120,
            detail="EFFECT_SHOW",
            goal=GoalKind.BUFF,
        ))

        # Always have idle fallback
        options.append(GameOption(
            ActionType.IDLE,
            "Ждать / реген",
            score=10,
            detail="нет выгодных действий",
            goal=GoalKind.IDLE,
        ))

        # Filter by master switches
        options = self._filter_by_settings(options)
        for o in options:
            o.score = max(1.0, o.score - self._stale_penalty(o))

        # Farm-push: leave village / find fights — but NEVER outrank an active
        # quest kill turn-in. Story first: NPC → required kill → turn-in → travel.
        farm_mode = self.farm_push_active() or any(
            v >= self.EMPTY_FARM_ESCALATE for v in self._stale.values()
        )
        if farm_mode and not self.awaiting_quest_turnin:
            for o in options:
                if o.action == ActionType.HUNT_MOB:
                    if self.need_quest_unlock and not self.awaiting_quest_turnin:
                        o.score += 50.0  # mild boost for the one required kill
                    else:
                        o.score += 40.0
                elif o.action == ActionType.TRAVEL:
                    if self.need_quest_unlock:
                        o.score = max(1.0, o.score - 100.0)
                    else:
                        o.score += 150.0
                elif o.action == ActionType.COMBAT_FRONT:
                    if self.need_quest_unlock:
                        o.score = min(o.score, 50.0)
                    else:
                        o.score += 80.0
                elif o.action == ActionType.COMBAT_AREA and o.score > 100:
                    name = str((o.payload or {}).get("name") or "")
                    if self.empty_streak(name) >= 1:
                        o.score = min(o.score, 90.0)
                    else:
                        o.score = max(1.0, o.score - 40.0)
                elif o.action == ActionType.QUEST_NPC:
                    if self.need_quest_unlock and not str(
                        (o.payload or {}).get("global_npc", 0)
                    ) in ("1",):
                        o.score += 100.0
                    # Do not demote local story NPCs during farm-push
                elif o.action in (ActionType.EQUIP, ActionType.REPAIR):
                    # Gear always beats casual farm under farm-push
                    o.score = max(float(o.score), 930.0 if o.action == ActionType.EQUIP else 910.0)
        elif self.awaiting_quest_turnin:
            for o in options:
                if o.action == ActionType.QUEST_NPC and not str(
                    (o.payload or {}).get("global_npc", 0)
                ) in ("1",):
                    o.score = max(o.score, 1250.0)
                elif o.action == ActionType.HUNT_MOB:
                    o.score = min(o.score, 60.0)

        options.sort(key=lambda o: -o.score)
        focus = options[0] if options else None

        plan = self._build_plan(profile, area, bottlenecks, focus, farm_mode=farm_mode)
        now = self._describe_now(char, state, area, focus, in_battle)

        snap = ProgressSnapshot(
            now=now,
            now_action=focus,
            plan=plan,
            options=options,
            focus=focus,
            power_score=power,
            bottlenecks=bottlenecks,
            tips=tips,
        )
        self.last = snap
        if focus:
            self._action_history.append(f"{focus.action.value}:{focus.title}")
            self._action_history = self._action_history[-40:]
        return snap

    # ------------------------------------------------------------------
    def _filter_by_settings(self, options: list[GameOption]) -> list[GameOption]:
        farm = self.settings.farm
        out = []
        for o in options:
            if o.action == ActionType.QUEST_NPC and not farm.auto_quests:
                continue
            if o.action in (
                ActionType.COMBAT_AREA, ActionType.COMBAT_ARENA,
                ActionType.COMBAT_FRONT, ActionType.HUNT_MOB,
            ) and not farm.auto_combat:
                continue
            if o.action == ActionType.COMBAT_FRONT and not farm.farm_fronts:
                continue
            if o.action == ActionType.COMBAT_ARENA and not farm.farm_arena:
                continue
            if o.action == ActionType.HUNT_MOB and not farm.farm_area:
                continue
            if o.action == ActionType.COMBAT_AREA and not farm.farm_area and not farm.auto_loot:
                continue
            if o.action == ActionType.AREA_ACTION and not farm.auto_loot and not farm.farm_area:
                continue
            if o.action == ActionType.TRAVEL and not farm.auto_travel:
                continue
            if o.action == ActionType.REPAIR and not farm.auto_repair:
                continue
            if o.action == ActionType.EQUIP and not farm.auto_equip:
                continue
            if o.action == ActionType.HEAL and not farm.auto_heal:
                continue
            out.append(o)
        return out or options[-1:]

    def _power_score(self, profile: FullProfile, area: AreaInfo) -> float:
        char = profile.char
        score = 0.0
        score += min(40.0, char.level * 8.0)
        score += min(15.0, profile.state.money * 2.0)
        score += min(20.0, len(profile.equipment) * 4.0)
        score += min(10.0, len(profile.potions) * 3.0)
        if char.hp_max:
            score += char.hp_percent * 0.1
        if profile.broken_items:
            score -= len(profile.broken_items) * 3
        # Stuck in newbie village without gear
        if char.level <= 1 and not profile.inventory:
            score = min(score, 12.0)
        return max(0.0, min(100.0, score))

    def _bottlenecks(self, profile: FullProfile, area: AreaInfo) -> list[str]:
        out = []
        if not profile.inventory:
            out.append("Пустой рюкзак — нужен лут с точек/квестов")
        if profile.char.level <= 1:
            out.append("Уровень 1 — приоритет сюжету и боям в деревне")
        if any(i.code == "COME_IN" for i in area.items):
            # travel exists but often gated
            out.append("Переходы есть, но могут быть закрыты военачальником")
        if profile.char.mp_max == 0:
            out.append("Нет маны — упор в физ. бой / отвары")
        if profile.state.money < 10:
            out.append("Мало золота — фарм мобов и квестовые награды")
        return out

    def _tips(self, profile: FullProfile, area: AreaInfo, bottlenecks: list[str]) -> list[str]:
        tips = [
            "Сюжетные NPC (Вождь) открывают карту и следующие бои",
            "Точки вроде «Расселина» дают лут и продвигают «Пробу сил»",
            "Держи HP выше порога retreat, чини экипировку сразу",
            "Арена/события — когда сюжет не блокирует",
        ]
        if not profile.inventory:
            tips.insert(0, "Собери первую награду с локации / квеста — без вещей рост медленный")
        return tips[:5]

    def _build_plan(
        self,
        profile: FullProfile,
        area: AreaInfo,
        bottlenecks: list[str],
        focus: Optional[GameOption],
        *,
        farm_mode: bool = False,
    ) -> list[PlanStep]:
        steps: list[PlanStep] = []
        if focus and focus.goal == GoalKind.SURVIVE:
            steps.append(PlanStep(GoalKind.SURVIVE, "Восстановить HP", "иначе смерть / простой"))
        if farm_mode or self.farm_push_active():
            # Balanced loop: gear → story check → farm → travel (not endless hunt)
            steps.append(PlanStep(GoalKind.GEAR, "Надеть и чинить добычу", "сила растёт от экипа"))
            steps.append(PlanStep(GoalKind.QUEST, "Проверить сюжет / Вождя", "приказ / сдача / новые цели"))
            steps.append(PlanStep(GoalKind.COMBAT, "Фарм между квестами", "XP и лут, не вместо сюжета"))
            steps.append(PlanStep(GoalKind.TRAVEL, "Сменить локацию", "когда военачальник разрешит"))
        else:
            steps.append(PlanStep(GoalKind.QUEST, "Сюжет / диалоги Вождя", "открывает переходы и новые цели"))
            if self.awaiting_quest_turnin:
                steps.insert(0, PlanStep(GoalKind.QUEST, "Сдать убийство Вождю", "type=2 после охоты"))
            elif self.need_quest_unlock and self.pending_hunt_mob:
                steps.insert(0, PlanStep(GoalKind.COMBAT, f"Убить {self.pending_hunt_mob} по квесту", "одно убийство, не фарм"))
            steps.append(PlanStep(GoalKind.COMBAT, "Мобы / точки по сюжету", "XP и лут между этапами"))
            steps.append(PlanStep(GoalKind.GEAR, "Надеть и чинить добычу", "сила растёт от экипа"))
            steps.append(PlanStep(GoalKind.TRAVEL, "Выйти в Дымные сопки", "когда военачальник разрешит"))
        steps.append(PlanStep(GoalKind.EVENT, "Арена / ивенты", "только когда таймер готов"))
        steps.append(PlanStep(GoalKind.COMBAT, "Фарм уровней и золота", "после сюжета / между квестами"))
        return steps

    def _describe_now(
        self,
        char: CharStats,
        state: GameState,
        area: AreaInfo,
        focus: Optional[GameOption],
        in_battle: bool,
    ) -> str:
        where = getattr(area, "title", None) or f"area {state.area_id}"
        if not isinstance(where, str):
            where = f"area {state.area_id}"
        base = f"{char.nick or '?'} Lv{char.level} @ {where} · HP {char.hp}/{char.hp_max}"
        if in_battle:
            return f"{base} · ⚔️ в бою"
        if focus:
            return f"{base} · → {focus.title}"
        return f"{base} · ожидание"

    def history_tail(self, n: int = 8) -> list[str]:
        return self._action_history[-n:]
