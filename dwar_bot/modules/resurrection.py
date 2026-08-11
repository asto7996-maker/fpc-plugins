"""
Automatic character resurrection for «Легенда: Наследие Драконов».

When the character dies (HP=0 / ghost spirit), the bot tries in order:
  1. Finish leftover fight stub
  2. Use resurrection items (Перо Феникса, свитки воскрешения…)
  3. Click area «Возродиться» / altar / temple hotspot
  4. Probe known common|action resurrection codes
  5. Fall back to HP regen wait (non-ghost 0-HP after PvE)

Wired from ``main`` HP≈0 gate and ``CombatEngine`` after LOSE.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

# Inventory titles that resurrect a dead/ghost character (out of fight).
RESURRECT_ITEM_KW: tuple[str, ...] = (
    "перо феникса",
    "перо феник",
    "феникса",
    "свиток воскреш",
    "свиток возрожд",
    "большое перо воскрес",
    "ледяное перо",
    "теневое возрожд",
    "перо воскреш",
)

# Skip combat-buff «возрождение» mounts / pets that are not self-rez.
RESURRECT_ITEM_EXCLUDE: tuple[str, ...] = (
    "ездов", "питом", "маунт", "конь", "дракончик", "животн",
)

# Area hotspot / NPC action names.
RESURRECT_AREA_KW: tuple[str, ...] = (
    "возрод", "воскрес", "алтар", "храм", "часовн",
    "вернуться к жизни", "восстанов", "дух",
)

RESURRECT_AREA_CODES: tuple[str, ...] = (
    "RESURRECT", "REBORN", "ALTAR", "TEMPLE", "TEMPLE_IN",
    "UNDEAD", "GHOST", "GHOST_OUT", "SPIRIT", "SPIRIT_OUT",
    "COME_TO_LIFE", "REVIVE", "DEAD_OUT",
)

# Best-effort common|action probes (server ignores unknown codes).
RESURRECT_ACTION_CODES: tuple[str, ...] = (
    "RESURRECT",
    "REBORN",
    "ALTAR",
    "TEMPLE",
    "UNDEAD",
    "GHOST_OUT",
    "SPIRIT_OUT",
    "REVIVE",
)


@dataclass
class ResurrectResult:
    ok: bool = False
    method: str = ""
    detail: str = ""
    hp_after: int = 0
    hp_max_after: int = 0

    @property
    def summary(self) -> str:
        if self.ok:
            return (
                f"возрождён через {self.method} "
                f"(HP {self.hp_after}/{self.hp_max_after})"
            )
        return f"не удалось ({self.method or '—'}): {self.detail or 'нет способа'}"


@dataclass
class ResurrectSession:
    attempts: int = 0
    successes: int = 0
    last_attempt_at: float = 0.0
    last_success_at: float = 0.0
    last_method: str = ""
    deaths_seen: int = 0


def is_resurrect_item(title: str, kind: str = "") -> bool:
    blob = f"{title or ''} {kind or ''}".lower()
    if not blob.strip():
        return False
    if any(x in blob for x in RESURRECT_ITEM_EXCLUDE):
        return False
    if any(k in blob for k in RESURRECT_ITEM_KW):
        return True
    # Broad «возрожд/воскреш» only with scroll/amulet/feather kind
    if ("возрожд" in blob or "воскреш" in blob) and any(
        k in blob for k in ("свиток", "амулет", "перо", "настой", "эликсир")
    ):
        return True
    return False


def is_dead_character(
    hp: int,
    hp_max: int,
    *,
    fight_id: int = 0,
) -> bool:
    """
    True when the character is down and not mid-fight.

    Fight stub with HP=0 is still «in battle» — finish fight first.
    """
    if int(hp_max or 0) <= 0:
        return False
    if int(hp or 0) > 0:
        return False
    if int(fight_id or 0) > 0:
        return False
    return True


def area_looks_like_ghost(area_title: str = "", items: Sequence[Any] = ()) -> bool:
    title = (area_title or "").lower()
    if any(k in title for k in ("кладбищ", "дух", "призра", "мертв", "храм")):
        return True
    for it in items or ():
        name = str(getattr(it, "name", "") or "")
        code = str(getattr(it, "code", "") or "")
        blob = f"{name} {code}".lower()
        if any(k in blob for k in RESURRECT_AREA_KW):
            return True
        if code.upper() in RESURRECT_AREA_CODES:
            return True
    return False


class ResurrectionEngine:
    """
    Auto-resurrect orchestrator.

    Expects a duck-typed bot with ``_client``, ``stats``, ``combat``,
    ``timers``, ``_char``, ``_state``, ``_profile``, ``settings``, ``notify``.
    Can also be driven with only ``client`` + ``stats`` for unit tests.
    """

    def __init__(self) -> None:
        self.session = ResurrectSession()
        self._min_gap_s = 8.0
        self._last_fail_detail = ""

    def reset_session(self) -> None:
        self.session = ResurrectSession()

    def should_try(self, bot: Any) -> bool:
        farm = getattr(getattr(bot, "settings", None), "farm", None)
        if farm is not None and not getattr(farm, "auto_resurrect", True):
            return False
        char = getattr(bot, "_char", None)
        state = getattr(bot, "_state", None)
        if char is None:
            return False
        hp = int(getattr(char, "hp", 0) or 0)
        hp_max = int(getattr(char, "hp_max", 0) or 0)
        fight_id = int(getattr(state, "fight_id", 0) or 0) if state else 0
        return is_dead_character(hp, hp_max, fight_id=fight_id)

    async def ensure_alive(self, bot: Any) -> ResurrectResult:
        """
        If dead — attempt resurrection. If already alive — ok noop.
        """
        char = getattr(bot, "_char", None)
        if char is None:
            return ResurrectResult(ok=False, method="none", detail="no char")

        hp = int(getattr(char, "hp", 0) or 0)
        hp_max = int(getattr(char, "hp_max", 0) or 0)
        if hp_max > 0 and hp > 0:
            return ResurrectResult(
                ok=True, method="already_alive",
                hp_after=hp, hp_max_after=hp_max,
            )

        if not self.should_try(bot):
            return ResurrectResult(ok=False, method="disabled", detail="auto_resurrect off or in fight")

        now = time.time()
        if now - self.session.last_attempt_at < self._min_gap_s:
            return ResurrectResult(
                ok=False, method="cooldown",
                detail=f"wait {self._min_gap_s:.0f}s",
            )

        self.session.last_attempt_at = now
        self.session.attempts += 1
        self.session.deaths_seen += 1

        # Track death for achievements / streak
        try:
            ach = getattr(bot, "achievements", None)
            if ach:
                ach.note_death()
        except Exception:
            pass

        logger.warning(
            "Death detected (HP %d/%d area=%s) — auto-resurrect…",
            hp, hp_max, getattr(getattr(bot, "_state", None), "area_id", "?"),
        )

        # 0) Finish fight if somehow still flagged
        try:
            if await bot.combat.is_in_battle():
                await bot.combat.finish_fight(timeout=120.0)
                await self._refresh(bot)
                if int(getattr(bot._char, "hp", 0) or 0) > 0:
                    return self._success(bot, "finish_fight")
        except Exception as exc:
            logger.debug("resurrect finish_fight: %s", exc)

        # 1) Inventory resurrection items
        result = await self._try_inventory_items(bot)
        if result.ok:
            return result

        # 2) Area hotspot «Возродиться»
        result = await self._try_area_actions(bot)
        if result.ok:
            return result

        # 3) Probe common|action codes
        result = await self._try_action_codes(bot)
        if result.ok:
            return result

        # 4) Regen wait — works when death is «0 HP» without ghost lock
        result = await self._try_regen_wait(bot)
        if result.ok:
            return result

        detail = self._last_fail_detail or "все способы исчерпаны"
        logger.error("Auto-resurrect FAILED: %s", detail)
        try:
            if getattr(bot.settings, "notify", None) and bot.settings.notify.hp_low:
                await bot.notify(
                    f"💀 Не удалось возродиться автоматически.\n{detail}",
                    "hp_low",
                )
        except Exception:
            pass
        return ResurrectResult(ok=False, method="failed", detail=detail)

    async def _refresh(self, bot: Any) -> None:
        try:
            bot._profile = await bot.stats.read_full_profile()
            bot._char = bot._profile.char
            bot._state = bot._profile.state
        except Exception:
            try:
                bot._char = await bot._client.get_char_stats()
                bot._state = await bot._client.get_state()
            except Exception as exc:
                logger.debug("resurrect refresh: %s", exc)

    def _success(self, bot: Any, method: str, detail: str = "") -> ResurrectResult:
        char = bot._char
        hp = int(getattr(char, "hp", 0) or 0)
        hp_max = int(getattr(char, "hp_max", 0) or 0)
        self.session.successes += 1
        self.session.last_success_at = time.time()
        self.session.last_method = method
        result = ResurrectResult(
            ok=True, method=method, detail=detail,
            hp_after=hp, hp_max_after=hp_max,
        )
        logger.info("Auto-resurrect OK: %s", result.summary)
        return result

    async def _notify_ok(self, bot: Any, result: ResurrectResult) -> None:
        try:
            if getattr(bot.settings, "notify", None) and bot.settings.notify.hp_low:
                await bot.notify(
                    f"✨ Возрождение: <b>{result.method}</b>\n"
                    f"❤️ HP {result.hp_after}/{result.hp_max_after}",
                    "hp_low",
                )
        except Exception:
            pass

    async def _try_inventory_items(self, bot: Any) -> ResurrectResult:
        client = bot._client
        try:
            profile = getattr(bot, "_profile", None) or await bot.stats.read_full_profile()
            inventory = list(getattr(profile, "inventory", []) or [])
        except Exception as exc:
            self._last_fail_detail = f"inventory: {exc}"
            return ResurrectResult(ok=False, method="inventory", detail=str(exc))

        # Also scan raw bag for action metas (phoenix feather may not be is_potion)
        bag_arts: list[dict] = []
        try:
            bag = await client.get_bag()
            bag_arts = list(bag.get("artifact_list") or [])
        except Exception as exc:
            logger.debug("resurrect get_bag: %s", exc)

        # Prefer explicit inventory Artifact objects
        candidates: list[tuple[str, str, str]] = []  # art_id, title, source
        for art in inventory:
            title = str(getattr(art, "title", "") or "")
            kind = str(getattr(art, "kind", "") or "")
            art_id = str(getattr(art, "art_id", "") or "")
            if art_id and is_resurrect_item(title, kind):
                candidates.append((art_id, title, "inv"))

        for a in bag_arts:
            if not isinstance(a, dict):
                continue
            title = str(a.get("title") or "")
            kind = str(a.get("kind") or a.get("type") or "")
            art_id = str(a.get("id") or "")
            if art_id and is_resurrect_item(title, kind):
                if not any(c[0] == art_id for c in candidates):
                    candidates.append((art_id, title, "bag"))

        for art_id, title, _src in candidates:
            # a) common|DRINK
            try:
                from dwar_bot.modules.combat_engine import CODE_DRINK
                resp = await client.common_action(CODE_DRINK, {"artifact_id": art_id})
                err = str(resp.redirect_error or resp.error or "")
                await self._refresh(bot)
                if int(getattr(bot._char, "hp", 0) or 0) > 0:
                    r = self._success(bot, "item_drink", title)
                    await self._notify_ok(bot, r)
                    return r
                if err and err.lower() not in ("false", "none", ""):
                    logger.debug("resurrect DRINK '%s': %s", title, err[:120])
            except Exception as exc:
                logger.debug("resurrect DRINK '%s': %s", title, exc)

            # b) bag action_run USE / ACTIVATE / any non-PUT action
            try:
                meta_list = []
                for a in bag_arts:
                    if str(a.get("id") or "") != art_id:
                        continue
                    acts = a.get("artifact_actions") or {}
                    if isinstance(acts, dict):
                        meta_list = list(acts.values())
                    break
                for meta in meta_list:
                    if not isinstance(meta, dict):
                        continue
                    code = str(meta.get("code") or "").upper()
                    atitle = str(meta.get("title") or "").lower()
                    if code in {"PUT_ON", "PUT_OFF", "DROP", "DESTROY", "NPC"}:
                        continue
                    if code and code not in {
                        "USE", "ACTIVATE", "DRINK", "APPLY", "RESURRECT", "REBORN", ""
                    } and not any(k in atitle for k in ("использ", "возрод", "воскрес", "актив")):
                        # Still try generic use-like codes
                        if "USE" not in code and "ACT" not in code:
                            continue
                    real_id = client.bag_action_real_id(meta)
                    if not real_id:
                        continue
                    resp = await client.run_artifact_action(
                        art_id, real_id, confirmed=True,
                    )
                    await self._refresh(bot)
                    if int(getattr(bot._char, "hp", 0) or 0) > 0:
                        r = self._success(bot, "item_action", f"{title}/{code or atitle}")
                        await self._notify_ok(bot, r)
                        return r
                    err = str(resp.redirect_error or resp.error or "")
                    if err:
                        logger.debug("resurrect action '%s': %s", title, err[:120])
            except Exception as exc:
                logger.debug("resurrect bag action '%s': %s", title, exc)

            await asyncio.sleep(0.4)

        self._last_fail_detail = "нет/не сработали предметы возрождения"
        return ResurrectResult(ok=False, method="inventory")

    async def _try_area_actions(self, bot: Any) -> ResurrectResult:
        client = bot._client
        try:
            info = await client.get_area_info()
        except Exception as exc:
            self._last_fail_detail = f"area: {exc}"
            return ResurrectResult(ok=False, method="area", detail=str(exc))

        items = list(info.items or [])
        # Sort: explicit resurrect names first
        def _score(it: Any) -> int:
            blob = f"{getattr(it, 'name', '')} {getattr(it, 'code', '')}".lower()
            sc = 0
            if "возрод" in blob or "воскрес" in blob:
                sc += 50
            if "алтар" in blob or "храм" in blob:
                sc += 30
            code = str(getattr(it, "code", "") or "").upper()
            if code in RESURRECT_AREA_CODES:
                sc += 40
            return sc

        ranked = sorted(items, key=_score, reverse=True)
        for it in ranked:
            if _score(it) <= 0:
                continue
            name = str(getattr(it, "name", "") or "")
            code = str(getattr(it, "code", "") or "")
            try:
                # Hotspot with object/action ids
                oid = str(getattr(it, "object_id", "") or getattr(it, "item_id", "") or "")
                aid = str(getattr(it, "action_id", "") or "")
                if oid and aid:
                    resp = await client.run_area_action(
                        oid, aid,
                        link_id=str(getattr(it, "link_id", "") or ""),
                        object_class=str(getattr(it, "object_class", "") or "AREA") or "AREA",
                    )
                    err = str(resp.redirect_error or resp.error or "")
                    await self._refresh(bot)
                    if int(getattr(bot._char, "hp", 0) or 0) > 0:
                        r = self._success(bot, "area_hotspot", name or code)
                        await self._notify_ok(bot, r)
                        return r
                    if err:
                        logger.debug("resurrect hotspot '%s': %s", name, err[:100])

                # common|action with code + area_id
                if code:
                    extra: dict[str, str] = {}
                    if getattr(it, "area_id", None):
                        extra["area_id"] = str(it.area_id)
                    if getattr(it, "npc_id", None):
                        extra["npc_id"] = str(it.npc_id)
                    resp = await client.common_action(code, extra or None)
                    await self._refresh(bot)
                    if int(getattr(bot._char, "hp", 0) or 0) > 0:
                        r = self._success(bot, "area_code", f"{code}/{name}")
                        await self._notify_ok(bot, r)
                        return r
            except Exception as exc:
                logger.debug("resurrect area '%s': %s", name, exc)
            await asyncio.sleep(0.3)

        self._last_fail_detail = "в локации нет кнопки возрождения"
        return ResurrectResult(ok=False, method="area")

    async def _try_action_codes(self, bot: Any) -> ResurrectResult:
        client = bot._client
        area_id = str(getattr(getattr(bot, "_state", None), "area_id", "") or "")
        for code in RESURRECT_ACTION_CODES:
            try:
                extra = {"area_id": area_id} if area_id else None
                resp = await client.common_action(code, extra)
                err = str(resp.redirect_error or resp.error or "")
                await self._refresh(bot)
                if int(getattr(bot._char, "hp", 0) or 0) > 0:
                    r = self._success(bot, "action_code", code)
                    await self._notify_ok(bot, r)
                    return r
                if err and "не задано" not in err.lower():
                    logger.debug("resurrect code %s: %s", code, err[:100])
            except Exception as exc:
                logger.debug("resurrect code %s: %s", code, exc)
            await asyncio.sleep(0.2)
        self._last_fail_detail = "коды common|action не сработали"
        return ResurrectResult(ok=False, method="action_codes")

    async def _try_regen_wait(self, bot: Any) -> ResurrectResult:
        """Last resort: wait for HP tick (works if not ghost-locked)."""
        farm = getattr(getattr(bot, "settings", None), "farm", None)
        target = 40.0
        if farm is not None:
            target = max(40.0, float(getattr(farm, "hp_heal", 55) or 55) * 0.7)
        try:
            await bot.timers.wait_for_hp(target_percent=target, max_wait=90)
        except Exception:
            await asyncio.sleep(20)
        await self._refresh(bot)
        if int(getattr(bot._char, "hp", 0) or 0) > 0:
            r = self._success(bot, "regen_wait")
            await self._notify_ok(bot, r)
            return r
        self._last_fail_detail = "реген не поднял HP (возможно дух/призрак)"
        return ResurrectResult(ok=False, method="regen_wait")
