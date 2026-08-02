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
    TRAVEL = "travel"
    AREA_ACTION = "area_action"
    BUFF = "buff"
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

    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings
        self.last = ProgressSnapshot()
        self._action_history: list[str] = []
        self._loot_seen: set[str] = set()
        # action_key → consecutive empty/no-progress results
        self._stale: dict[str, int] = {}

    def note_result(self, option: Optional[GameOption], *, progressed: bool) -> None:
        """Feed back whether the chosen action produced progress (loot/fight/dialog)."""
        if not option:
            return
        key = f"{option.action.value}:{option.title}"
        if progressed:
            self._stale.pop(key, None)
        else:
            self._stale[key] = self._stale.get(key, 0) + 1

    def _stale_penalty(self, option: GameOption) -> float:
        key = f"{option.action.value}:{option.title}"
        n = self._stale.get(key, 0)
        if n <= 0:
            return 0.0
        # After a few empty hotspot ticks, prefer other goals (NPC / travel / fronts)
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
                ActionType.COMBAT_AREA,
                "Продолжить бой",
                score=900,
                detail="уже в схватке",
                goal=GoalKind.COMBAT,
            ))

        # --- Gear ---
        if farm.auto_repair and profile.broken_items:
            options.append(GameOption(
                ActionType.REPAIR,
                f"Ремонт ×{len(profile.broken_items)}",
                score=850,
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
                score=820,
                detail=unequipped[0].title,
                goal=GoalKind.GEAR,
            ))

        # --- Story / quests ---
        if farm.auto_quests and story_npc and story_npc.get("npc_id"):
            sid = str(story_npc["npc_id"])
            if sid not in exhausted:
                options.append(GameOption(
                    ActionType.QUEST_NPC,
                    f"Сюжетный NPC #{sid}",
                    score=780,
                    detail="сервер указал следующего NPC",
                    payload={
                        "npc_id": sid,
                        "global_npc": int(story_npc.get("global_npc", 1) or 1),
                        "link_id": str(story_npc.get("link_id") or "0"),
                        "f_id": str(story_npc.get("f_id") or "0"),
                        "area_id": str(story_npc.get("area_id") or area.area_id or "0"),
                    },
                    goal=GoalKind.QUEST,
                ))

        for npc in (local_npcs or []):
            if not farm.auto_quests:
                break
            if str(npc.npc_id) in exhausted:
                continue
            options.append(GameOption(
                ActionType.QUEST_NPC,
                f"NPC: {npc.title}",
                score=760 if not npc.is_global else 520,
                detail=f"id={npc.npc_id}",
                payload={
                    "npc_id": str(npc.npc_id),
                    "global_npc": 1 if npc.is_global else 0,
                    "link_id": str(npc.link_id or "0"),
                    "f_id": str(npc.f_id or "0"),
                    "area_id": str(npc.area_id or area.area_id or "0"),
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
                options.append(GameOption(
                    ActionType.QUEST_NPC,
                    f"NPC: {name}",
                    score=770,
                    detail=f"локальный id={item.npc_id}",
                    payload={
                        "npc_id": str(item.npc_id),
                        "global_npc": 0,
                        "link_id": str(item.link_id or "0"),
                        "f_id": str(item.f_id or "0"),
                        "area_id": str(area.area_id or "0"),
                    },
                    goal=GoalKind.QUEST,
                ))
                continue

            if item.code == "COME_IN" and item.area_id and farm.auto_travel:
                # Travel is often gated — try occasionally, prefer when max_farm
                options.append(GameOption(
                    ActionType.TRAVEL,
                    f"Переход: {name}",
                    score=450 if max_farm else 400,
                    detail=f"→ area {item.area_id}",
                    payload={"area_id": item.area_id, "code": item.code},
                    goal=GoalKind.TRAVEL,
                ))
                continue

            if item.action_id and (farm.farm_area or farm.auto_loot):
                # Hotspots like Расселина — XP + loot + quest progress
                combatish = any(
                    kw in name.lower()
                    for kw in ("расселин", "охот", "пещер", "тренир", "бой", "арена", "логово")
                )
                # Empty bag → loot/farm points outrank stuck dialogues
                if empty_bag and farm.auto_loot:
                    score = 795 if combatish else 740
                elif combatish:
                    score = 720 if max_farm else 700
                else:
                    score = 580 if farm.auto_loot else 550
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
                    },
                    goal=GoalKind.LOOT if empty_bag else (
                        GoalKind.COMBAT if combatish else GoalKind.LOOT
                    ),
                ))

        # --- Arena / fronts / events ---
        if farm.auto_combat and farm.farm_arena:
            for n in npcs or []:
                title = str(n.get("title", ""))
                if "арен" in title.lower():
                    options.append(GameOption(
                        ActionType.COMBAT_ARENA,
                        f"Арена: {title}",
                        score=500 if max_farm else 480,
                        detail=f"⏱{n.get('time_left', 0)}с",
                        payload={"npc_id": n.get("npc_id"), "url": n.get("url", "")},
                        goal=GoalKind.EVENT,
                    ))

        if farm.auto_combat and farm.farm_fronts:
            options.append(GameOption(
                ActionType.COMBAT_FRONT,
                "Искать фронт / PvP",
                score=440 if max_farm else 420,
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
        # Periodic quest nudge: every 4th stale hotspot cycle, boost story NPC
        if any(self._stale.get(k, 0) >= 2 for k in self._stale):
            for o in options:
                if o.action == ActionType.QUEST_NPC:
                    o.score += 40
        options.sort(key=lambda o: -o.score)
        focus = options[0] if options else None

        plan = self._build_plan(profile, area, bottlenecks, focus)
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
            if o.action in (ActionType.COMBAT_AREA, ActionType.COMBAT_ARENA, ActionType.COMBAT_FRONT) and not farm.auto_combat:
                continue
            if o.action == ActionType.COMBAT_FRONT and not farm.farm_fronts:
                continue
            if o.action == ActionType.COMBAT_ARENA and not farm.farm_arena:
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
    ) -> list[PlanStep]:
        steps: list[PlanStep] = []
        if focus and focus.goal == GoalKind.SURVIVE:
            steps.append(PlanStep(GoalKind.SURVIVE, "Восстановить HP", "иначе смерть / простой"))
        steps.append(PlanStep(GoalKind.QUEST, "Закрыть диалоги Вождя / сюжет", "открывает переходы и бои"))
        steps.append(PlanStep(GoalKind.COMBAT, "Фарм точки Расселина / мобы", "XP + лут + прогресс «Проба сил»"))
        steps.append(PlanStep(GoalKind.GEAR, "Надеть и чинить добычу", "сила растёт от экипа"))
        steps.append(PlanStep(GoalKind.TRAVEL, "Выйти в Дымные сопки", "когда военачальник разрешит"))
        steps.append(PlanStep(GoalKind.EVENT, "Арена / ивенты", "доп. награды и опыт"))
        steps.append(PlanStep(GoalKind.COMBAT, "Фарм уровней и золота", "максимальный рост персонажа"))
        return steps

    def _describe_now(
        self,
        char: CharStats,
        state: GameState,
        area: AreaInfo,
        focus: Optional[GameOption],
        in_battle: bool,
    ) -> str:
        where = area.title or f"area {state.area_id}"
        base = f"{char.nick or '?'} Lv{char.level} @ {where} · HP {char.hp}/{char.hp_max}"
        if in_battle:
            return f"{base} · ⚔️ в бою"
        if focus:
            return f"{base} · → {focus.title}"
        return f"{base} · ожидание"

    def history_tail(self, n: int = 8) -> list[str]:
        return self._action_history[-n:]
