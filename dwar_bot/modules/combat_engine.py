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

        potion = await self._stats.find_hp_potion()
        if potion is None:
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
        return equipped

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
        try:
            outcome: FightOutcome = await self._fight.complete_current_fight(
                timeout=timeout,
            )
            if outcome.finished:
                if outcome.won:
                    self.session.wins += 1
                    logger.info(
                        "Бой выигран (ударов=%d).", outcome.attacks,
                    )
                    return BattleResult.WIN
                self.session.losses += 1
                logger.info("Бой проигран (ударов=%d).", outcome.attacks)
                return BattleResult.LOSE
            if outcome.error:
                logger.warning("finish_fight: %s", outcome.error)
                # Still in fight? treat as ongoing
                if await self.is_in_battle():
                    return BattleResult.ONGOING
                return BattleResult.ERROR
            return BattleResult.ONGOING
        except Exception as exc:
            logger.warning("finish_fight error: %s", exc)
            return BattleResult.ERROR

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
        try:
            if await self.is_in_battle():
                return await self.finish_fight()

            bots = await self._client.get_hunt_bots(area_id=area_id, free_only=True)
            if not bots:
                logger.debug("hunt_farm: no free bots")
                return BattleResult.NO_BATTLE

            needle = (name_substr or "").lower()
            art = str(artikul_id or "")
            chosen = None
            for b in bots:
                nm = str(b.get("name") or "").lower()
                if needle and needle not in nm:
                    continue
                if art and str(b.get("artikul_id") or "") != art:
                    continue
                chosen = b
                break
            if chosen is None and not needle and not art:
                chosen = bots[0]
            if chosen is None:
                logger.debug(
                    "hunt_farm: no bot matching name=%r artikul=%r among %d",
                    name_substr, artikul_id, len(bots),
                )
                return BattleResult.NO_BATTLE

            bot_id = str(chosen.get("id") or "")
            bot_name = str(chosen.get("name") or "?")
            if not bot_id:
                return BattleResult.ERROR

            logger.info(
                "Нападаю на охотничьего моба '%s' (id=%s, artikul=%s)…",
                bot_name, bot_id, chosen.get("artikul_id"),
            )
            resp = await self._client.attack_bot(bot_id)
            err = str(resp.redirect_error or resp.error or "")
            logger.info(
                "ATTACK_BOT(%s) → status=%s %s",
                bot_id, resp.status, err[:140],
            )

            # Confirm dialog path — retry with confirmed=1 already default
            if resp.data.get("param_confirm"):
                resp = await self._client.attack_bot(bot_id, confirmed=1)
                err = str(resp.redirect_error or resp.error or "")
                logger.info(
                    "ATTACK_BOT confirm(%s) → status=%s %s",
                    bot_id, resp.status, err[:140],
                )

            await asyncio.sleep(random.uniform(0.6, 1.4))
            if not await self.is_in_battle():
                # redirect to fight.php is also a signal
                redir = str(resp.redirect_url or "")
                if "fight" not in redir.lower():
                    if err and "напад" in err.lower():
                        return BattleResult.ERROR
                    return BattleResult.NO_BATTLE

            self.session.battles_joined += 1
            self.session.consecutive_battles += 1
            self.session.attacks_made += 1
            result = await self.finish_fight()
            if result == BattleResult.WIN:
                return BattleResult.WIN
            if result == BattleResult.LOSE:
                return BattleResult.LOSE
            return BattleResult.JOINED if result != BattleResult.ERROR else result
        except Exception as exc:
            logger.warning("try_hunt_attack error: %s", exc)
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
