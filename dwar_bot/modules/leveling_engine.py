"""
LevelingEngine — Level-Up Decision Tree + Exp booster / idle multitasking.

Priority ladder
---------------
1. Urgent quests (exp/valor doable at current level) — shortest path
2. High-efficiency farm (max Exp/Min for gear-appropriate mobs)
3. Reputation / valor when XP is capped — caves / arena / rep quests

Also:
* Presses Exp boosters before fight series
* Tracks daily Exp-hour events via GameKnowledgeBase
* During HP regen windows runs mail / quest turn-in / auction / craft stubs
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from dwar_bot.core.bot_state import BotState
from dwar_bot.core.game_knowledge_base import (
    EfficiencyRow,
    GameKnowledgeBase,
    QuestRecord,
    get_knowledge_base,
)
from dwar_bot.core.master_controller import MasterController, StrategicDirective
from dwar_bot.modules.progression_brain import ActionType, GameOption, GoalKind
from dwar_bot.modules.stats_parser import FullProfile

logger = logging.getLogger(__name__)

# Keywords that look like Exp boosters in effect / inventory titles
_EXP_BUFF_MARKERS = (
    "опыт", "exp", "experience", "премиум", "premium",
    "благослов", "эликсир опыта", "свиток опыта",
    "часа опыта", "x2", "×2", "x1.5",
)

_FOOD_BUFF_MARKERS = (
    "еда", "пища", "рацион", "похлёб", "похлеб", "зелье",
)


@dataclass
class LevelProgress:
    level: int = 0
    exp_pct: float = 0.0          # 0..100 within current level (estimated)
    exp_per_hour: float = 0.0
    eta_seconds: float = 0.0
    priority_title: str = ""
    mode: str = "idle"
    wins: int = 0
    quests_done: int = 0
    updated_at: float = field(default_factory=time.time)

    def telegram_html(self) -> str:
        eta = self._fmt_eta(self.eta_seconds)
        return (
            "📈 <b>Level-Up Update:</b>\n"
            f"- Уровень: {self.level} ({self.exp_pct:.1f}%)\n"
            f"- Скорость кача: +{self.exp_per_hour:,.0f} Exp/час\n"
            f"- До следующего уровня: ~{eta}\n"
            f"- Текущий приоритет: {self.priority_title or '—'}"
        ).replace(",", " ")

    @staticmethod
    def _fmt_eta(sec: float) -> str:
        if sec <= 0 or sec == float("inf"):
            return "н/д"
        sec = int(sec)
        h, rem = divmod(sec, 3600)
        m, _ = divmod(rem, 60)
        if h:
            return f"{h} ч. {m} мин."
        return f"{m} мин."


@dataclass
class LevelingDecision:
    """Result of one decision-tree evaluation."""

    directive: StrategicDirective
    focus_override: Optional[GameOption] = None
    boost_needed: bool = False
    idle_tasks: list[str] = field(default_factory=list)
    progress: LevelProgress = field(default_factory=LevelProgress)
    notes: list[str] = field(default_factory=list)


class LevelingEngine:
    """
    Strategic Level-Up manager.

    Call ``observe()`` after each tick's world sense, then ``decide()`` to
    produce a directive for ``MasterController`` and optional focus override
    for ``DwarBot._execute_focus``.
    """

    REPORT_INTERVAL_SEC = 900.0  # Level-Up Update every 15 minutes
    XP_CAP_IDLE_WINS = 8         # wins without level% progress → treat as soft cap

    def __init__(
        self,
        knowledge: Optional[GameKnowledgeBase] = None,
        controller: Optional[MasterController] = None,
        *,
        account_id: str = "",
    ) -> None:
        self.kb = knowledge or get_knowledge_base()
        self.controller = controller
        self.account_id = account_id

        self.progress = LevelProgress()
        self._session_started = time.time()
        self._wins = 0
        self._quest_turns = 0
        self._exp_samples: list[tuple[float, float]] = []  # (ts, cumulative_proxy)
        self._proxy_exp = 0.0
        self._last_level = 0
        self._last_report_at = 0.0
        self._last_boost_at = 0.0
        self._idle_cursor = 0
        self._no_level_wins = 0
        self._last_focus_title = ""

        # Seed known daily windows (adjustable via KB upsert_event)
        self.kb.upsert_event(
            event_key="hours_of_exp",
            title="Часы опыта",
            kind="daily",
            starts_hour=18,
            ends_hour=21,
            bonus_mult=1.5,
            meta={"hint": "prefer farm during window"},
        )

    # ------------------------------------------------------------------
    # Observation / learning
    # ------------------------------------------------------------------

    def observe_world(
        self,
        *,
        profile: FullProfile,
        area_id: str = "",
        area_title: str = "",
        hunt_bots: Optional[list[dict]] = None,
        npc_quests: Any = None,
        npc_id: str = "",
        event_timers: Optional[list[Any]] = None,
    ) -> None:
        """Ingest live world facts into the knowledge base (24/7 learning)."""
        char = profile.char
        if area_id:
            self.kb.touch_area_title(area_id, area_title or area_id)
        if hunt_bots:
            n = self.kb.ingest_hunt_bots(hunt_bots, area_id=area_id)
            if n:
                logger.debug("KB: ingested %d hunt bots @%s", n, area_id)
        if npc_quests is not None:
            self.kb.ingest_npc_quests(
                npc_quests,
                npc_id=npc_id,
                area_id=area_id,
                char_level=char.level,
            )
        # Track level % proxy from level-ups
        if self._last_level and char.level > self._last_level:
            self._proxy_exp += 1000.0 * (char.level)  # level-up burst
            self._no_level_wins = 0
            self.progress.exp_pct = 5.0
        self._last_level = char.level or self._last_level
        self.progress.level = char.level
        self._note_exp_sample()

        # Parse event timers into KB if they look like Exp windows
        for ev in event_timers or []:
            title = ""
            if isinstance(ev, dict):
                title = str(ev.get("title") or ev.get("name") or "")
            else:
                title = str(getattr(ev, "title", "") or getattr(ev, "name", "") or ev)
            low = title.lower()
            if any(m in low for m in ("опыт", "exp", "x2", "час")):
                self.kb.upsert_event(
                    event_key=f"live:{title[:40]}",
                    title=title[:80],
                    kind="live",
                    starts_hour=time.localtime().tm_hour,
                    ends_hour=(time.localtime().tm_hour + 1) % 24,
                    bonus_mult=1.5 if "x2" in low or "×2" in low else 1.25,
                )

    def note_kill(
        self,
        *,
        mob_id: str = "",
        mob_name: str = "",
        area_id: str = "",
        fight_sec: float = 35.0,
        gold_spent: float = 0.0,
        level: int = 0,
        loot: Optional[list[str]] = None,
    ) -> None:
        # Proxy exp: scale with mob level (refined when real exp is known)
        exp_guess = 40.0 * max(1, level or self.progress.level or 1)
        mult = self.kb.active_exp_multiplier()
        exp_guess *= mult
        self.kb.record_kill(
            mob_id=mob_id or mob_name,
            name=mob_name or mob_id,
            area_id=area_id,
            fight_sec=fight_sec,
            exp_gained=exp_guess,
            gold_spent=gold_spent,
            level=level,
            loot=loot,
        )
        self._wins += 1
        self._proxy_exp += exp_guess
        self._no_level_wins += 1
        self.progress.wins = self._wins
        self.progress.exp_pct = min(99.5, self.progress.exp_pct + max(0.4, exp_guess / 80.0))
        self._note_exp_sample()

    def note_quest_progress(self, *, title: str = "", exp_reward: float = 0.0) -> None:
        self._quest_turns += 1
        self.progress.quests_done = self._quest_turns
        if exp_reward > 0:
            self._proxy_exp += exp_reward
            self.progress.exp_pct = min(99.5, self.progress.exp_pct + exp_reward / 50.0)
        elif title:
            self.progress.exp_pct = min(99.5, self.progress.exp_pct + 2.0)
        self._no_level_wins = 0
        self._note_exp_sample()

    def _note_exp_sample(self) -> None:
        now = time.time()
        self._exp_samples.append((now, self._proxy_exp))
        # Keep ~2h window
        cut = now - 7200
        self._exp_samples = [s for s in self._exp_samples if s[0] >= cut]
        eph = self._calc_exp_per_hour()
        self.progress.exp_per_hour = eph
        remaining = max(0.0, 100.0 - self.progress.exp_pct)
        if eph > 1:
            # Map % → rough exp units: assume 100% ≈ 800 * level
            level_bucket = 800.0 * max(1, self.progress.level)
            need = remaining / 100.0 * level_bucket
            self.progress.eta_seconds = need / eph * 3600.0
        else:
            self.progress.eta_seconds = 0.0
        self.progress.updated_at = now

    def _calc_exp_per_hour(self) -> float:
        if len(self._exp_samples) < 2:
            # Bootstrap from KB best farm
            best = self.kb.best_farm_target(char_level=self.progress.level or 1)
            if best and best.exp_per_min > 0:
                return best.exp_per_min * 60.0 * self.kb.active_exp_multiplier()
            return 0.0
        t0, e0 = self._exp_samples[0]
        t1, e1 = self._exp_samples[-1]
        dt = max(1.0, t1 - t0)
        return max(0.0, (e1 - e0) / dt * 3600.0)

    # ------------------------------------------------------------------
    # Decision tree
    # ------------------------------------------------------------------

    def decide(
        self,
        *,
        profile: FullProfile,
        brain_focus: Optional[GameOption],
        brain_options: Optional[list[GameOption]] = None,
        area_id: str = "",
        pending_hunt_mob: str = "",
        awaiting_turnin: bool = False,
        need_quest_unlock: bool = False,
        in_battle: bool = False,
        world_objective_kind: str = "",
        world_objective_flash_only: bool = False,
        blocked_npc_ids: Optional[set[str]] = None,
    ) -> LevelingDecision:
        char = profile.char
        level = char.level or self.progress.level
        self.progress.level = level
        options = list(brain_options or [])
        notes: list[str] = []
        blocked = blocked_npc_ids or set()

        # Soft XP cap detection
        xp_capped = self._no_level_wins >= self.XP_CAP_IDLE_WINS and level >= 2

        # World objective (heal wounded etc.) — never force story-NPC re-entry
        if world_objective_kind:
            notes.append(f"world_obj={world_objective_kind}")
            awaiting_turnin = False
            need_quest_unlock = False
            pending_hunt_mob = ""
            # Drop junk global NPC options; Flash-only → no Расселина spam
            filtered: list[GameOption] = []
            for o in options:
                title_l = (o.title or "").lower()
                npc = str((o.payload or {}).get("npc_id") or "")
                if o.action == ActionType.QUEST_NPC and (
                    npc in {"816", "817"}
                    or "летопис" in title_l
                    or "сезонн" in title_l
                    or "арену" in title_l
                    or "зной" in title_l
                ):
                    continue
                if world_objective_flash_only and o.action in (
                    ActionType.COMBAT_AREA,
                    ActionType.AREA_ACTION,
                ):
                    # Расселина / hotspots do not heal ополченцев — skip spam
                    continue
                if o.action == ActionType.HUNT_MOB:
                    if world_objective_flash_only:
                        # Light village farm for Exp while user clicks Flash
                        o.score = min(max(float(o.score), 200.0), 350.0)
                        o.detail = f"flash wait · hunt ok · {o.detail}"
                    else:
                        o.score = min(float(o.score), 120.0)
                        o.detail = f"world_obj cap · {o.detail}"
                if o.action == ActionType.TRAVEL and world_objective_flash_only:
                    # Village exit gated until war-chief order after heal
                    o.score = min(float(o.score), 40.0)
                    o.detail = f"blocked until heal · {o.detail}"
                if o.action == ActionType.COMBAT_FRONT and world_objective_flash_only:
                    o.score = min(float(o.score), 30.0)
                filtered.append(o)
            options = filtered
            idle_opt = next((o for o in options if o.action == ActionType.IDLE), None)
            if idle_opt is None:
                idle_opt = GameOption(
                    ActionType.IDLE,
                    title="Ждать снадобье / Flash",
                    score=150.0 if world_objective_flash_only else 50.0,
                    detail="heal_wounded flash-only — no Расселина spam",
                    goal=GoalKind.IDLE,
                )
            elif world_objective_flash_only:
                idle_opt.score = max(float(idle_opt.score), 150.0)
                idle_opt.title = "Ждать снадобье / Flash"
                idle_opt.detail = "HTTP USE недоступен — охота или пауза"
            hunt_opt = None
            if world_objective_flash_only:
                hunt_candidates = [
                    o for o in options if o.action == ActionType.HUNT_MOB
                ]
                if hunt_candidates:
                    hunt_opt = max(hunt_candidates, key=lambda o: float(o.score))
            # Flash-only: hunt for Exp if available, else idle — NEVER Расселина
            if world_objective_flash_only:
                focus_wo = hunt_opt or idle_opt
            else:
                area_opt = next(
                    (
                        o for o in options
                        if o.action in (
                            ActionType.COMBAT_AREA,
                            ActionType.AREA_ACTION,
                            ActionType.USE_ITEM,
                        )
                        and float(o.score) >= 50
                    ),
                    None,
                )
                focus_wo = area_opt or idle_opt
            directive = StrategicDirective(
                state=BotState.FARMING,
                priority=2,
                title=f"Мир-цель: {world_objective_kind}",
                reason=(
                    f"P2-world: {world_objective_kind}"
                    + (
                        " FLASH-hunt"
                        if world_objective_flash_only and hunt_opt
                        else (
                            " FLASH-idle"
                            if world_objective_flash_only
                            else " capped"
                        )
                    )
                ),
                area_id=area_id,
                exp_per_hour=self.progress.exp_per_hour,
            )
            self.progress.priority_title = directive.title
            self.progress.mode = "world_objective"
            return LevelingDecision(
                directive=directive,
                focus_override=focus_wo,
                progress=self.progress,
                notes=notes + [
                    "priority=WORLD_OBJECTIVE",
                    "flash_only" if world_objective_flash_only else "http",
                    "flash_hunt" if (world_objective_flash_only and hunt_opt) else "",
                ],
            )

        # --- Priority 1: urgent / doable quests ---
        quest_opt = self._pick_quest_option(
            options,
            level=level,
            pending_hunt_mob=pending_hunt_mob,
            awaiting_turnin=awaiting_turnin,
            need_quest_unlock=need_quest_unlock,
            blocked_npc_ids=blocked,
        )
        if world_objective_kind:
            quest_opt = None  # hard-skip P1 while world goal is open
        urgent_quest = None if world_objective_kind else self._best_kb_quest(
            level=level, area_id=area_id,
        )

        if quest_opt or (urgent_quest and (need_quest_unlock or awaiting_turnin or pending_hunt_mob)):
            title = (
                (quest_opt.title if quest_opt else "")
                or (urgent_quest.title if urgent_quest else "")
                or pending_hunt_mob
                or "Сюжетный квест"
            )
            focus = quest_opt
            if not focus and pending_hunt_mob:
                focus = GameOption(
                    ActionType.HUNT_MOB,
                    title=f"Квест-охота: {pending_hunt_mob}",
                    score=950,
                    detail="P1 Level-Up: kill gate",
                    payload={"mob_name": pending_hunt_mob},
                    goal=GoalKind.QUEST,
                )
            elif not focus and urgent_quest:
                # Skip KB quests that point at a blocked world-objective NPC
                u_npc = str(getattr(urgent_quest, "npc_id", "") or "")
                if u_npc and u_npc in blocked:
                    focus = None
                else:
                    focus = GameOption(
                        ActionType.QUEST_NPC,
                        title=f"Квест: {urgent_quest.title}",
                        score=920,
                        detail=f"Exp≈{urgent_quest.exp_reward:.0f}",
                        payload={"npc_id": urgent_quest.npc_id, "quest_key": urgent_quest.quest_key},
                        goal=GoalKind.QUEST,
                    )
            if focus:
                directive = StrategicDirective(
                    state=BotState.EXECUTING_QUEST,
                    priority=1,
                    title=title,
                    reason="P1: срочный квест с опытом/доблестью",
                    quest_title=title,
                    npc_id=(urgent_quest.npc_id if urgent_quest else ""),
                    area_id=area_id,
                    mob_name=pending_hunt_mob,
                    exp_per_hour=self.progress.exp_per_hour,
                )
                self.progress.priority_title = f"Квест '{title}'"
                self.progress.mode = "quest"
                notes.append("priority=QUEST")
                decision = LevelingDecision(
                    directive=directive,
                    focus_override=focus,
                    boost_needed=False,
                    progress=self.progress,
                    notes=notes,
                )
                return decision

        # --- Buff check before farm series ---
        boost_needed = self._needs_exp_boost(profile) and not in_battle
        if boost_needed and char.hp_percent >= 50:
            buff_opt = next((o for o in options if o.action == ActionType.BUFF), None)
            if not buff_opt:
                buff_opt = GameOption(
                    ActionType.BUFF,
                    title="Баффы опыта",
                    score=880,
                    detail="P0: Exp boosters before farm",
                    goal=GoalKind.BUFF,
                )
            directive = StrategicDirective(
                state=BotState.BUFFING,
                priority=0,
                title="Максимизация баффов опыта",
                reason="Press Exp boosters before fight series",
                area_id=area_id,
                exp_per_hour=self.progress.exp_per_hour,
            )
            self.progress.priority_title = "Баффы опыта / премиум"
            self.progress.mode = "buff"
            return LevelingDecision(
                directive=directive,
                focus_override=buff_opt,
                boost_needed=True,
                progress=self.progress,
                notes=["priority=BUFF"],
            )

        # --- Priority 3: reputation when capped ---
        if xp_capped:
            rep_opt = self._pick_reputation_option(options)
            directive = StrategicDirective(
                state=BotState.REPUTATION,
                priority=3,
                title=rep_opt.title if rep_opt else "Репутация / Арена",
                reason="P3: XP soft-cap — valor/reputation farm",
                area_id=area_id,
                exp_per_hour=self.progress.exp_per_hour,
            )
            self.progress.priority_title = directive.title
            self.progress.mode = "reputation"
            return LevelingDecision(
                directive=directive,
                focus_override=rep_opt,
                progress=self.progress,
                notes=["priority=REPUTATION", f"no_level_wins={self._no_level_wins}"],
            )

        # --- Priority 2: max Exp/Min farm ---
        best = self.kb.best_farm_target(char_level=level, area_id=area_id)
        mult = self.kb.active_exp_multiplier()
        farm_opt = self._pick_farm_option(options, best=best, brain_focus=brain_focus)
        eph = (best.exp_per_min * 60.0 * mult) if best else self.progress.exp_per_hour
        title = farm_opt.title if farm_opt else (best.name if best else "Фарм")
        directive = StrategicDirective(
            state=BotState.FARMING,
            priority=2,
            title=title,
            reason=f"P2: max Exp/Min (×{mult:.2f} event)",
            mob_id=best.key if best else "",
            mob_name=best.name if best else "",
            area_id=(best.area_id if best else area_id),
            exp_per_hour=eph or self.progress.exp_per_hour,
            payload={
                "exp_per_min": best.exp_per_min if best else 0.0,
                "exp_per_gold": best.exp_per_gold if best else 0.0,
            },
        )
        self.progress.priority_title = title
        self.progress.mode = "farm"
        if eph:
            self.progress.exp_per_hour = max(self.progress.exp_per_hour, eph)

        # Idle multitasking suggestion during low HP
        idle_tasks: list[str] = []
        if char.hp_percent < 55:
            idle_tasks = self._idle_task_queue()
            if idle_tasks:
                notes.append(f"idle_tasks={idle_tasks}")

        return LevelingDecision(
            directive=directive,
            focus_override=farm_opt if (best and farm_opt) else None,
            boost_needed=False,
            idle_tasks=idle_tasks,
            progress=self.progress,
            notes=notes + ["priority=FARM"],
        )

    def _best_kb_quest(self, *, level: int, area_id: str) -> Optional[QuestRecord]:
        quests = self.kb.list_quests(
            area_id=area_id,
            max_level=level,
            statuses=("available", "active", "ready", "seen"),
        )
        # Prefer exp/valor rich
        scored = sorted(
            quests,
            key=lambda q: (-(q.exp_reward + q.valor_reward * 50 + q.gold_reward), q.level_req),
        )
        return scored[0] if scored else None

    def _pick_quest_option(
        self,
        options: list[GameOption],
        *,
        level: int,
        pending_hunt_mob: str,
        awaiting_turnin: bool,
        need_quest_unlock: bool,
        blocked_npc_ids: Optional[set[str]] = None,
    ) -> Optional[GameOption]:
        blocked = blocked_npc_ids or set()
        quest_opts = [
            o for o in options
            if (
                o.action in (ActionType.QUEST_NPC, ActionType.QUEST_TURNIN)
                or o.goal == GoalKind.QUEST
            )
            and str((o.payload or {}).get("npc_id") or "") not in blocked
        ]
        if awaiting_turnin or need_quest_unlock:
            if quest_opts:
                return max(quest_opts, key=lambda o: o.score)
        if pending_hunt_mob:
            hunt = next(
                (o for o in options
                 if o.action == ActionType.HUNT_MOB and pending_hunt_mob.lower() in o.title.lower()),
                None,
            )
            if hunt:
                return hunt
        # High-score story NPC already preferred by brain
        if quest_opts:
            top = max(quest_opts, key=lambda o: o.score)
            if top.score >= 500 or "квест" in top.title.lower() or "вожд" in top.title.lower():
                return top
        return None

    def _pick_reputation_option(self, options: list[GameOption]) -> Optional[GameOption]:
        for preferred in (
            ActionType.COMBAT_ARENA,
            ActionType.COMBAT_FRONT,
            ActionType.QUEST_NPC,
            ActionType.COMBAT_AREA,
        ):
            opts = [o for o in options if o.action == preferred]
            if opts:
                return max(opts, key=lambda o: o.score)
        return None

    def _pick_farm_option(
        self,
        options: list[GameOption],
        *,
        best: Optional[EfficiencyRow],
        brain_focus: Optional[GameOption],
    ) -> Optional[GameOption]:
        hunt_opts = [o for o in options if o.action == ActionType.HUNT_MOB]
        area_opts = [
            o for o in options
            if o.action in (ActionType.COMBAT_AREA, ActionType.AREA_ACTION)
        ]
        if best and hunt_opts:
            # Prefer hunt option matching best mob name
            for o in hunt_opts:
                if best.name and best.name.lower() in o.title.lower():
                    return o
            return max(hunt_opts, key=lambda o: o.score)
        if hunt_opts:
            return max(hunt_opts, key=lambda o: o.score)
        if area_opts:
            return max(area_opts, key=lambda o: o.score)
        if brain_focus and brain_focus.action in (
            ActionType.HUNT_MOB, ActionType.COMBAT_AREA, ActionType.COMBAT_FRONT,
            ActionType.COMBAT_ARENA,
        ):
            return brain_focus
        return brain_focus

    def _needs_exp_boost(self, profile: FullProfile) -> bool:
        if time.time() - self._last_boost_at < 600:
            return False
        active = " ".join(e.title.lower() for e in (profile.effects or []))
        if any(m in active for m in _EXP_BUFF_MARKERS):
            return False
        # Inventory has a booster?
        for art in profile.inventory or []:
            title = (getattr(art, "title", "") or getattr(art, "name", "") or "").lower()
            if any(m in title for m in _EXP_BUFF_MARKERS) or any(m in title for m in _FOOD_BUFF_MARKERS):
                return True
        # During Exp hours — still try to activate anything available via BUFF action
        if self.kb.active_exp_multiplier() > 1.05:
            return True
        return False

    def mark_boost_used(self) -> None:
        self._last_boost_at = time.time()

    def _idle_task_queue(self) -> list[str]:
        """Round-robin side tasks between fights / while regenerating HP."""
        tasks = ["mail_check", "quest_turnin", "auction_refresh", "craft_profession"]
        # Rotate starting point
        self._idle_cursor = (self._idle_cursor + 1) % len(tasks)
        ordered = tasks[self._idle_cursor:] + tasks[:self._idle_cursor]
        return ordered[:2]

    # ------------------------------------------------------------------
    # Apply + report
    # ------------------------------------------------------------------

    async def apply(self, decision: LevelingDecision) -> None:
        """Push directive into MasterController."""
        self._last_focus_title = decision.directive.title
        if self.controller:
            await self.controller.apply_directive(decision.directive)

    def should_report(self) -> bool:
        return (time.time() - self._last_report_at) >= self.REPORT_INTERVAL_SEC

    def mark_reported(self) -> None:
        self._last_report_at = time.time()

    def build_level_up_update(self) -> str:
        return self.progress.telegram_html()

    async def run_idle_tasks(self, bot: Any, tasks: list[str]) -> list[str]:
        """
        Best-effort side work during regen windows.

        Auction / mail / craft endpoints are not fully reverse-engineered yet —
        we perform safe no-ops / quest turn-ins / bag free that already exist.
        """
        done: list[str] = []
        for task in tasks:
            try:
                if task == "quest_turnin" and hasattr(bot, "quests"):
                    turned = await bot.quests.retry_pending_type2()
                    if turned:
                        done.append("quest_turnin")
                        self.note_quest_progress(title="turn-in")
                elif task == "mail_check":
                    # Placeholder: log intent; extend when mail API is mapped
                    logger.debug("LevelingEngine idle: mail_check (stub)")
                    done.append("mail_check:stub")
                elif task == "auction_refresh":
                    # Sample quest item prices if KB has required items
                    for q in self.kb.list_quests(max_level=self.progress.level)[:5]:
                        for item, price in self.kb.quest_item_prices(q).items():
                            logger.debug("auction sample %s=%s", item, price)
                    done.append("auction_refresh:kb")
                elif task == "craft_profession":
                    logger.debug("LevelingEngine idle: craft_profession (stub)")
                    done.append("craft_profession:stub")
            except Exception as exc:
                logger.debug("idle task %s failed: %s", task, exc)
        return done
