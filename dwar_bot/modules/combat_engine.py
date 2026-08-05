"""
Combat engine — drives battles through the dwar.ru HTTP API.

Supported combat flows
----------------------
* **Front / PvP battles** — ``front|locations`` → ``front|fight_join`` → ``front|fight_start``
* **Direct attack**       — ``common|action?code=ATTACK&nick=<target>``
* **Arena (battleground)**— ``battleground|chaotic_confirm``
* **Potion usage**        — ``common|action?code=DRINK&artifact_id=<id>``
* **Combat log parsing**  — from ``fight.php`` / API bonus_text
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from dwar_bot.config import COMBAT, DELAY_COMBAT
from dwar_bot.core.game_client import DwarGameClient, ApiResponse, STATUS_OK
from dwar_bot.modules.fight_client import FightClient, FightOutcome
from dwar_bot.modules.stats_parser import StatsParser, Artifact, FullProfile

logger = logging.getLogger(__name__)

# Action codes discovered on the live server
CODE_ATTACK = "ATTACK"
CODE_DRINK = "DRINK"
CODE_UNDRINK = "UNDRINK"
CODE_PUT_ON = "PUT_ON"
CODE_PUT_OFF = "PUT_OFF"
CODE_ART_REPAIR = "ART_REPAIR"


class BattleResult(Enum):
    NO_BATTLE = auto()
    JOINED = auto()
    ONGOING = auto()
    WIN = auto()
    LOSE = auto()
    FLED = auto()
    ERROR = auto()


@dataclass
class BattleStats:
    battles_joined: int = 0
    wins: int = 0
    losses: int = 0
    potions_used: int = 0
    attacks_made: int = 0
    consecutive_battles: int = 0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total else 0.0


@dataclass
class CombatLogEntry:
    text: str = ""
    damage: int = 0
    is_critical: bool = False
    actor: str = ""


# ---------------------------------------------------------------------------
# CombatEngine
# ---------------------------------------------------------------------------

class CombatEngine:
    """Drives combat via HTTP API calls."""

    def __init__(self, client: DwarGameClient, stats: StatsParser) -> None:
        self._client = client
        self._stats = stats
        self.session = BattleStats()
        self._fight = FightClient(client)
        # Serialize hunt/finish so Local recover / fight-first cannot open
        # a second WebSocket on the same fight (hangs: no ATTACKNOW / no win).
        self._fight_lock = asyncio.Lock()
        self._fight_busy = False
        self.last_fight_attacks: int = 0
        self.last_fight_damage_dealt: int = 0
        self.last_fight_damage_taken: int = 0
        self.last_fight_hit_seq: str = ""
        # SUIS session soft-limits (dwar.browsergamebots.com)
        self._suis_session_started: float = 0.0
        self._suis_session_kills: int = 0
        self._suis_error_ops: int = 0
        self._suis_failed_ops: int = 0
        self._last_map_hunt_name: str = ""
        self._hygiene = None
        if getattr(COMBAT, "suis_enabled", True):
            try:
                from dwar_bot.modules.suis_knowledge import apply_suis_defaults_to_combat_dict
                for k, v in apply_suis_defaults_to_combat_dict().items():
                    if hasattr(COMBAT, k):
                        setattr(COMBAT, k, v)
                logger.info(
                    "SUIS defaults applied: elixir=%.0f%% block=%.0f/%.0f",
                    COMBAT.hp_elixir_threshold,
                    COMBAT.hp_block_threshold,
                    COMBAT.hp_unblock_threshold,
                )
            except Exception as exc:
                logger.debug("SUIS defaults: %s", exc)
        if getattr(COMBAT, "rfcheats_hygiene_enabled", True):
            try:
                from dwar_bot.modules.rfcheats_knowledge import (
                    HygieneTracker,
                    RfCheatsDefaults,
                    RFCHEATS_DEFAULTS,
                )
                d = RfCheatsDefaults(
                    max_continuous_minutes=int(
                        getattr(
                            COMBAT,
                            "rfcheats_max_continuous_minutes",
                            RFCHEATS_DEFAULTS.max_continuous_minutes,
                        )
                        or RFCHEATS_DEFAULTS.max_continuous_minutes
                    ),
                    max_daily_minutes=int(
                        getattr(
                            COMBAT,
                            "rfcheats_max_daily_minutes",
                            RFCHEATS_DEFAULTS.max_daily_minutes,
                        )
                        or RFCHEATS_DEFAULTS.max_daily_minutes
                    ),
                    burst_minutes=int(
                        getattr(
                            COMBAT,
                            "rfcheats_burst_minutes",
                            RFCHEATS_DEFAULTS.burst_minutes,
                        )
                        or RFCHEATS_DEFAULTS.burst_minutes
                    ),
                )
                self._hygiene = HygieneTracker(defaults=d)
                # Self-heal stuck empty-hunt loops: long orphan break after 8h budget
                try:
                    rem = float(self._hygiene.remaining_break_sec() or 0)
                    used = float(self._hygiene.daily_active_sec or 0) / 60.0
                    if rem > 120.0:
                        self.hygiene_clear_break()
                        logger.warning(
                            "RF-Cheats hygiene: cleared orphan break "
                            "(%.0fs left, daily=%.0fmin / %d) — resume farm",
                            rem, used, d.max_daily_minutes,
                        )
                except Exception as exc:
                    logger.debug("hygiene auto-clear: %s", exc)
                logger.info(
                    "RF-Cheats hygiene: continuous≤%dmin daily≤%dmin burst≤%dmin",
                    d.max_continuous_minutes,
                    d.max_daily_minutes,
                    d.burst_minutes,
                )
            except Exception as exc:
                logger.debug("RF-Cheats hygiene: %s", exc)
                self._hygiene = None

    # ------------------------------------------------------------------
    # State detection
    # ------------------------------------------------------------------

    async def is_in_battle(self) -> bool:
        """Return True if the character is currently in a fight."""
        state = await self._client.get_state()
        # flags bit 0 and a non-zero fight_id both indicate an active battle
        return bool(state.flags & 0x1) or bool(state.fight_id)

    async def needs_rest(self) -> bool:
        """True when the bot has fought too many battles in a row."""
        return self.session.consecutive_battles >= COMBAT.max_consecutive_battles

    # ------------------------------------------------------------------
    # Front / PvP battles
    # ------------------------------------------------------------------

    async def try_join_front(self) -> BattleResult:
        """
        Look for an active front (PvP zone) and join it.

        Returns JOINED on success, NO_BATTLE when no fronts are available.
        """
        try:
            fronts = await self._client.get_front_locations()
            if not fronts:
                logger.info("Активных фронтов нет.")
                return BattleResult.NO_BATTLE

            logger.info("Найдено фронтов: %d.", len(fronts))
            for front in fronts:
                area_id = str(front.get("area_id", "") or front.get("id", ""))
                title = front.get("title", front.get("name", "?"))
                if not area_id:
                    continue

                resp = await self._client.join_front(area_id)
                if resp.status == STATUS_OK:
                    self.session.battles_joined += 1
                    self.session.consecutive_battles += 1
                    logger.info("Joined front '%s' (area=%s).", title, area_id)
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    # Attempt to start the fight
                    start = await self._client.start_front()
                    if start.status == STATUS_OK:
                        logger.info("Front battle started.")
                    return BattleResult.JOINED
                else:
                    logger.debug(
                        "join_front(%s) rejected: status=%d err=%s",
                        area_id, resp.status, resp.error,
                    )
            return BattleResult.NO_BATTLE
        except Exception as exc:
            logger.warning("try_join_front error: %s", exc)
            return BattleResult.ERROR

    # ------------------------------------------------------------------
    # Direct attack
    # ------------------------------------------------------------------

    async def attack_player(self, nick: str) -> BattleResult:
        """Attack a specific player/mob by nickname."""
        if not nick:
            return BattleResult.ERROR
        try:
            resp = await self._client.common_action(CODE_ATTACK, {"nick": nick})
            self.session.attacks_made += 1

            err = str(resp.redirect_error or "")
            if err and "Не задан" not in err:
                logger.info("attack(%s) → %s", nick, err)
                return BattleResult.ERROR

            if resp.redirect_url:
                logger.info("attack(%s) → redirect: %s", nick, resp.redirect_url)
                self.session.battles_joined += 1
                self.session.consecutive_battles += 1
                return BattleResult.JOINED

            logger.debug("attack(%s): status=%d", nick, resp.status)
            return BattleResult.ONGOING
        except Exception as exc:
            logger.warning("attack_player(%s) error: %s", nick, exc)
            return BattleResult.ERROR

    # ------------------------------------------------------------------
    # Arena / battleground
    # ------------------------------------------------------------------

    async def try_arena(self, area_id: str = "") -> BattleResult:
        """Attempt to enter the chaotic arena (battleground)."""
        try:
            extra = {"area_id": area_id} if area_id else {}
            resp = await self._client.entry_point("battleground", "chaotic_confirm", extra)
            if resp.status == STATUS_OK:
                self.session.battles_joined += 1
                self.session.consecutive_battles += 1
                logger.info("Entered arena battleground.")
                return BattleResult.JOINED
            logger.debug("Arena unavailable: status=%d err=%s", resp.status, resp.error)
            return BattleResult.NO_BATTLE
        except Exception as exc:
            logger.warning("try_arena error: %s", exc)
            return BattleResult.ERROR

    # ------------------------------------------------------------------
    # Potion / consumable usage
    # ------------------------------------------------------------------

    async def use_potion(self, artifact: Artifact) -> bool:
        """Drink a potion by artifact id."""
        try:
            resp = await self._client.common_action(
                CODE_DRINK, {"artifact_id": artifact.art_id}
            )
            err = str(resp.redirect_error or "")
            if err and err.lower() not in ("false", "none", ""):
                logger.debug("use_potion('%s') → %s", artifact.title, err)
                return False
            self.session.potions_used += 1
            logger.info(
                "Used potion '%s' (total: %d).",
                artifact.title, self.session.potions_used,
            )
            if resp.bonus_text:
                for t in resp.bonus_text:
                    logger.info("  → %s", t)
            return True
        except Exception as exc:
            logger.warning("use_potion error: %s", exc)
            return False

    async def heal_if_needed(self, profile: FullProfile) -> bool:
        """
        Drink an HP potion when HP falls below the configured threshold.
        Returns True if a potion was consumed.
        """
        hp_pct = profile.char.hp_percent
        if hp_pct >= COMBAT.hp_elixir_threshold:
            return False

        # HP≈0: often fight-lock stub / death — potion spam doesn't help.
        if hp_pct <= 0.5:
            now = time.time()
            last = float(getattr(self, "_hp0_warn_at", 0.0) or 0.0)
            if now - last >= 120.0:
                self._hp0_warn_at = now
                logger.warning(
                    "HP at %.0f%% — wait regen / finish fight (no potion spam).",
                    hp_pct,
                )
            return False

        potion = await self._stats.find_hp_potion()
        if potion is None:
            now = time.time()
            last = float(getattr(self, "_no_potion_warn_at", 0.0) or 0.0)
            if now - last >= 90.0:
                self._no_potion_warn_at = now
                logger.warning("HP at %.0f%% but no healing potion in backpack!", hp_pct)
            return False

        logger.info("HP %.0f%% below threshold — drinking '%s'.", hp_pct, potion.title)
        return await self.use_potion(potion)

    async def restore_mana_if_needed(self, profile: FullProfile) -> bool:
        """Drink an MP potion when mana falls below the threshold."""
        if profile.char.mp_max <= 0:
            return False
        mp_pct = profile.char.mp_percent
        if mp_pct >= COMBAT.mp_elixir_threshold:
            return False

        potion = await self._stats.find_mp_potion()
        if potion is None:
            return False

        logger.info("MP %.0f%% below threshold — drinking '%s'.", mp_pct, potion.title)
        return await self.use_potion(potion)

    # ------------------------------------------------------------------
    # Equipment maintenance
    # ------------------------------------------------------------------

    async def repair_broken_gear(self, profile: FullProfile) -> int:
        """Repair all broken equipment. Returns count repaired."""
        repaired = 0
        for item in profile.broken_items:
            try:
                resp = await self._client.common_action(
                    CODE_ART_REPAIR, {"artifact_id": item.art_id}
                )
                err = str(resp.redirect_error or "")
                if not err or err.lower() in ("false", "none"):
                    repaired += 1
                    logger.info("Repaired '%s'.", item.title)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            except Exception as exc:
                logger.debug("repair '%s' failed: %s", item.title, exc)
        return repaired

    async def equip_item(self, artifact: Artifact) -> bool:
        """Put on a piece of equipment."""
        try:
            resp = await self._client.common_action(
                CODE_PUT_ON, {"artifact_id": artifact.art_id}
            )
            err = str(resp.redirect_error or "")
            if err and "Не удалось" in err:
                logger.debug("equip('%s') → %s", artifact.title, err)
                return False
            logger.info("Equipped '%s'.", artifact.title)
            return True
        except Exception as exc:
            logger.debug("equip_item error: %s", exc)
            return False

    async def auto_equip(self, profile: FullProfile) -> int:
        """Equip every unequipped, non-broken piece of gear. Returns count."""
        equipped = 0
        for item in profile.equipment:
            if item.is_broken:
                continue
            # "info" as the only icon means the item is not currently worn
            if item.icon_list and "info" in item.icon_list and len(item.icon_list) == 1:
                if await self.equip_item(item):
                    equipped += 1
                await asyncio.sleep(random.uniform(0.4, 1.0))
        if equipped:
            return equipped
        # user.php inventory often empty in village — try bag API gear
        return await self.equip_from_bag()

    async def equip_from_bag(self, *, max_items: int = 8) -> int:
        """
        Equip wearable gear discovered via ``get_bag`` (not user.php art_alt).

        Newbie village often shows 0 inventory items while bag has leather /
        sword / rings — put them on so story fights are less empty-handed.

        Skips items already in a slot and rate-limits repeats so MaxFarm
        side ticks do not re-equip the same bag copies every minute.
        """
        now = time.time()
        # Avoid thrashing: re-equip at most every 10 minutes unless forced fresh.
        last = float(getattr(self, "_last_equip_from_bag_at", 0) or 0)
        if last and (now - last) < 600.0:
            return 0
        try:
            bag = await self._client.get_bag()
        except Exception as exc:
            logger.debug("equip_from_bag get_bag: %s", exc)
            return 0
        arts = bag.get("artifact_list") or []
        # Prefer known starter gear artikuls, then anything with PUT_ON-ish title
        prefer = {
            "10378", "10379", "17972", "46176", "46177", "46314",  # leather / sword / rings
            "3908", "3909", "3910", "3911",
            "33369", "17993",  # newbie backpack / torba — expands capacity
        }
        wear_kw = (
            "меч", "понож", "куртк", "кольц", "амулет", "щит", "шлем",
            "доспех", "перчат", "сапог", "пояс", "наруч",
            "рюкзак", "торб",
        )
        # Artikuls already worn (slot_id set) — do not PUT_ON bag duplicates.
        worn_artikuls = {
            str(a.get("artikul_id") or "")
            for a in arts
            if isinstance(a, dict) and str(a.get("slot_id") or "").strip()
        }
        ranked: list[tuple[int, dict]] = []
        for a in arts:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("artikul_id") or "")
            title = str(a.get("title") or a.get("name") or "")
            iid = str(a.get("id") or "")
            if not iid:
                continue
            # Already equipped in a body/effect slot
            if str(a.get("slot_id") or "").strip():
                continue
            if aid and aid in worn_artikuls:
                continue
            # Skip quest medicine / consumables / chests / summon junk
            if aid in self._KEEP_ARTIKULS:
                continue
            tl = title.lower()
            if any(
                k in tl
                for k in (
                    "снадоб", "эликсир", "яблок", "сундук", "набор", "свиток",
                    "очерк", "сфера", "знамя", "оберег", "вызова", "пхадд",
                )
            ):
                continue
            # Prefer items that expose PUT_ON in bag actions
            acts = a.get("artifact_actions") or {}
            has_put_on = False
            if isinstance(acts, dict):
                for meta in acts.values():
                    if isinstance(meta, dict) and str(meta.get("code") or "") == CODE_PUT_ON:
                        has_put_on = True
                        break
            score = 0
            if aid in prefer:
                score += 100
            if any(k in tl for k in wear_kw):
                score += 50
            if has_put_on:
                score += 30
            if score < 50:
                continue
            ranked.append((score, a))
        ranked.sort(key=lambda x: -x[0])
        equipped = 0
        for _, a in ranked[:max_items]:
            art = Artifact(
                art_id=str(a.get("id") or ""),
                title=str(a.get("title") or a.get("name") or ""),
                kind=str(a.get("kind") or "gear"),
            )
            if await self.equip_item(art):
                equipped += 1
            await asyncio.sleep(random.uniform(0.3, 0.8))
        self._last_equip_from_bag_at = now
        if equipped:
            logger.info("equip_from_bag: надето %d предмет(ов) из сумки.", equipped)
        return equipped

    # Bag actions that unlock gear / finish hunt loot (use nested meta.id!)
    _BAG_OPEN_KEYWORDS = (
        "вскрыть",
        "открыть",
        "развернуть",
        "добить",
        "прикоснуться",
        "использовать",
        "съесть",
        "собрать",  # quest craft: копьё ополченца from parts
    )
    _BAG_OPEN_ITEM_KEYWORDS = (
        "набор",
        "сундук",
        "шкатул",
        "горшок",
        "поручен",
        "пленен",
        "крэтс",
        "душ",
        "яблок",
        "древко",
        "наконечник",
        "бичев",
        "коп",
        "мешочек",
        "корм",
        "билет",
        "сказан",
        "очерк",
        "торб",
    )
    # High-value opens that may warrant delaying a hunt once (not food).
    _BAG_PRIORITY_KEYWORDS = (
        "набор", "сундук", "шкатул", "пленен", "поручен", "душ",
        "древко", "наконечник", "бичев", "коп", "торб",
    )
    _BAG_SKIP_CODES = frozenset({"PUT_ON", "PUT_OFF", "NPC", "BONUS", "ADD_HP", "ADD_HP_MP"})
    _BAG_SKIP_ACTION_KW = (
        "взлом", "взломать", "подарить", "съесть", "яблок",
    )

    async def open_bag_actions(
        self,
        *,
        max_actions: int = 6,
        include_food: bool = False,
    ) -> int:
        """
        Open kits/chests and finish captives via ``action_run.php``.

        Uses ``artifact_actions[].id`` (not the dict key). Wrong id yields
        «Не задано действие!» and was why village loot never progressed.
        Returns number of successful actions.

        By default skips food (apples) and lockpick — those hung MaxFarm
        in a bag loop and starved hunts.
        """
        if await self.is_in_battle():
            return 0
        if not hasattr(self, "_bag_action_blacklist"):
            self._bag_action_blacklist: set[str] = set()
        try:
            bag = await self._client.get_bag()
        except Exception as exc:
            logger.debug("open_bag_actions get_bag: %s", exc)
            return 0

        plans: list[tuple[int, str, str, str, dict]] = []
        for a in bag.get("artifact_list") or []:
            if not isinstance(a, dict):
                continue
            art_id = str(a.get("id") or "")
            art_title = str(a.get("title") or "")
            acts = a.get("artifact_actions") or {}
            if not art_id or not isinstance(acts, dict):
                continue
            for _key, meta in acts.items():
                if not isinstance(meta, dict):
                    continue
                code = str(meta.get("code") or "")
                if code in self._BAG_SKIP_CODES and not (
                    include_food and code in {"ADD_HP", "ADD_HP_MP"}
                ):
                    continue
                real_id = self._client.bag_action_real_id(meta)
                if not real_id:
                    continue
                bl_key = f"{art_id}:{real_id}"
                if bl_key in self._bag_action_blacklist:
                    continue
                atitle = str(meta.get("title") or "")
                blob = f"{art_title} {atitle}".lower()
                if any(k in blob for k in self._BAG_SKIP_ACTION_KW) and not (
                    include_food and ("яблок" in blob or "съесть" in blob)
                ):
                    continue
                if not any(k in blob for k in self._BAG_OPEN_ITEM_KEYWORDS):
                    continue
                if str(meta.get("visible") or "1") == "0":
                    continue
                score = 0
                if any(k in blob for k in ("набор", "сундук", "шкатул", "душ")):
                    score += 50
                if "пленен" in blob or "добить" in blob:
                    score += 80  # hunt loot turn-in
                if "горшок" in blob:
                    score += 40
                if any(k in atitle.lower() for k in self._BAG_OPEN_KEYWORDS):
                    score += 10
                # Food is low priority even when allowed
                if "яблок" in blob or "съесть" in blob:
                    score -= 40
                plans.append((score, art_id, real_id, f"{art_title}/{atitle}", meta))

        plans.sort(key=lambda x: -x[0])
        opened = 0
        for _score, art_id, real_id, label, meta in plans[: max(1, int(max_actions))]:
            if await self.is_in_battle():
                break
            bl_key = f"{art_id}:{real_id}"
            try:
                resp = await self._client.run_artifact_action(art_id, real_id)
                ok = bool((resp.raw or {}).get("action_run", {}).get("ok")) or (
                    resp.status == 0 and not resp.error
                )
                err = str(resp.error or resp.redirect_error or "")
                if (not ok) and meta.get("description"):
                    resp = await self._client.run_artifact_action(
                        art_id, real_id, confirmed=True
                    )
                    ok = bool((resp.raw or {}).get("action_run", {}).get("ok")) or (
                        resp.status == 0 and not resp.error
                    )
                    err = str(resp.error or resp.redirect_error or "")
                if ok:
                    opened += 1
                    logger.info("Bag action OK: %s (id=%s)", label, real_id)
                    # Captive finish / some kits may start a fight
                    if await self.is_in_battle():
                        await self.finish_fight(timeout=180.0)
                else:
                    low = err.lower()
                    # Soft skips — expected gates, not protocol bugs
                    soft = (
                        "не задано",
                        "ключом",
                        "звания",
                        "уровн",
                        "символов",
                        "чаще",
                        "бою",
                        "професс",
                        "взломщик",
                    )
                    if any(x in low for x in soft):
                        self._bag_action_blacklist.add(bl_key)
                        logger.debug("Bag action skip %s: %s", label, err[:120])
                    else:
                        logger.info("Bag action fail %s: %s", label, err[:120])
            except Exception as exc:
                logger.debug("open_bag_actions %s: %s", label, exc)
            await asyncio.sleep(random.uniform(0.25, 0.6))

        if opened:
            logger.info("open_bag_actions: выполнено %d действи(й) из сумки.", opened)
        return opened

    # Starter duplicate junk — safe to DROP when backpack is overloaded
    _JUNK_ARTIKULS = {
        "3908", "3909", "3910", "3911",  # starter leather / club / shield
        "18012",  # apple
        "12913", "12914", "12915", "12916", "12917", "12918",  # essays
        "1012", "1013",  # empty / summon amulets
        "17990", "17991", "17992",  # broken luck tokens
        "110",  # guidebook
        "7166",  # toy wand
        "19089",  # decorative pet
        "25658",  # merchant mark
    }
    _KEEP_ARTIKULS = {
        "18209", "18208",  # quest medicine / lava
        "18013", "18014",  # starter elixirs
        "2497",  # captive Cretas (hunt turn-in)
        "33370", "17978", "17979", "17980", "24421", "47706",  # kits/chests
    }

    async def free_backpack(self, *, target_free: int = 5, max_drops: int = 25) -> int:
        """
        Drop duplicate junk when bag weight exceeds capacity.

        Returns number of items dropped. Quest / useful artikuls are kept.
        """
        try:
            bag = await self._client.get_bag()
        except Exception as exc:
            logger.debug("get_bag: %s", exc)
            return 0
        cap = bag.get("capacity") or {}
        try:
            used = int(cap.get("used") or 0)
            total = int(cap.get("total") or 0)
        except (TypeError, ValueError):
            return 0
        if total <= 0:
            return 0
        # Need headroom for travel / kit opens — free even at exact capacity.
        free_now = total - used
        want_free = max(1, int(target_free))
        if free_now >= want_free:
            return 0
        need = want_free - free_now
        # Also clear any hard overweight
        if used > total:
            need = max(need, used - total + want_free)
        arts = bag.get("artifact_list") or []
        # Prefer dropping junk artikuls; keep one of each if possible
        seen_junk: set[str] = set()
        drop_ids: list[str] = []
        for a in arts:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("artikul_id") or "")
            iid = str(a.get("id") or "")
            if not iid or aid in self._KEEP_ARTIKULS:
                continue
            if aid in self._JUNK_ARTIKULS:
                if aid in seen_junk:
                    drop_ids.append(iid)
                else:
                    seen_junk.add(aid)  # keep one copy
            if len(drop_ids) >= need:
                break
        # Still overweight? drop remaining junk including first copies
        if len(drop_ids) < need:
            for a in arts:
                if not isinstance(a, dict):
                    continue
                aid = str(a.get("artikul_id") or "")
                iid = str(a.get("id") or "")
                if not iid or aid in self._KEEP_ARTIKULS or iid in drop_ids:
                    continue
                if aid in self._JUNK_ARTIKULS:
                    drop_ids.append(iid)
                if len(drop_ids) >= need:
                    break
        # Still overweight? drop essays / empty summons by title
        if len(drop_ids) < need:
            for a in arts:
                if not isinstance(a, dict):
                    continue
                aid = str(a.get("artikul_id") or "")
                iid = str(a.get("id") or "")
                title = str(a.get("title") or "").lower()
                if not iid or aid in self._KEEP_ARTIKULS or iid in drop_ids:
                    continue
                if any(k in title for k in ("очерк", "амулет вызова")):
                    drop_ids.append(iid)
                if len(drop_ids) >= need:
                    break

        dropped = 0
        # Keep (iid, count) — stacked junk often rejects count=1 as «неверное количество»
        drop_plan: list[tuple[str, int]] = []
        by_id = {
            str(a.get("id") or ""): a
            for a in arts
            if isinstance(a, dict) and a.get("id")
        }
        for iid in drop_ids[:max_drops]:
            a = by_id.get(iid) or {}
            try:
                cnt = int(a.get("cnt") or 1)
            except (TypeError, ValueError):
                cnt = 1
            # cnt=0 gear-like rows still occupy weight — try 1
            drop_plan.append((iid, max(1, cnt) if cnt > 0 else 1))

        for iid, cnt in drop_plan:
            try:
                resp = await self._client.drop_artifact(iid, count=cnt)
                err = str(resp.redirect_error or resp.error or "")
                ca = (resp.raw or {}).get("common|action") or {}
                ca_err = str(ca.get("redirect_error") or ca.get("error") or err or "")
                # status 0 or missing error = ok; game often returns 100 with empty err on success
                ok = (
                    (resp.status == STATUS_OK and ca_err.lower() in ("", "false", "none"))
                    or (
                        int(ca.get("status") or resp.status or 0) in (0, 100)
                        and ca_err.lower() in ("", "false", "none")
                    )
                )
                if ok and "неверн" not in ca_err.lower() and "нельзя" not in ca_err.lower():
                    dropped += 1
                else:
                    # common|DROP often rejects essay stacks — action_run DROP/DESTROY works
                    freed = False
                    for act in ("DROP", "DESTROY"):
                        try:
                            r2 = await self._client.run_artifact_action(
                                iid, act, confirmed=True,
                            )
                            loc = str(
                                ((r2.raw or {}).get("action_run") or {}).get("location")
                                or ""
                            )
                            if ((r2.raw or {}).get("action_run") or {}).get("ok") or (
                                "success=1" in loc
                            ):
                                if loc:
                                    path = loc if loc.startswith("/") else "/" + loc
                                    try:
                                        await self._client._get(path)
                                    except Exception:
                                        pass
                                dropped += 1
                                freed = True
                                break
                        except Exception as exc:
                            logger.debug("action_run %s %s: %s", act, iid, exc)
                    if not freed:
                        logger.debug("DROP %s → %s", iid, ca_err[:80])
            except Exception as exc:
                logger.debug("DROP %s failed: %s", iid, exc)
            await asyncio.sleep(random.uniform(0.2, 0.5))
        if dropped:
            logger.info(
                "🗑 Освободил рюкзак: выбросил %d хлама (было %s/%s).",
                dropped, used, total,
            )
        return dropped

    # ------------------------------------------------------------------
    # Combat log
    # ------------------------------------------------------------------

    async def read_combat_log(self) -> list[CombatLogEntry]:
        """Fetch and parse the current battle log."""
        entries: list[CombatLogEntry] = []
        try:
            resp = await self._client._get("/fight.php")
            html = resp.text
            # Log lines are plain text separated by <br> or in <div class="log">
            text = re.sub(r"<br\s*/?>", "\n", html)
            text = re.sub(r"<[^>]+>", " ", text)
            for line in text.splitlines():
                line = line.strip()
                if not line or len(line) < 8:
                    continue
                if not any(kw in line.lower() for kw in
                           ("удар", "урон", "промах", "крит", "атак", "защит", "лечен")):
                    continue
                dmg_m = re.search(r"(\d+)\s*(?:урон|повреж|hp|хп)", line, re.IGNORECASE)
                entries.append(CombatLogEntry(
                    text=line[:200],
                    damage=int(dmg_m.group(1)) if dmg_m else 0,
                    is_critical="крит" in line.lower(),
                ))
        except Exception as exc:
            logger.debug("read_combat_log error: %s", exc)
        return entries[-15:]

    # ------------------------------------------------------------------
    # High-level combat tick
    # ------------------------------------------------------------------

    async def combat_tick(self, profile: FullProfile) -> BattleResult:
        """
        One full combat decision cycle:
          1. Retreat check (HP critically low)
          2. Heal / restore mana
          3. If already fighting → keep fighting
          4. Otherwise look for a battle to join
        """
        hp_pct = profile.char.hp_percent

        # 1. Critical HP — do not fight, try to heal
        if hp_pct < COMBAT.hp_retreat_threshold:
            logger.warning(
                "HP %.0f%% below retreat threshold (%.0f%%) — not engaging.",
                hp_pct, COMBAT.hp_retreat_threshold,
            )
            healed = await self.heal_if_needed(profile)
            if not healed:
                logger.info("Resting to recover HP …")
            self.session.consecutive_battles = 0
            return BattleResult.FLED

        # 2. Top up resources
        await self.heal_if_needed(profile)
        await self.restore_mana_if_needed(profile)

        # 3. Already in a fight? — finish it via fight WebSocket
        if await self.is_in_battle():
            return await self.finish_fight()

        # 4. Rest cycle if we've been grinding too long
        if await self.needs_rest():
            logger.info(
                "Fought %d battles in a row — taking a rest.",
                self.session.consecutive_battles,
            )
            self.session.consecutive_battles = 0
            await asyncio.sleep(random.uniform(30, 90))
            return BattleResult.NO_BATTLE

        # 5. Hunt farm mobs (Крэтс etc.) → fronts → arena → area hotspots
        result = await self.try_hunt_attack()
        if result in (BattleResult.JOINED, BattleResult.WIN, BattleResult.ONGOING):
            return result

        result = await self.try_join_front()
        if result == BattleResult.JOINED:
            return result

        result = await self.try_arena()
        if result == BattleResult.JOINED:
            return result

        result = await self.try_area_combat()
        if result == BattleResult.JOINED:
            return result

        return BattleResult.NO_BATTLE

    # ------------------------------------------------------------------
    # Hunt farm (live bots) + fight WS
    # ------------------------------------------------------------------

    async def finish_fight(self, *, timeout: float = 180.0) -> BattleResult:
        """Complete the current fight over wsproxy. Returns WIN/LOSE/ONGOING/ERROR."""
        if self._fight_lock.locked() and self._fight_busy:
            logger.info("finish_fight: бой уже ведётся другим таском — жду lock.")
        async with self._fight_lock:
            return await self._finish_fight_unlocked(timeout=timeout)

    async def _finish_fight_unlocked(self, *, timeout: float = 180.0) -> BattleResult:
        self._fight_busy = True
        try:
            # Seed HP into fight brain from last known profile (DwarBOT block/elixir)
            brain = None
            level = 1
            try:
                from dwar_bot.modules.battle_strategy import FightBrain, pick_hit_sequence
                from dwar_bot.modules.botmek_presets import build_fight_plan
                from dwar_bot.modules.suis_knowledge import (
                    default_suis_sequence,
                    suis_sequence_to_hit_list,
                )

                profile = self._stats.get_cached_profile()
                if profile and getattr(profile.char, "level", None):
                    level = int(profile.char.level or 1)
                botmek_fb = None
                botmek_name = ""
                want_magic = False
                if getattr(COMBAT, "botmek_enabled", True):
                    plan = build_fight_plan(
                        level=level,
                        enabled=True,
                        preset_name=str(getattr(COMBAT, "botmek_preset", "") or ""),
                    )
                    if plan:
                        botmek_fb = plan.fallback_hit_seq()
                        botmek_name = plan.preset.name
                        want_magic = bool(plan.enter_magic_stance)

                suis_fb = None
                suis_label = ""
                if getattr(COMBAT, "suis_enabled", True):
                    raw_seq = str(getattr(COMBAT, "suis_sequence", "") or "").strip()
                    if not raw_seq:
                        raw_seq = default_suis_sequence(level)
                    suis_fb = suis_sequence_to_hit_list(raw_seq)
                    suis_label = f"suis:{raw_seq}"

                seq = pick_hit_sequence(
                    None,
                    configured=getattr(COMBAT, "hit_list", None),
                    botmek_fallback=botmek_fb,
                    suis_fallback=suis_fb,
                    source_label=suis_label or botmek_name,
                )
                brain = FightBrain(
                    hit_seq=seq,
                    block_hp_percent=float(getattr(COMBAT, "hp_block_threshold", 45.0)),
                    unblock_hp_percent=float(getattr(COMBAT, "hp_unblock_threshold", 60.0)),
                    unblock_before_finisher=bool(
                        getattr(COMBAT, "unblock_before_finisher", True)
                    ),
                    want_magic_stance=want_magic,
                    botmek_preset=botmek_name or suis_label,
                )
                if profile and profile.char.hp_max > 0:
                    brain.seed_hp(int(profile.char.hp), int(profile.char.hp_max))
            except Exception as exc:
                logger.debug("fight brain seed: %s", exc)
                brain = None

            outcome: FightOutcome = await self._fight.complete_current_fight(
                timeout=timeout,
                brain=brain,
                level=level,
            )
            self.last_fight_attacks = int(outcome.attacks or 0)
            self.last_fight_damage_dealt = int(outcome.damage_dealt or 0)
            self.last_fight_damage_taken = int(outcome.damage_taken or 0)
            self.last_fight_hit_seq = str(outcome.hit_seq or "")
            if outcome.finished:
                if outcome.won:
                    self.session.wins += 1
                    self._suis_note_kill()
                    self._rfcheats_note_fight(won=True)
                    logger.info(
                        "Бой выигран (ударов=%d dmg=%d/%d seq=%s).",
                        outcome.attacks,
                        outcome.damage_dealt,
                        outcome.damage_taken,
                        outcome.hit_seq or "?",
                    )
                    if getattr(COMBAT, "post_battle_heal", True):
                        await self._post_battle_refresh()
                    return BattleResult.WIN
                self.session.losses += 1
                self._suis_failed_ops += 1
                self._rfcheats_note_fight(won=False)
                logger.info(
                    "Бой проигран (ударов=%d dmg=%d/%d).",
                    outcome.attacks, outcome.damage_dealt, outcome.damage_taken,
                )
                if getattr(COMBAT, "post_battle_heal", True):
                    await self._post_battle_refresh()
                return BattleResult.LOSE
            if outcome.error:
                logger.warning("finish_fight: %s", outcome.error)
                self._suis_error_ops += 1
                # Still in fight? treat as ongoing
                if await self.is_in_battle():
                    return BattleResult.ONGOING
                return BattleResult.ERROR
            return BattleResult.ONGOING
        except Exception as exc:
            logger.warning("finish_fight error: %s", exc)
            self._suis_error_ops += 1
            return BattleResult.ERROR
        finally:
            self._fight_busy = False

    def _suis_note_kill(self) -> None:
        if not getattr(COMBAT, "suis_enabled", True):
            return
        import time as _time
        if not self._suis_session_started:
            self._suis_session_started = _time.time()
        self._suis_session_kills += 1

    def suis_session_exhausted(self) -> bool:
        """SUIS session soft-limits: time / kills / error budget."""
        if not getattr(COMBAT, "suis_enabled", True):
            return False
        import time as _time
        from dwar_bot.modules.suis_knowledge import SUIS_DEFAULTS
        mins = int(getattr(COMBAT, "suis_session_minutes", SUIS_DEFAULTS.session_minutes) or 0)
        kills = int(getattr(COMBAT, "suis_session_kill_limit", SUIS_DEFAULTS.session_kill_limit) or 0)
        if self._suis_session_started and mins > 0:
            if (_time.time() - self._suis_session_started) >= mins * 60:
                logger.info("SUIS: лимит сессии %d мин достигнут.", mins)
                return True
        if kills > 0 and self._suis_session_kills >= kills:
            logger.info("SUIS: лимит убийств %d достигнут.", kills)
            return True
        if self._suis_error_ops >= SUIS_DEFAULTS.max_error_ops:
            logger.warning(
                "SUIS: слишком много ошибочных операций (%d).", self._suis_error_ops,
            )
            return True
        if self._suis_failed_ops >= SUIS_DEFAULTS.max_failed_ops:
            logger.warning(
                "SUIS: слишком много неудач (%d).", self._suis_failed_ops,
            )
            return True
        return False

    def reset_suis_session(self) -> None:
        self._suis_session_started = 0.0
        self._suis_session_kills = 0
        self._suis_error_ops = 0
        self._suis_failed_ops = 0

    def hygiene_remaining_sec(self) -> float:
        hy = self._hygiene
        if hy is None or not getattr(COMBAT, "rfcheats_hygiene_enabled", True):
            return 0.0
        try:
            return float(hy.remaining_break_sec())
        except Exception:
            return 0.0

    def hygiene_clear_break(self) -> None:
        hy = self._hygiene
        if hy is None:
            return
        try:
            hy.clear_break()
            # Soft-reset inflated daily counter so farm can resume today
            if float(getattr(hy, "daily_active_sec", 0) or 0) > 0:
                limit = float(hy.d.max_daily_minutes) * 60.0
                if hy.daily_active_sec >= limit:
                    hy.daily_active_sec = max(0.0, limit * 0.35)
                    hy._save()
                    logger.info(
                        "RF-Cheats hygiene: cleared break + soft daily reset → %.0fmin used",
                        hy.daily_active_sec / 60.0,
                    )
        except Exception as exc:
            logger.debug("hygiene_clear_break: %s", exc)

    async def _rfcheats_before_hunt(self) -> bool:
        """
        RF-Cheats hygiene gate. Returns True if hunt may proceed.
        On required break: arm cooldown via note_break and return False —
        never sleep minutes inside hunt (that froze Level-Up on empty ticks).
        """
        if not getattr(COMBAT, "rfcheats_hygiene_enabled", True):
            return True
        hy = self._hygiene
        if hy is None:
            return True
        try:
            decision = hy.check()
            if decision.should_pause and decision.sleep_sec > 0:
                left = float(decision.sleep_sec)
                logger.info(
                    "RF-Cheats hygiene cooldown (%s) — skip hunt %.0fs (no in-hunt sleep)",
                    decision.reason, left,
                )
                # Idempotent if already in break_until from prior check()
                if not (hy.break_until and time.time() < hy.break_until):
                    hy.note_break(left)
                return False
            think = hy.maybe_think_pause_sec()
            if think > 0:
                await asyncio.sleep(min(float(think), 3.0))
            delay = hy.action_delay_sec()
            await asyncio.sleep(min(float(delay), 2.5))
        except Exception as exc:
            logger.debug("rfcheats before hunt: %s", exc)
        return True

    def _rfcheats_note_fight(self, *, won: bool) -> None:
        hy = self._hygiene
        if hy is None or not getattr(COMBAT, "rfcheats_hygiene_enabled", True):
            return
        try:
            # Rough active time per finished fight
            hy.note_activity(45.0 if won else 30.0)
        except Exception as exc:
            logger.debug("rfcheats note fight: %s", exc)

    async def prepare_botmek_prebuff(self, profile: Optional[FullProfile] = None) -> int:
        """
        BotMek-style pre-fight drinks (гнев / мощь / ярость) before ATTACK_BOT.

        Returns number of potions consumed.
        """
        if not getattr(COMBAT, "botmek_enabled", True):
            return 0
        if not getattr(COMBAT, "botmek_prebuff", True):
            return 0
        used = 0
        try:
            from dwar_bot.modules.botmek_presets import build_fight_plan
            level = 1
            if profile and getattr(profile.char, "level", None):
                level = int(profile.char.level or 1)
            plan = build_fight_plan(
                level=level,
                enabled=True,
                preset_name=str(getattr(COMBAT, "botmek_preset", "") or ""),
            )
            if not plan or not plan.prebuff_keywords:
                return 0
            for kw in plan.prebuff_keywords:
                pot = await self._stats.find_potion([kw])
                if pot is None:
                    continue
                if await self.use_potion(pot):
                    used += 1
                    logger.info(
                        "BotMek prebuff '%s' via '%s' (preset=%s)",
                        kw, pot.title, plan.preset.name,
                    )
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                if used >= 2:
                    break
        except Exception as exc:
            logger.debug("prepare_botmek_prebuff: %s", exc)
        return used

    async def _post_battle_refresh(self) -> None:
        """DwarBOT/SUIS post-battle — potion + food ladder."""
        try:
            profile = await self._stats.read_full_profile()
        except Exception as exc:
            logger.debug("post_battle profile: %s", exc)
            return
        # Fight-lock stub often reports HP 0/0 — don't WARNING→TG spam
        try:
            fight_id = int(getattr(getattr(profile, "state", None), "fight_id", 0) or 0)
        except Exception:
            fight_id = 0
        if fight_id or not getattr(profile.char, "hp_max", 0):
            logger.debug(
                "post_battle skip heal — fight stub fight_id=%s hp=%s/%s",
                fight_id, profile.char.hp, profile.char.hp_max,
            )
            return
        if float(profile.char.hp_percent or 0) <= 0.5:
            logger.debug(
                "post_battle skip heal — HP %.0f%% right after fight (regen next tick)",
                profile.char.hp_percent,
            )
            return
        try:
            healed = await self.heal_if_needed(profile)
            if healed:
                logger.info("Post-battle heal: potion used.")
            await self.restore_mana_if_needed(profile)
            if getattr(COMBAT, "suis_post_battle_food", True) and getattr(
                COMBAT, "suis_enabled", True
            ):
                await self._suis_eat_after_battle(profile)
        except Exception as exc:
            logger.debug("post_battle_refresh: %s", exc)

    async def _suis_eat_after_battle(self, profile: FullProfile) -> bool:
        """SUIS «Еда после боя» ladder by HP% (fallback to next food if missing)."""
        from dwar_bot.modules.suis_knowledge import (
            SUIS_DEFAULTS,
            food_ladder_candidates,
        )

        hp_pct = float(profile.char.hp_percent)
        skip = float(
            getattr(COMBAT, "suis_food_skip_above", SUIS_DEFAULTS.food_skip_above_hp)
            or SUIS_DEFAULTS.food_skip_above_hp
        )
        candidates = food_ladder_candidates(hp_pct, skip_above=skip)
        if not candidates:
            return False
        for name in candidates:
            food = await self._stats.find_food(
                [name.lower(), name.split()[0].lower()]
            )
            if food is None:
                for part in name.lower().split():
                    if len(part) >= 4:
                        food = await self._stats.find_food([part])
                        if food:
                            break
            if food is None:
                continue
            ok = await self.use_potion(food)
            if ok:
                logger.info(
                    "SUIS post-battle food '%s' (wanted %s, hp=%.0f%%)",
                    food.title, name, hp_pct,
                )
                return True
        logger.debug("SUIS food ladder miss bag (hp=%.0f%% want=%s)", hp_pct, candidates)
        return False

    async def try_hunt_attack(
        self,
        *,
        name_substr: str = "",
        artikul_id: str = "",
        area_id: str = "",
    ) -> BattleResult:
        """
        Attack a free hunt_farm mob and play the fight to the end.

        For quest «Проба сил» use name_substr='крэтс'.
        """
        if self._fight_lock.locked() and self._fight_busy:
            logger.info(
                "try_hunt_attack: бой уже идёт — жду завершения вместо второго WS."
            )
        async with self._fight_lock:
            return await self._try_hunt_attack_unlocked(
                name_substr=name_substr,
                artikul_id=artikul_id,
                area_id=area_id,
            )

    async def _try_hunt_attack_unlocked(
        self,
        *,
        name_substr: str = "",
        artikul_id: str = "",
        area_id: str = "",
    ) -> BattleResult:
        try:
            if await self.is_in_battle():
                return await self._finish_fight_unlocked()

            if self.suis_session_exhausted():
                # Soft reset — do NOT sleep 20–40s here (freezes Level-Up ticks).
                logger.info("SUIS session exhausted — soft reset, skip this hunt tick")
                self.reset_suis_session()
                return BattleResult.NO_BATTLE

            if not await self._rfcheats_before_hunt():
                return BattleResult.NO_BATTLE

            # GameBots «Скрывать занятые»: fetch all, filter free, log free/busy
            raw_bots = await self._client.get_hunt_bots(
                area_id=area_id, free_only=False,
            )
            gb_on = getattr(COMBAT, "gamebots_enabled", True)
            skip_occ = bool(getattr(COMBAT, "gamebots_skip_occupied", True))
            if gb_on:
                try:
                    from dwar_bot.modules.gamebots_knowledge import (
                        filter_hunt_targets,
                        summarize_targets,
                    )
                    logger.info(
                        "GameBots map filter: %s",
                        summarize_targets(raw_bots),
                    )
                    bots = filter_hunt_targets(
                        raw_bots,
                        skip_occupied=skip_occ,
                        skip_hidden=True,
                    )
                except Exception as exc:
                    logger.debug("gamebots filter: %s", exc)
                    bots = [
                        b for b in raw_bots
                        if str(b.get("fight_id", "0") or "0") in ("0", "")
                    ]
            else:
                bots = [
                    b for b in raw_bots
                    if str(b.get("fight_id", "0") or "0") in ("0", "")
                ]
            if not bots:
                logger.debug("hunt_farm: no free bots")
                return BattleResult.NO_BATTLE

            needle = (name_substr or "").lower()
            # Prefer last successful map mob over ghost pins (Зигред in village)
            last_map = str(getattr(self, "_last_map_hunt_name", "") or "").strip()
            if last_map and needle and needle not in last_map.lower():
                # Check if pin exists; if not, switch to last_map early
                pin_present = any(
                    needle in str(b.get("name") or "").lower() for b in bots
                )
                if not pin_present:
                    logger.info(
                        "Hunt pin '%s' absent — using last map hit '%s'",
                        name_substr, last_map,
                    )
                    needle = last_map.lower()
                    name_substr = last_map
            # SUIS hunt priority when caller didn't pin a specific mob
            if (
                not needle
                and getattr(COMBAT, "suis_enabled", True)
                and getattr(COMBAT, "suis_hunt_priority", True)
            ):
                try:
                    from dwar_bot.modules.suis_knowledge import hunt_names_for_level
                    profile = self._stats.get_cached_profile()
                    lvl = int(getattr(getattr(profile, "char", None), "level", 1) or 1)
                    for prefer in hunt_names_for_level(lvl):
                        for b in bots:
                            if prefer.lower() in str(b.get("name") or "").lower():
                                needle = prefer.lower()
                                logger.info("SUIS hunt priority → '%s'", prefer)
                                break
                        if needle:
                            break
                except Exception as exc:
                    logger.debug("suis hunt priority: %s", exc)

            art = str(artikul_id or "")
            candidates: list = []
            for b in bots:
                nm = str(b.get("name") or "").lower()
                if needle and needle not in nm:
                    continue
                if art and str(b.get("artikul_id") or "") != art:
                    continue
                candidates.append(b)
            # Preferred pin missing on map (e.g. Зигред-воин in village 932) —
            # walk SUIS level list, then any free bot so Lv3 farm is not stuck.
            if not candidates and needle and not art:
                try:
                    from dwar_bot.modules.suis_knowledge import hunt_names_for_level
                    profile = self._stats.get_cached_profile()
                    lvl = int(getattr(getattr(profile, "char", None), "level", 1) or 1)
                    for prefer in hunt_names_for_level(lvl, pad=2):
                        pref_l = prefer.lower()
                        if pref_l == needle:
                            continue
                        alt = [
                            b for b in bots
                            if pref_l in str(b.get("name") or "").lower()
                        ]
                        if alt:
                            logger.info(
                                "SUIS hunt fallback '%s' → '%s' (pin absent)",
                                name_substr, prefer,
                            )
                            candidates = alt
                            needle = pref_l
                            # Remember map-real mob so next ticks don't re-pin ghost
                            try:
                                self._last_map_hunt_name = prefer
                            except Exception:
                                pass
                            break
                except Exception as exc:
                    logger.debug("suis hunt fallback: %s", exc)
            if not candidates and not art:
                candidates = list(bots)
                if needle and candidates:
                    hit_name = str(candidates[0].get("name") or "")
                    logger.info(
                        "SUIS hunt fallback → any free bot (no match for %r) → '%s'",
                        name_substr, hit_name,
                    )
                    needle = ""
                    if hit_name:
                        self._last_map_hunt_name = hit_name
            if not candidates and not needle and not art:
                candidates = list(bots)
            if not candidates:
                logger.debug(
                    "hunt_farm: no bot matching name=%r artikul=%r among %d",
                    name_substr, artikul_id, len(bots),
                )
                return BattleResult.NO_BATTLE

            retries = int(getattr(COMBAT, "gamebots_occupied_retry", 2) or 0)
            if not gb_on:
                retries = 0
            max_tries = min(len(candidates), 1 + max(0, retries))

            # BotMek share macros: drink гнев/мощь before the pull
            try:
                await self.prepare_botmek_prebuff(self._stats.get_cached_profile())
            except Exception as exc:
                logger.debug("botmek prebuff skip: %s", exc)

            last_err = ""
            for attempt, chosen in enumerate(candidates[:max_tries]):
                bot_id = str(chosen.get("id") or "")
                bot_name = str(chosen.get("name") or "?")
                if not bot_id:
                    continue

                logger.info(
                    "Нападаю на охотничьего моба '%s' (id=%s, artikul=%s)%s…",
                    bot_name, bot_id, chosen.get("artikul_id"),
                    f" try={attempt + 1}/{max_tries}" if max_tries > 1 else "",
                )
                resp = await self._client.attack_bot(bot_id)
                err = str(resp.redirect_error or resp.error or "")
                last_err = err
                logger.info(
                    "ATTACK_BOT(%s) → status=%s %s",
                    bot_id, resp.status, err[:140],
                )

                if resp.data.get("param_confirm"):
                    resp = await self._client.attack_bot(bot_id, confirmed=1)
                    err = str(resp.redirect_error or resp.error or "")
                    last_err = err
                    logger.info(
                        "ATTACK_BOT confirm(%s) → status=%s %s",
                        bot_id, resp.status, err[:140],
                    )

                await asyncio.sleep(random.uniform(0.6, 1.4))
                if await self.is_in_battle():
                    self.session.battles_joined += 1
                    self.session.consecutive_battles += 1
                    self.session.attacks_made += 1
                    result = await self._finish_fight_unlocked()
                    if result == BattleResult.WIN:
                        return BattleResult.WIN
                    if result == BattleResult.LOSE:
                        return BattleResult.LOSE
                    return BattleResult.JOINED if result != BattleResult.ERROR else result

                redir = str(resp.redirect_url or "")
                err_l = err.lower()
                if "fight" in redir.lower() or "бой" in err_l or "сражен" in err_l:
                    logger.info(
                        "ATTACK_BOT soft-miss but fight signals present — finish_fight."
                    )
                    if await self.is_in_battle():
                        self.session.battles_joined += 1
                        self.session.consecutive_battles += 1
                        self.session.attacks_made += 1
                        return await self._finish_fight_unlocked()

                occupied = False
                try:
                    from dwar_bot.modules.gamebots_knowledge import error_looks_occupied
                    occupied = error_looks_occupied(err)
                except Exception:
                    occupied = "занят" in err_l

                if occupied and attempt + 1 < max_tries:
                    logger.info(
                        "GameBots: цель занята ('%s') — следующий свободный моб.",
                        bot_name,
                    )
                    continue

                if err and (
                    "нельзя" in err_l
                    or "напад" in err_l
                    or occupied
                ):
                    await asyncio.sleep(0.8)
                    if await self.is_in_battle():
                        return await self._finish_fight_unlocked()
                    return BattleResult.ERROR

                if attempt + 1 < max_tries:
                    continue
                return BattleResult.NO_BATTLE

            if last_err:
                logger.debug("hunt_farm: all candidates failed (%s)", last_err[:80])
            return BattleResult.NO_BATTLE
        except Exception as exc:
            logger.warning("try_hunt_attack error: %s", exc)
            self._suis_error_ops += 1
            return BattleResult.ERROR

    async def try_area_combat(self) -> BattleResult:
        """
        Trigger combat-capable area hotspots (e.g. Расселина) and hunt events.

        Newbie villages often gate PvE behind an AREA action_id rather than fronts.
        """
        try:
            area = await self._client.get_area_info()
            for item in area.items:
                itype = (item.item_type or "").lower()
                name = (item.name or "").lower()
                # Skip pure travel / NPC nodes
                if itype == "npc":
                    continue
                if itype == "area" and item.code in ("COME_IN", "GO", "MOVE"):
                    continue
                if item.action_id or itype == "action" or any(
                    kw in name for kw in ("расселин", "охот", "пещер", "арен", "тренир", "бой")
                ):
                    obj_id = item.object_id or area.area_id
                    act_id = item.action_id
                    if not act_id:
                        continue
                    logger.info(
                        "Пробую боевую точку '%s' (action_id=%s).",
                        item.name, act_id,
                    )
                    resp = await self._client.run_area_action(
                        object_id=obj_id,
                        action_id=act_id,
                        link_id=item.link_id,
                        object_class=item.object_class or "AREA",
                    )
                    for line in resp.loot_lines()[:5]:
                        logger.info("🎁 [%s] %s", item.name, line[:160])
                    # After the action, check if we entered a fight
                    if await self.is_in_battle():
                        self.session.battles_joined += 1
                        self.session.consecutive_battles += 1
                        logger.info("Бой начат через точку '%s'.", item.name)
                        return BattleResult.JOINED
                    err = str(resp.redirect_error or resp.error or "")
                    if err and err.lower() not in ("false", "none", ""):
                        logger.debug("area combat '%s': %s", item.name, err)
                    elif resp.loot_lines():
                        # Loot / quest tick without entering fight — still progress
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                        return BattleResult.JOINED
                    await asyncio.sleep(random.uniform(1.0, 2.0))

            # Global arena NPC from hunt_conf (id 817 etc.)
            hunt = await self._client.get_hunt_conf()
            for npc in hunt.get("npcs", []):
                title = str(npc.get("title", "")).lower()
                if "арен" not in title and "battleground" not in title:
                    continue
                npc_id = str(npc.get("npc_id", ""))
                url = str(npc.get("url", ""))
                hash_flag = ""
                if "npc.php?" in url:
                    # trailing bare hash flag in hunt URL
                    parts = url.split("&")
                    for p in parts:
                        if "=" not in p and p and "npc.php" not in p:
                            hash_flag = p
                logger.info("Арена NPC '%s' (id=%s).", npc.get("title"), npc_id)
                await self._client.join_arena(int(npc_id), hash_flag)
                # Try chaotic confirm after opening arena NPC
                arena = await self.try_arena()
                if arena == BattleResult.JOINED:
                    return arena
                break

        except Exception as exc:
            logger.debug("try_area_combat error: %s", exc)
        return BattleResult.NO_BATTLE
