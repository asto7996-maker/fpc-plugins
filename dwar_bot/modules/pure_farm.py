"""
PureFarmEngine — hunt-only filler loop.

Ignores Flash side-quests (heal_wounded) and Level-Up planner spam.
One job: hunt free map bots, finish fights over WS, report real wins.

Story/quests own the tick when ``auto_quests`` is on. PureFarm runs only
as a filler when quests are disabled (or ``PURE_FARM_ONLY=1``), and/or
when ``should_run`` says the area is a farm-only village grind.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

VILLAGE_AREAS = frozenset({"930", "931", "932"})


@dataclass
class PureFarmStats:
    wins: int = 0
    losses: int = 0
    skips: int = 0
    started_at: float = field(default_factory=time.time)
    last_win_at: float = 0.0
    last_mob: str = ""
    money_at_start: float = 0.0
    money_now: float = 0.0
    level_at_start: int = 0
    level_now: int = 0
    notified_wins: int = 0

    def telegram_html(self) -> str:
        elapsed = max(1.0, time.time() - self.started_at)
        wph = self.wins / elapsed * 3600.0
        gold_delta = self.money_now - self.money_at_start
        lines = [
            "⚔️ <b>Pure Farm</b>",
            f"• Побед: <b>{self.wins}</b> · поражений: {self.losses} · skip: {self.skips}",
            f"• Скорость: <b>{wph:.0f}</b> побед/час",
            f"• Последний моб: {self.last_mob or '—'}",
            f"• Уровень: {self.level_at_start} → <b>{self.level_now}</b>",
            f"• Золото: {self.money_at_start:.2f} → <b>{self.money_now:.2f}</b> "
            f"({gold_delta:+.2f})",
        ]
        if self.wins >= 10 and abs(gold_delta) < 0.01 and self.level_now <= self.level_at_start:
            lines.append(
                "• ⚠️ Победы без золота/уровня — деревенские Крэтс дают 0 exp; "
                "открываем наборы/пленённых, нужен клик по раненым для выхода."
            )
        return "\n".join(lines)


class PureFarmEngine:
    """
    Drop-in farm loop for DwarBot.

    Call ``should_run`` each tick; if True, ``run_tick(bot)`` owns the tick
    (hunt → fight → report) and returns True so the legacy planner is skipped.
    """

    REPORT_EVERY_WINS = 5

    def __init__(self) -> None:
        self.stats = PureFarmStats()
        self._armed = False
        self._cleared_wo = False
        self._last_report_at = 0.0

    def should_run(
        self,
        *,
        max_farm: bool,
        area_id: str,
        level: int,
        auto_quests: bool = True,
        force: bool = False,
        story_stalled: bool = False,
    ) -> bool:
        """
        Hunt-only mode.

        When ``auto_quests`` is True (default), return False so the main
        planner can talk to story NPCs. Pass ``force=True`` (env
        PURE_FARM_ONLY) or ``story_stalled=True`` (Flash heal blocked) to
        hunt anyway.
        """
        if force or story_stalled:
            return True
        if auto_quests:
            return False
        if max_farm:
            return True
        if str(area_id or "") in VILLAGE_AREAS and int(level or 1) >= 2:
            return True
        return False

    def arm(self, *, level: int, money: float) -> None:
        if self._armed:
            self.stats.level_now = level
            self.stats.money_now = money
            return
        self._armed = True
        self.stats = PureFarmStats(
            level_at_start=level,
            level_now=level,
            money_at_start=money,
            money_now=money,
        )
        logger.info(
            "PureFarm ARMED Lv%d gold=%.2f — flash quests ignored, hunt-only.",
            level, money,
        )

    def clear_flash_quest(self, quests: Any) -> None:
        """Mark Flash heal as ignored for hunt ticks — do NOT wipe WO state.

        Wiping pending_world_objective broke later story-checks after stall.
        Keep the objective (stalled_until / flash_only) so the bot can re-poll
        Торгор when the stall expires.
        """
        if self._cleared_wo:
            return
        wo = getattr(quests, "pending_world_objective", None) or {}
        if not wo:
            self._cleared_wo = True
            return
        kind = str(wo.get("kind") or "")
        if kind == "heal_wounded" and (
            wo.get("flash_only") or wo.get("http_impossible") or wo.get("farm_open")
        ):
            try:
                # Ensure stall so planner won't spin empty story-checks mid-hunt
                import time as _time
                wo = dict(wo)
                if float(wo.get("stalled_until") or 0) < _time.time():
                    wo["stalled_until"] = _time.time() + 900.0
                wo["farm_open"] = True
                quests.pending_world_objective = wo
                if hasattr(quests, "_persist_world_objective"):
                    quests._persist_world_objective()
                logger.info(
                    "PureFarm: keeping flash WO '%s' stalled — hunt now, "
                    "story-check later.",
                    kind,
                )
            except Exception as exc:
                logger.debug("PureFarm stall WO: %s", exc)
        self._cleared_wo = True

    async def run_tick(self, bot: Any) -> bool:
        """
        Execute one pure-farm tick on *bot* (DwarBot).
        Returns True if this tick was handled (caller must skip legacy plan).
        """
        from dwar_bot.modules.combat_engine import BattleResult

        farm = bot.settings.farm
        char = bot._char
        state = bot._state
        level = int(getattr(char, "level", 1) or 1)
        money = float(getattr(state, "money", 0) or 0)
        area = str(getattr(state, "area_id", "") or "")

        self.arm(level=level, money=money)
        self.clear_flash_quest(bot.quests)

        # Keep open-farm push, but do not wipe quest/story pins — those are
        # owned by the planner when auto_quests is on.
        try:
            bot.brain.push_farm(300.0)
        except Exception:
            pass

        if not farm.auto_combat or not farm.farm_area:
            logger.info("PureFarm: combat/farm_area off — idle.")
            return True

        # HP gate
        hp_pct = float(getattr(char, "hp_percent", 100) or 100)
        if hp_pct < float(farm.hp_retreat or 15):
            logger.info("PureFarm: HP %.0f%% — wait regen.", hp_pct)
            if farm.auto_heal:
                try:
                    await bot.timers.wait_for_hp(
                        target_percent=max(40.0, float(farm.hp_heal or 40)),
                        max_wait=120,
                    )
                except Exception:
                    import asyncio
                    await asyncio.sleep(20)
            return True

        # Finish active fight first
        if await bot.combat.is_in_battle():
            result = await bot.combat.finish_fight(timeout=180.0)
            await self._note_result(bot, result, mob="(active)")
            return True

        # Real progress: open kits/chests/captives BEFORE empty Cretas grind.
        # Wrong action_id used to make this a no-op («Не задано действие!»).
        try:
            n_bag = await bot.combat.open_bag_actions(max_actions=5)
            if n_bag:
                logger.info("PureFarm: bag actions opened=%d", n_bag)
                try:
                    await bot.combat.free_backpack(target_free=3, max_drops=20)
                except Exception:
                    pass
                try:
                    await bot.combat.equip_from_bag(max_items=6)
                except Exception:
                    pass
                # Re-check money/level after loot
                try:
                    bot._state = await bot._client.get_state()
                    self.stats.money_now = float(bot._state.money or self.stats.money_now)
                    self.stats.level_now = int(bot._state.level or self.stats.level_now)
                except Exception:
                    pass
                return True
        except Exception as exc:
            logger.debug("PureFarm bag actions: %s", exc)

        # Overweight bag blocks travel / some actions
        try:
            await bot.combat.free_backpack(target_free=2, max_drops=15)
        except Exception:
            pass

        # Village: always Крэтс; elsewhere empty needle → SUIS priority / any free
        mob = ""
        if area in VILLAGE_AREAS:
            mob = "Крэтс"
        else:
            mob = str(getattr(bot.combat, "_last_map_hunt_name", "") or "")

        logger.info(
            "PureFarm tick#%d: hunt '%s' area=%s HP=%.0f%% wins=%d",
            getattr(bot, "_iteration", 0), mob or "any", area, hp_pct, self.stats.wins,
        )

        result = await bot.combat.try_hunt_attack(
            name_substr=mob,
            area_id=area,
        )
        await self._note_result(bot, result, mob=mob or "any")

        # Periodic TG report so user sees REAL results (wins), not fake Exp%
        if (
            self.stats.wins > 0
            and self.stats.wins % self.REPORT_EVERY_WINS == 0
            and self.stats.wins != self.stats.notified_wins
        ):
            self.stats.notified_wins = self.stats.wins
            try:
                if bot.settings.notify.battles:
                    await bot.notify(self.stats.telegram_html(), "battles")
            except Exception as exc:
                logger.debug("PureFarm notify: %s", exc)

        return True

    async def _note_result(self, bot: Any, result: Any, *, mob: str) -> None:
        from dwar_bot.modules.combat_engine import BattleResult

        name = result.name if hasattr(result, "name") else str(result)
        if result == BattleResult.WIN:
            self.stats.wins += 1
            self.stats.last_win_at = time.time()
            self.stats.last_mob = mob
            try:
                bot._profile = await bot.stats.read_full_profile()
                bot._char = bot._profile.char
                bot._state = bot._profile.state
                self.stats.level_now = int(bot._char.level or self.stats.level_now)
                self.stats.money_now = float(bot._state.money or self.stats.money_now)
            except Exception:
                pass
            # Keep open-farm push alive
            try:
                bot.brain.push_farm(300.0)
                bot.quests.clear_exhausted(local_only=True)
            except Exception:
                pass
            logger.info(
                "PureFarm WIN #%d «%s» Lv%d gold=%.2f",
                self.stats.wins, mob, self.stats.level_now, self.stats.money_now,
            )
        elif result == BattleResult.LOSE:
            self.stats.losses += 1
            logger.info("PureFarm LOSE «%s»", mob)
        elif result == BattleResult.NO_BATTLE:
            self.stats.skips += 1
            rem = 0.0
            try:
                rem = float(bot.combat.hygiene_remaining_sec() or 0)
            except Exception:
                rem = 0.0
            if rem > 1:
                import asyncio
                wait = min(rem, 60.0)
                logger.info("PureFarm hygiene wait %.0fs", wait)
                await asyncio.sleep(wait)
            else:
                import asyncio
                await asyncio.sleep(8)
        else:
            logger.info("PureFarm result=%s «%s»", name, mob)
