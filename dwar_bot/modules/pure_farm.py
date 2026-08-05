"""
PureFarmEngine — hunt-only filler loop.

Ignores Flash side-quests (heal_wounded) and Level-Up planner spam.
One job: hunt free map bots, finish fights over WS, report real wins.

When village fights yield 0 gold/level for several wins, stop hunting and
idle quietly (bag opens + rare story-check) instead of spamming TG.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

VILLAGE_AREAS = frozenset({"930", "931", "932"})
# Post-village farm fronts with real gold (Lv2–3 spiders / Zigred zones).
POST_VILLAGE_FARM_AREAS = ("227", "226", "159", "192")

# Max-farm side work (loot / events / quests) cadence.
SIDE_EVERY_WINS = 5
SIDE_EVERY_SEC = 90.0

# Area hotspot loot keywords (skip PvP bandits / lava stream).
AREA_LOOT_KEYWORDS = (
    "коряг", "колес", "череп", "окошк", "арк", "сарай",
    "амбар", "пристрой", "лужиц", "обыскать", "заглянуть",
    "изучить", "покрутить", "осмотреть",
)
AREA_LOOT_SKIP = (
    "лав", "бандит", "боро", "никс", "вельмож", "крумп",
)

# Flavor NPCs in farm fronts — dialogue only, no quest progress / gold.
FLAVOR_NPC_IDS = frozenset({"121", "132"})  # Лука, Сугор
FLAVOR_NPC_NAME_KW = (
    "сугор", "лука", "сиротск", "дом сугора",
)

# After this many wins with no gold/level change — stop Cretas grind.
ZERO_REWARD_WINS = 8
# How often to re-try bag opens / exit / story while idling (seconds).
ZERO_REWARD_IDLE_SEC = 120.0
# TG reminder about Flash heal while idling (once per this window).
ZERO_REWARD_TG_SEC = 3600.0


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
    zero_reward: bool = False
    zero_reward_notified: bool = False
    last_zero_tg_at: float = 0.0

    @property
    def gold_delta(self) -> float:
        return float(self.money_now) - float(self.money_at_start)

    @property
    def leveled(self) -> bool:
        return int(self.level_now) > int(self.level_at_start)

    def is_zero_reward(self) -> bool:
        return (
            self.wins >= ZERO_REWARD_WINS
            and abs(self.gold_delta) < 0.01
            and not self.leveled
        )

    def telegram_html(self) -> str:
        elapsed = max(1.0, time.time() - self.started_at)
        wph = self.wins / elapsed * 3600.0
        gold_delta = self.gold_delta
        lines = [
            "⚔️ <b>Pure Farm</b>",
            f"• Побед: <b>{self.wins}</b> · поражений: {self.losses} · skip: {self.skips}",
            f"• Скорость: <b>{wph:.0f}</b> побед/час",
            f"• Последний моб: {self.last_mob or '—'}",
            f"• Уровень: {self.level_at_start} → <b>{self.level_now}</b>",
            f"• Золото: {self.money_at_start:.2f} → <b>{self.money_now:.2f}</b> "
            f"({gold_delta:+.2f})",
        ]
        if self.zero_reward or self.is_zero_reward():
            lines.append(
                "• ⏸ Охота в деревне остановлена (Крэтс = 0 exp).\n"
                "  Нужен выход в Дымные сопки / ущелье (пауки, Зигред).\n"
                "  Бот проверяет Торгора, крафт копий и переход."
            )
        return "\n".join(lines)


class PureFarmEngine:
    """
    Drop-in farm loop for DwarBot.

    Call ``should_run`` each tick; if True, ``run_tick(bot)`` owns the tick
    (hunt → fight → report) and returns True so the legacy planner is skipped.
    """

    REPORT_EVERY_WINS = 10  # only when gold/level actually moves

    def __init__(self) -> None:
        self.stats = PureFarmStats()
        self._armed = False
        self._cleared_wo = False
        self._last_report_at = 0.0
        self._last_idle_work_at = 0.0
        self._last_side_at = 0.0
        self._side_wins_marker = 0
        self._area_loot_tried: set[str] = set()
        self._event_barn_hits = 0

    def _need_side_progress(self) -> bool:
        now = time.time()
        if now - float(self._last_side_at or 0) >= SIDE_EVERY_SEC:
            return True
        if self.stats.wins - int(self._side_wins_marker or 0) >= SIDE_EVERY_WINS:
            return True
        return False

    async def _max_side_progress(self, bot: Any, *, area: str) -> int:
        """
        Between hunts: loot hotspots, barn event, bag opens, local quests, arena.

        Returns number of productive actions.
        """
        self._last_side_at = time.time()
        self._side_wins_marker = int(self.stats.wins or 0)
        done = 0
        farm = bot.settings.farm

        # 1) Bag loot / craft / equip / free space (no food — hunts first)
        if farm.auto_loot or farm.auto_equip:
            try:
                n_bag = await bot.combat.open_bag_actions(
                    max_actions=4, include_food=False,
                )
                if n_bag:
                    done += n_bag
                    logger.info("MaxFarm side: bag actions=%d", n_bag)
                await bot.combat.free_backpack(target_free=4, max_drops=20)
                if farm.auto_equip and n_bag:
                    await bot.combat.equip_from_bag(max_items=4)
            except Exception as exc:
                logger.debug("MaxFarm bag: %s", exc)

        # Essay collection exchange (frees stacks → rewards)
        try:
            bag = await bot._client.get_bag()
            for a in bag.get("artifact_list") or []:
                if not isinstance(a, dict):
                    continue
                acts = a.get("artifact_actions") or {}
                if not isinstance(acts, dict):
                    continue
                for meta in acts.values():
                    if not isinstance(meta, dict):
                        continue
                    title = str(meta.get("title") or "").lower()
                    if "бмен" not in title and "коллекц" not in title:
                        continue
                    real = bot._client.bag_action_real_id(meta)
                    if not real:
                        continue
                    resp = await bot._client.run_artifact_action(
                        a.get("id"), real, confirmed=True,
                    )
                    ok = bool((resp.raw or {}).get("action_run", {}).get("ok"))
                    if ok:
                        done += 1
                        logger.info(
                            "MaxFarm side: exchanged «%s»",
                            a.get("title") or real,
                        )
                    break
        except Exception as exc:
            logger.debug("MaxFarm essay exchange: %s", exc)

        # 2) Area loot + barn event («Переполох в сарае»)
        if farm.auto_loot or farm.farm_area:
            try:
                info = await bot._client.get_area_info()
                for item in info.items or []:
                    name = str(getattr(item, "name", "") or "")
                    low = name.lower()
                    act_id = str(getattr(item, "action_id", "") or "")
                    if not act_id:
                        continue
                    if any(s in low for s in AREA_LOOT_SKIP):
                        continue
                    # Barn: always try when off CD (event animals)
                    is_barn = "сарай" in low
                    is_loot = any(k in low for k in AREA_LOOT_KEYWORDS)
                    if not (is_barn or is_loot):
                        continue
                    # Respect client cooldown (ltime/dtime)
                    try:
                        ltime = int(getattr(item, "ltime", 0) or 0)
                        dtime = int(getattr(item, "dtime", 0) or 0)
                    except (TypeError, ValueError):
                        ltime, dtime = 0, 0
                    if ltime > 0 and dtime > int(time.time()):
                        continue
                    key = f"{act_id}:{getattr(item, 'link_id', '')}"
                    if key in self._area_loot_tried and not is_barn:
                        continue
                    resp = await bot._client.run_area_action(
                        object_id=getattr(item, "object_id", None)
                        or info.area_id
                        or area,
                        action_id=act_id,
                        link_id=getattr(item, "link_id", "") or "",
                        object_class=getattr(item, "object_class", None) or "AREA",
                    )
                    err = str(resp.redirect_error or resp.error or "")
                    loot = []
                    try:
                        loot = list(resp.loot_lines() or [])
                    except Exception:
                        loot = []
                    if is_barn and (not err or err.lower() in ("false", "none", "")):
                        self._event_barn_hits += 1
                        done += 1
                        logger.info(
                            "MaxFarm event barn «%s» hit#%d",
                            name, self._event_barn_hits,
                        )
                    elif loot:
                        done += 1
                        self._area_loot_tried.add(key)
                        logger.info(
                            "MaxFarm loot «%s»: %s",
                            name, (loot[0] if loot else "")[:100],
                        )
                    elif err and "бурный поток" not in err.lower():
                        self._area_loot_tried.add(key)
                        logger.debug("MaxFarm hotspot «%s»: %s", name, err[:80])
                    else:
                        self._area_loot_tried.add(key)
                    # Cap hotspot spam per side tick
                    if done >= 6:
                        break
            except Exception as exc:
                logger.debug("MaxFarm area loot: %s", exc)

        # 3) Local quest NPCs (skip flavor: Сугор / Лука / orphan cave)
        if farm.auto_quests:
            try:
                info = await bot._client.get_area_info()
                for item in info.items or []:
                    npc_id = str(getattr(item, "npc_id", "") or "")
                    if not npc_id:
                        continue
                    name = str(getattr(item, "name", "") or "")
                    low = name.lower()
                    if npc_id in FLAVOR_NPC_IDS or any(
                        kw in low for kw in FLAVOR_NPC_NAME_KW
                    ):
                        continue
                    href = getattr(item, "href", "") or ""
                    steps = await bot.quests.walk_npc_api(
                        npc_id,
                        link_id=str(getattr(item, "link_id", "0") or "0"),
                        f_id=str(getattr(item, "f_id", "0") or "0"),
                        area_id=area,
                        href=href,
                        max_steps=8,
                    )
                    if steps:
                        done += steps
                        logger.info(
                            "MaxFarm quest NPC %s (%s) steps=%d",
                            npc_id, name, steps,
                        )
                        break  # one NPC per side tick
            except Exception as exc:
                logger.debug("MaxFarm quests: %s", exc)

        # 4) Arena / event NPC from hunt_conf when ready
        if farm.farm_arena:
            try:
                hunt = await bot._client.get_hunt_conf()
                for npc in hunt.get("npcs", []) or []:
                    title = str(npc.get("title") or "")
                    left = int(npc.get("time_left") or 0)
                    if left > 0:
                        continue
                    low = title.lower()
                    if "арен" not in low and "battleground" not in low:
                        continue
                    npc_id = str(npc.get("npc_id") or "")
                    url = str(npc.get("url") or "")
                    hash_flag = ""
                    for p in url.split("&"):
                        if "=" not in p and p and "npc.php" not in p:
                            hash_flag = p
                    if npc_id:
                        await bot._client.join_arena(int(npc_id), hash_flag)
                        done += 1
                        logger.info("MaxFarm arena open «%s»", title)
                        break
                # Live hunt event title (barn panic etc.)
                ev = hunt.get("event") or {}
                if ev.get("title"):
                    logger.debug(
                        "MaxFarm live event: %s id=%s",
                        ev.get("title"), ev.get("id"),
                    )
            except Exception as exc:
                logger.debug("MaxFarm arena/event: %s", exc)

        if done:
            try:
                bot._state = await bot._client.get_state()
                self.stats.money_now = float(bot._state.money or self.stats.money_now)
                self.stats.level_now = int(bot._state.level or self.stats.level_now)
            except Exception:
                pass
        return done

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
            if self.stats.leveled or abs(self.stats.gold_delta) >= 0.01:
                # Real progress resumed — allow hunting again
                self.stats.zero_reward = False
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

    async def _notify_zero_reward_once(self, bot: Any, *, force: bool = False) -> None:
        now = time.time()
        if self.stats.zero_reward_notified and not force:
            if now - float(self.stats.last_zero_tg_at or 0) < ZERO_REWARD_TG_SEC:
                return
        self.stats.zero_reward_notified = True
        self.stats.last_zero_tg_at = now
        try:
            if bot.settings.notify.battles or bot.settings.notify.level_up:
                await bot.notify(self.stats.telegram_html(), "battles")
        except Exception as exc:
            logger.debug("PureFarm zero-reward notify: %s", exc)

    async def _try_leave_village_now(self, bot: Any, *, area: str) -> bool:
        """Leave 932/930/931 for Дымные сопки (192) when war-chief unlocked."""
        try:
            # Prefer mapped exit from area info
            info = await bot._client.get_area_info()
            for item in info.items or []:
                if str(getattr(item, "code", "") or "") != "COME_IN":
                    continue
                dest = str(getattr(item, "area_id", "") or "")
                low = str(getattr(item, "name", "") or "").lower()
                if dest not in {"192", "191", "100"} and "сопк" not in low and "дымн" not in low:
                    continue
                tr = await bot._client.go_area(dest)
                err = str(tr.redirect_error or tr.error or "")
                st = await bot._client.get_state()
                if str(st.area_id) not in VILLAGE_AREAS:
                    logger.info(
                        "PureFarm: left village %s → %s (%s)",
                        area, st.area_id, getattr(item, "name", dest),
                    )
                    self.stats.zero_reward = False
                    self.stats.money_at_start = float(st.money or 0)
                    self.stats.level_at_start = int(st.level or 0)
                    bot._state = st
                    return True
                if err:
                    logger.debug("PureFarm leave %s: %s", dest, err[:100])
            # Hard probe 192
            tr = await bot._client.go_area("192")
            st = await bot._client.get_state()
            if str(st.area_id) not in VILLAGE_AREAS:
                logger.info("PureFarm: left village → area=%s", st.area_id)
                self.stats.zero_reward = False
                self.stats.money_at_start = float(st.money or 0)
                self.stats.level_at_start = int(st.level or 0)
                bot._state = st
                return True
            err = str(tr.redirect_error or tr.error or "")
            if err:
                logger.debug("PureFarm leave probe: %s", err[:100])
        except Exception as exc:
            logger.debug("PureFarm leave village: %s", exc)
        return False

    async def _zero_reward_idle(self, bot: Any, *, area: str) -> bool:
        """Idle instead of farming 0-exp Cretas; rare bag/story/exit probes."""
        self.stats.zero_reward = True
        await self._notify_zero_reward_once(bot)

        now = time.time()
        if now - self._last_idle_work_at >= ZERO_REWARD_IDLE_SEC:
            self._last_idle_work_at = now
            try:
                n_bag = await bot.combat.open_bag_actions(max_actions=3)
                if n_bag:
                    logger.info("PureFarm idle: bag actions=%d", n_bag)
                    await bot.combat.free_backpack(target_free=3, max_drops=15)
                    await bot.combat.equip_from_bag(max_items=4)
            except Exception as exc:
                logger.debug("PureFarm idle bag: %s", exc)

            # Probe exit — if war-chief unlocked, leave village
            try:
                tr = await bot._client.go_area("192")
                ca = (tr.raw or {}).get("common|action") or {}
                err = str(ca.get("redirect_error") or ca.get("error") or "")
                if not err or "не задано" in err.lower():
                    st = await bot._client.get_state()
                    if str(st.area_id) not in VILLAGE_AREAS:
                        logger.info(
                            "PureFarm: left village → area=%s — resume normal farm.",
                            st.area_id,
                        )
                        self.stats.zero_reward = False
                        self.stats.money_at_start = float(st.money or 0)
                        self.stats.level_at_start = int(st.level or 0)
                        bot._state = st
                        return True
                else:
                    logger.debug("PureFarm idle exit probe: %s", err[:100])
            except Exception as exc:
                logger.debug("PureFarm idle exit: %s", exc)

            # Soft story-check Торгор if stall expired
            try:
                wo = bot.quests.pending_world_objective or {}
                stalled_until = float(wo.get("stalled_until") or 0)
                if stalled_until and stalled_until <= time.time():
                    n = await bot.quests._story_check_flash_heal_npc(
                        "409", link_id="2960", f_id="4", area_id=area or "932"
                    )
                    if n:
                        self.stats.zero_reward = False
                        logger.info("PureFarm idle: story advanced (%d steps).", n)
            except Exception as exc:
                logger.debug("PureFarm idle story: %s", exc)

        logger.info(
            "PureFarm idle (0-reward village): wins=%d gold=%.2f — waiting Flash heal.",
            self.stats.wins, self.stats.money_now,
        )
        await asyncio.sleep(45.0)
        return True

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
                    await asyncio.sleep(20)
            return True

        # Finish active fight first
        if await bot.combat.is_in_battle():
            result = await bot.combat.finish_fight(timeout=180.0)
            await self._note_result(bot, result, mob="(active)")
            return True

        # Real progress: open kits/chests/captives BEFORE empty Cretas grind.
        # In post-village MaxFarm never early-return on bag — eating apples /
        # opening junk used to skip hunts and look like a hang.
        post_village = area in {"192", "227", "226", "159", "228"}
        try:
            n_bag = await bot.combat.open_bag_actions(
                max_actions=5 if not post_village else 3,
                include_food=False,
            )
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
                try:
                    bot._state = await bot._client.get_state()
                    self.stats.money_now = float(bot._state.money or self.stats.money_now)
                    self.stats.level_now = int(bot._state.level or self.stats.level_now)
                    if self.stats.leveled or abs(self.stats.gold_delta) >= 0.01:
                        self.stats.zero_reward = False
                except Exception:
                    pass
                # Village only: bag progress beats 0-gold Cretas; open farm hunts.
                if not post_village and not (farm.aggressive or farm.max_farm):
                    return True
        except Exception as exc:
            logger.debug("PureFarm bag actions: %s", exc)

        try:
            await bot.combat.free_backpack(target_free=2, max_drops=15)
        except Exception:
            pass

        # Aggressive / max farm: raise SUIS session so we don't skip every 12 kills
        try:
            if farm.aggressive or farm.max_farm:
                from dwar_bot import config as _cfg
                _cfg.COMBAT.suis_session_kill_limit = max(
                    int(getattr(_cfg.COMBAT, "suis_session_kill_limit", 0) or 0),
                    80,
                )
                _cfg.COMBAT.max_consecutive_battles = max(
                    int(getattr(_cfg.COMBAT, "max_consecutive_battles", 0) or 0),
                    int(farm.max_battles_row or 50),
                )
        except Exception:
            pass

        # Side progress: loot / barn event / quests / arena between hunts
        if self._need_side_progress():
            n_side = await self._max_side_progress(bot, area=area)
            if n_side:
                logger.info("MaxFarm side progress actions=%d", n_side)
                return True

        # Village + Flash heal lock: Cretas are known 0-exp. Don't grind them.
        wo = getattr(bot.quests, "pending_world_objective", None) or {}
        flash_locked_village = (
            area in VILLAGE_AREAS
            and str(wo.get("kind") or "") == "heal_wounded"
            and bool(wo.get("flash_only") or wo.get("http_impossible") or wo.get("farm_open"))
        )
        if flash_locked_village:
            # Skip Cretas entirely while exit is Flash-gated
            self.stats.zero_reward = True
            return await self._zero_reward_idle(bot, area=area)

        # Post-heal / no WO: leave newbie village — Cretas here stay 0-reward.
        if area in VILLAGE_AREAS and not wo:
            left = await self._try_leave_village_now(bot, area=area)
            if left:
                return True
            # Still stuck: story NPC then idle (never grind Cretas)
            try:
                area_info = await bot._client.get_area_info()
                for item in area_info.items or []:
                    if str(getattr(item, "npc_id", "") or "") in {"409", "113", "112"}:
                        href = getattr(item, "href", "") or ""
                        steps = await bot.quests.walk_npc_api(
                            str(item.npc_id),
                            link_id=str(getattr(item, "link_id", "0") or "0"),
                            f_id=str(getattr(item, "f_id", "0") or "0"),
                            area_id=area,
                            href=href,
                            max_steps=12,
                        )
                        if steps:
                            logger.info("PureFarm village story steps=%d", steps)
                            return True
            except Exception as exc:
                logger.debug("PureFarm village story: %s", exc)
            return await self._zero_reward_idle(bot, area=area)

        # Also stop after enough empty wins even without flash WO
        if area in VILLAGE_AREAS and (
            self.stats.zero_reward or self.stats.is_zero_reward()
        ):
            return await self._zero_reward_idle(bot, area=area)

        mob = ""
        if area in VILLAGE_AREAS:
            mob = "Крэтс"
        else:
            # Prefer SUIS level pin (Зигред-воин at Lv3+) over leftover Cretas
            try:
                from dwar_bot.modules.suis_knowledge import default_hunt_mob
                mob = default_hunt_mob(level)
                if mob.lower() == "крэтс" and level >= 3:
                    mob = "Зигред"
            except Exception:
                mob = str(getattr(bot.combat, "_last_map_hunt_name", "") or "")
                if level >= 3 and (not mob or "рэтс" in mob.lower()):
                    mob = "Зигред"

        # Area 192 often still spawns only 0-reward Cretas — rotate to ущелье.
        if area == "192" and level >= 3:
            try:
                bots = await bot._client.get_hunt_bots("192")
                names = {str(b.get("name") or "").lower() for b in bots}
                only_cretas = bool(names) and all("рэтс" in n for n in names)
                if only_cretas or not names:
                    for dest in POST_VILLAGE_FARM_AREAS:
                        if dest == "192":
                            continue
                        tr = await bot._client.go_area(dest)
                        st = await bot._client.get_state()
                        if str(st.area_id) == dest:
                            logger.info(
                                "PureFarm: 192 Cretas-only → farm area %s", dest,
                            )
                            bot._state = st
                            area = dest
                            if "зигред" not in (mob or "").lower():
                                mob = "Огненный паук" if dest == "227" else mob
                            break
                        err = str(tr.redirect_error or tr.error or "")
                        if err:
                            logger.debug("PureFarm travel %s: %s", dest, err[:80])
            except Exception as exc:
                logger.debug("PureFarm post-village travel: %s", exc)

        # Area 227: fire spiders pay gold; avoid Cretas-вожак fallback spam.
        if area == "227":
            mob = "Огненный паук"
        elif area in {"226", "159"} and (not mob or "рэтс" in mob.lower()):
            mob = "Зигред"

        logger.info(
            "PureFarm tick#%d: hunt '%s' area=%s HP=%.0f%% wins=%d",
            getattr(bot, "_iteration", 0), mob or "any", area, hp_pct, self.stats.wins,
        )

        result = await bot.combat.try_hunt_attack(
            name_substr=mob,
            area_id=area,
        )
        await self._note_result(bot, result, mob=mob or "any")

        # Enter idle immediately when threshold hit this tick
        if area in VILLAGE_AREAS and self.stats.is_zero_reward():
            self.stats.zero_reward = True
            await self._notify_zero_reward_once(bot)
            return True

        # TG only when economy actually moves (no spam of 0-gold win counters)
        if (
            self.stats.wins > 0
            and self.stats.wins % self.REPORT_EVERY_WINS == 0
            and self.stats.wins != self.stats.notified_wins
            and (abs(self.stats.gold_delta) >= 0.01 or self.stats.leveled)
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
            try:
                bot.brain.push_farm(300.0)
                bot.quests.clear_exhausted(local_only=True)
            except Exception:
                pass
            logger.info(
                "PureFarm WIN #%d «%s» Lv%d gold=%.2f%s",
                self.stats.wins,
                mob,
                self.stats.level_now,
                self.stats.money_now,
                " [0-reward]" if self.stats.is_zero_reward() else "",
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
            aggressive = bool(getattr(bot.settings.farm, "aggressive", False))
            if rem > 1:
                wait = min(rem, 20.0 if aggressive else 60.0)
                logger.info("PureFarm hygiene wait %.0fs", wait)
                await asyncio.sleep(wait)
            else:
                await asyncio.sleep(2.0 if aggressive else 8.0)
        else:
            logger.info("PureFarm result=%s «%s»", name, mob)
