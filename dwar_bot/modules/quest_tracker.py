"""
Quest tracker — story progression via the dwar.ru JSON NPC API.

Primary path (Flash-free)
-------------------------
* ``npc|quests``  — list available quest points on an NPC
* ``npc|point``   — read current dialogue node + ``child_list`` answers
* ``npc|answer``  — choose a child point to progress

Also discovers NPCs from:
* ``area.php`` items (type=npc) — e.g. Вождь Торгор
* ``hunt_conf.php`` global event NPCs
* ``common|action?code=NPC`` story pointer
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

from dwar_bot.config import DELAY_DIALOGUE
from dwar_bot.core.game_client import DwarGameClient, STATUS_OK

logger = logging.getLogger(__name__)

CODE_NPC = "NPC"
CODE_QUEST_COMPLETE = "QUEST_COMPLETE"

POSITIVE_KEYWORDS = [
    "принять", "принимаю", "согласен", "согласна", "да", "конечно",
    "продолжить", "далее", "дальше", "получить", "взять", "хорошо",
    "готов", "готова", "начать", "выполнить", "сдать", "завершить",
    "награда", "забрать", "понятно", "лиха беда", "вперёд", "вперед",
]
NEGATIVE_KEYWORDS = [
    "отказ", "нет", "позже", "уйти", "выйти", "закрыть", "отменить",
    "вернуться", "назад", "обратно", "к квестам", "отмена",
]


class QuestStatus(Enum):
    ACTIVE = auto()
    COMPLETABLE = auto()
    COMPLETED = auto()
    AVAILABLE = auto()
    UNKNOWN = auto()


@dataclass
class Quest:
    quest_id: str = ""
    title: str = ""
    description: str = ""
    status: QuestStatus = QuestStatus.UNKNOWN
    npc_id: str = ""


@dataclass
class NpcInfo:
    npc_id: str = ""
    title: str = ""
    url: str = ""
    time_left: int = 0
    is_global: bool = True
    link_id: str = "0"
    f_id: str = "0"
    area_id: str = "0"


@dataclass
class DialogueOption:
    text: str = ""
    url: str = ""
    code: str = ""
    quest_id: str = ""
    point_id: str = ""

    @property
    def is_positive(self) -> bool:
        t = self.text.lower()
        return any(kw in t for kw in POSITIVE_KEYWORDS)

    @property
    def is_negative(self) -> bool:
        t = self.text.lower()
        return any(kw in t for kw in NEGATIVE_KEYWORDS)


@dataclass
class QuestStats:
    dialogues_handled: int = 0
    quests_accepted: int = 0
    quests_completed: int = 0
    npcs_visited: int = 0


class QuestTracker:
    """Handles NPC interaction and quest progression over HTTP."""

    def __init__(self, client: DwarGameClient) -> None:
        self._client = client
        self.session = QuestStats()
        self._visited_npcs: set[str] = set()
        self._exhausted_dialogues: set[str] = set()
        self._last_quest_signature: str = ""
        self._answered_points: set[str] = set()
        # key → unix ts; soft-banned dialogues become available again
        self._soft_ban_until: dict[str, float] = {}
        # After type=2 needs a hunt kill — remembered for brain / retry
        self._pending_type2: dict[str, str] = {}
        self.pending_hunt_mob: str = ""
        # World objective after dialogue closes to area.php (heal wounded, gather, …)
        # Survives clear_exhausted so we do not re-spam the same NPC.
        self.pending_world_objective: dict[str, Any] = {}
        self._world_objective_keys: set[str] = set()
        self._world_obj_last_try: float = 0.0

    def clear_hunt_gate(self) -> None:
        """Clear quest kill gate after turn-in or travel unlock."""
        self.pending_hunt_mob = ""
        self._pending_type2 = {}

    def clear_world_objective(self, reason: str = "") -> None:
        if self.pending_world_objective:
            logger.info(
                "World objective cleared (%s): %s",
                reason or "done",
                self.pending_world_objective.get("kind"),
            )
        self.pending_world_objective = {}
        self._world_objective_keys.clear()
        self._world_obj_last_try = 0.0

    def has_world_objective(self, kind: str = "") -> bool:
        if not self.pending_world_objective:
            return False
        if kind:
            return self.pending_world_objective.get("kind") == kind
        return True

    def world_objective_npc_ids(self) -> set[str]:
        """NPC ids that must stay soft-banned while a world objective is active."""
        out: set[str] = set()
        npc = str((self.pending_world_objective or {}).get("npc_id") or "")
        if npc:
            out.add(npc)
        for key in self._world_objective_keys:
            parts = str(key).split(":")
            if len(parts) >= 2 and parts[1]:
                out.add(parts[1])
        return out

    @staticmethod
    def detect_world_objective_kind(*texts: str) -> str:
        """Infer world-objective kind from NPC dialogue snippets."""
        blob = " ".join(str(t or "") for t in texts).lower()
        if any(
            kw in blob
            for kw in (
                "ранен", "излечен", "излечени", "снадоб", "раздал",
                "ополчен", "поле боя",
            )
        ):
            return "heal_wounded"
        if any(kw in blob for kw in ("лав", "ведр", "собер", "добыч")):
            return "gather"
        return ""

    def set_world_objective(
        self,
        *,
        kind: str,
        title: str = "",
        npc_id: str = "",
        artikul_id: str = "",
        artifact_title: str = "",
        quest_id: str = "",
        area_id: str = "",
        ban_key: str = "",
        ban_sec: float = 1800.0,
    ) -> None:
        # World goals are outside dialogue — kill-gate / type=2 no longer apply.
        self.clear_hunt_gate()
        self.pending_world_objective = {
            "kind": kind,
            "title": title,
            "npc_id": str(npc_id or ""),
            "artikul_id": str(artikul_id or ""),
            "artifact_title": artifact_title,
            "quest_id": str(quest_id or ""),
            "area_id": str(area_id or ""),
            "set_at": time.time(),
        }
        if ban_key:
            self._world_objective_keys.add(ban_key)
            self._exhausted_dialogues.add(ban_key)
            self._soft_ban_until[ban_key] = time.time() + max(120.0, ban_sec)
        logger.warning(
            "World objective SET kind=%s title=%r artikul=%s npc=%s ban=%ss",
            kind, title, artikul_id or "—", npc_id or "—", int(ban_sec),
        )

    async def pursue_world_objective(self) -> bool:
        """Attempt the active world objective once (rate-limited)."""
        obj = self.pending_world_objective
        if not obj:
            return False
        now = time.time()
        if now - self._world_obj_last_try < 45.0:
            return False
        self._world_obj_last_try = now
        kind = str(obj.get("kind") or "")
        if kind == "heal_wounded":
            return await self._try_heal_wounded(obj)
        logger.info("World objective '%s' — no automated protocol yet.", kind)
        return False

    async def _try_heal_wounded(self, obj: dict[str, Any]) -> bool:
        """Best-effort: use quest medicine on bots / via common USE."""
        artikul = str(obj.get("artikul_id") or "18209")
        art_id = ""
        try:
            bag = await self._client.get_bag()
        except Exception as exc:
            logger.debug("heal_wounded get_bag: %s", exc)
            return False
        for a in bag.get("artifact_list") or []:
            if str(a.get("artikul_id") or "") == artikul:
                art_id = str(a.get("id") or a.get("artifact_id") or "")
                if not obj.get("artifact_title"):
                    obj["artifact_title"] = str(a.get("title") or a.get("name") or "")
                break
        if not art_id:
            logger.warning(
                "heal_wounded: medicine artikul=%s not in bag — keep farming / wait.",
                artikul,
            )
            return False

        attempts: list[tuple[str, dict[str, Any]]] = [
            ("USE", {"artifact_id": art_id}),
            ("DRINK", {"artifact_id": art_id}),
        ]
        try:
            st = await self._client.get_state()
            bots = await self._client.get_hunt_bots(st.area_id or obj.get("area_id") or "")
        except Exception:
            bots = []
        for bot in bots[:6]:
            bid = str(bot.get("id") or "")
            if not bid:
                continue
            name = str(bot.get("name") or "")
            # Prefer non-aggressive / wounded-looking targets if labeled
            attempts.append((
                "USE",
                {
                    "artifact_id": art_id,
                    "object_class": "BOT",
                    "object_id": bid,
                    "bot_id": bid,
                },
            ))
            logger.debug("heal target candidate bot=%s %s", bid, name[:40])

        for code, extra in attempts:
            try:
                resp = await self._client.common_action(code, extra)
            except Exception as exc:
                logger.debug("heal_wounded %s failed: %s", code, exc)
                continue
            err = str(resp.error or getattr(resp, "redirect_error", "") or "")
            logger.info(
                "heal_wounded %s %s → status=%s err=%s",
                code, {k: extra[k] for k in extra if k != "artifact_id"},
                resp.status, (err or "—")[:80],
            )
            if resp.status == STATUS_OK and not (
                err and err.lower() not in ("false", "none", "")
                and "не задано" in err.lower()
            ):
                # Ambiguous OK without error — treat as progress signal
                if not err or err.lower() in ("false", "none", ""):
                    logger.warning("heal_wounded: USE accepted — clearing objective.")
                    self.clear_world_objective("medicine_used")
                    return True
            await asyncio.sleep(random.uniform(0.3, 0.7))

        logger.info(
            "heal_wounded: HTTP USE not accepted yet (Flash-only?). "
            "NPC %s stays banned; farming until turn-in possible.",
            obj.get("npc_id") or "?",
        )
        return False

    def has_pending_type2(self) -> bool:
        return bool(self._pending_type2)

    async def retry_pending_type2(self) -> int:
        """
        After a hunt kill, re-submit the type=2 answer that was waiting.

        Returns dialogue steps advanced (0 if nothing pending / failed).
        """
        pending = dict(self._pending_type2 or {})
        if not pending:
            return 0
        npc_id = str(pending.get("npc_id") or "")
        to_point = str(pending.get("to_point") or "")
        from_point = str(pending.get("from_point") or "")
        child_id = str(pending.get("child_id") or "")
        quest_id = pending.get("quest_id") or 0
        if not npc_id or not to_point:
            return 0
        logger.info(
            "Сдача type=2 после охоты: NPC %s msg %s → %s (mob=%s)",
            npc_id, child_id or from_point, to_point, pending.get("mob_name"),
        )
        # Soft-ban keys must not block the turn-in
        self._answered_points.discard(child_id)
        self._answered_points.discard(to_point)
        for key in list(self._exhausted_dialogues):
            if f":{npc_id}:" in f":{key}:":
                self._exhausted_dialogues.discard(key)
                self._soft_ban_until.pop(key, None)

        global_npc = int(pending.get("global_npc") or 0)
        link_id = str(pending.get("link_id") or "0")
        f_id = str(pending.get("f_id") or "0")
        href = str(pending.get("href") or "")
        try:
            # Prefer HTML ref= answer — same path as live dialogue for type=2 messages
            if child_id:
                try:
                    html_ok, _, redir = await self._client.npc_html_answer(
                        npc_id,
                        child_id,
                        global_npc=global_npc,
                        link_id=link_id,
                        f_id=f_id,
                        href=href,
                    )
                    if html_ok:
                        self.session.dialogues_handled += 1
                        self.clear_hunt_gate()
                        logger.info(
                            "type=2 принят (HTML%s) — охота больше не нужна.",
                            f" → {redir[:40]}" if redir else "",
                        )
                        return 1
                except Exception as exc:
                    logger.debug("retry_pending_type2 html: %s", exc)

            raw_ans = await self._client.npc_answer(
                npc_id,
                quest_id=quest_id,
                point_id=to_point,
                global_npc=global_npc,
                link_id=link_id,
                f_id=f_id,
                subpoint_id=to_point,
            )
            ans = raw_ans.get("npc|answer") or {}
            if int(ans.get("status", 0) or 0) != STATUS_OK:
                raw_ans = await self._client.npc_answer(
                    npc_id,
                    quest_id=quest_id,
                    point_id=from_point or child_id,
                    global_npc=global_npc,
                    link_id=link_id,
                    f_id=f_id,
                    subpoint_id=child_id or to_point,
                )
                ans = raw_ans.get("npc|answer") or {}
            ok = int(ans.get("status", 0) or 0) == STATUS_OK
            if ok:
                self.session.dialogues_handled += 1
                self.clear_hunt_gate()
                awards = (raw_ans.get("npc|point") or {}).get("award_list") or []
                for a in awards[:5]:
                    logger.info("  Награда (type=2): %s", a)
                logger.info("type=2 принят — охота больше не нужна для этого этапа.")
                return 1
            logger.info(
                "type=2 ещё не принят: %s — вернусь к Вождю через walk_npc",
                ans.get("error") or ans.get("status"),
            )
            return 0
        except Exception as exc:
            logger.warning("retry_pending_type2: %s", exc)
            return 0

    def _infer_hunt_mob_name(self, *texts: str) -> str:
        """Extract mob name from a kill-objective line. Empty = not a hunt gate."""
        blob = " ".join(t for t in texts if t).strip()
        if not blob:
            return ""
        low = blob.lower()
        # Enlistment / thanks / travel replies are NOT kill objectives
        if any(k in low for k in ("командован", "честь для", "готов служить", "примите")):
            return ""
        killish = any(
            k in low
            for k in ("поверж", "убит", "побежд", "сражён", "сражен", "хвост", "туш", "напад")
        )
        if not killish and "крэт" not in low:
            return ""
        if "крэт" in low or "крейт" in low or "krats" in low:
            return "Крэтс" if killish else ""
        m = re.search(
            r"([A-Za-zА-Яа-яЁё]{3,})\s+(?:поверж|убит|побежд|сраж)",
            blob,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)
        return ""

    async def _attack_hunt_for_quest(self, mob_name: str) -> bool:
        """Attack a free hunt_farm bot by name. Returns True if fight likely started."""
        self.pending_hunt_mob = mob_name or "Крэтс"
        try:
            st = await self._client.get_state()
            if st.fight_id:
                logger.info(
                    "Уже в бою fight_id=%s — сначала завершить бой.",
                    st.fight_id,
                )
                return True
            bots = await self._client.get_hunt_bots(
                area_id=st.area_id, free_only=True,
            )
            needle = (mob_name or "").lower()
            chosen = None
            for b in bots:
                if needle and needle not in str(b.get("name") or "").lower():
                    continue
                chosen = b
                break
            if chosen is None and bots and not needle:
                chosen = bots[0]
            if chosen is None:
                logger.info(
                    "hunt_farm: нет свободных '%s' (bots=%d)",
                    mob_name, len(bots),
                )
                return False
            bot_id = str(chosen.get("id") or "")
            logger.info(
                "Квест требует убийства → ATTACK_BOT live id=%s name=%s",
                bot_id, chosen.get("name"),
            )
            atk = await self._client.attack_bot(bot_id)
            err = str(atk.redirect_error or atk.error or "")
            logger.info(
                "ATTACK_BOT(%s) → status=%s %s",
                bot_id, atk.status, err[:140],
            )
            if atk.data.get("param_confirm"):
                atk = await self._client.attack_bot(bot_id, confirmed=1)
                err = str(atk.redirect_error or atk.error or "")
                logger.info(
                    "ATTACK_BOT confirm(%s) → status=%s %s",
                    bot_id, atk.status, err[:140],
                )
            await asyncio.sleep(random.uniform(0.5, 1.0))
            st2 = await self._client.get_state()
            if st2.fight_id:
                return True
            redir = str(atk.redirect_url or "")
            if "fight" in redir.lower():
                return True
            if err and "напад" in err.lower():
                return False
            return atk.status == STATUS_OK
        except Exception as exc:
            logger.warning("_attack_hunt_for_quest: %s", exc)
            return False

    def _reset_exhausted_if_quests_changed(self, quests: list[Quest]) -> None:
        sig = "|".join(sorted(f"{q.quest_id}:{q.status.name}" for q in quests))
        if sig != self._last_quest_signature:
            preserve = set(self._world_objective_keys)
            if self._exhausted_dialogues - preserve:
                logger.debug(
                    "Quest state changed — re-enabling NPC dialogues "
                    "(kept %d world-objective bans).",
                    len(preserve),
                )
            self._exhausted_dialogues = set(preserve)
            self._visited_npcs.clear()
            self._soft_ban_until = {
                k: v for k, v in self._soft_ban_until.items() if k in preserve
            }
            self._last_quest_signature = sig

    def _purge_expired_soft_bans(self) -> None:
        now = time.time()
        expired = [k for k, until in self._soft_ban_until.items() if until <= now]
        for k in expired:
            # Keep world-objective bans alive while the objective is pending
            if k in self._world_objective_keys and self.pending_world_objective:
                self._soft_ban_until[k] = now + 900.0
                self._exhausted_dialogues.add(k)
                continue
            self._soft_ban_until.pop(k, None)
            self._exhausted_dialogues.discard(k)
            # Allow retrying the type=2 message after soft ban
            parts = str(k).split(":")
            if len(parts) >= 2:
                # answered point ids are global across NPCs — clear all to retry
                self._answered_points.clear()

    def exhausted_npc_ids(self) -> set[str]:
        """NPC ids whose dialogue was marked exhausted this session."""
        self._purge_expired_soft_bans()
        out: set[str] = set()
        for key in self._exhausted_dialogues:
            # key format: "{global_npc}:{npc_id}:{link_id}"
            parts = str(key).split(":")
            if len(parts) >= 2 and parts[1]:
                out.add(parts[1])
        out.update(self.world_objective_npc_ids())
        return out

    def mark_npc_exhausted(
        self,
        npc_id: str,
        *,
        global_npc: int = 0,
        link_id: str = "0",
    ) -> None:
        key = f"{global_npc}:{npc_id}:{link_id}"
        self._exhausted_dialogues.add(key)
        # Default soft ban so farm unlock can retry
        self._soft_ban_until.setdefault(key, time.time() + 180.0)

    def clear_exhausted(self, *, local_only: bool = False) -> int:
        """Re-enable NPC dialogues. Returns how many keys were cleared.

        Keys tied to an active ``pending_world_objective`` are preserved so
        the bot does not re-enter the same NPC dialogue loop (e.g. heal wounded).
        """
        preserve = set(self._world_objective_keys)
        if not local_only:
            n = len(self._exhausted_dialogues - preserve)
            self._exhausted_dialogues = set(preserve)
            self._answered_points.clear()
            self._soft_ban_until = {
                k: v for k, v in self._soft_ban_until.items() if k in preserve
            }
            return n
        # local_only: drop local (global_npc=0) keys except world-objective bans
        keep = {
            k for k in self._exhausted_dialogues
            if str(k).startswith("1:") or k in preserve
        }
        n = len(self._exhausted_dialogues) - len(keep)
        self._exhausted_dialogues = keep
        self._soft_ban_until = {
            k: v for k, v in self._soft_ban_until.items() if k in keep
        }
        self._answered_points.clear()
        return n

    # ------------------------------------------------------------------
    # NPC discovery
    # ------------------------------------------------------------------

    async def list_available_npcs(
        self,
        *,
        area: Any = None,
        hunt: Optional[dict] = None,
    ) -> list[NpcInfo]:
        """
        Discover NPCs. Pass already-fetched ``area`` / ``hunt`` to avoid
        duplicate area.php / hunt_conf.php requests within one bot tick.
        """
        npcs: list[NpcInfo] = []
        seen: set[str] = set()

        # Local NPCs from area items
        try:
            if area is None:
                area = await self._client.get_area_info()
            for item in area.items:
                if item.item_type == "npc" and item.npc_id:
                    key = f"local:{item.npc_id}:{item.link_id}"
                    if key in seen:
                        continue
                    seen.add(key)
                    npcs.append(NpcInfo(
                        npc_id=str(item.npc_id),
                        title=item.name or f"NPC #{item.npc_id}",
                        url=item.href or item.link_href,
                        is_global=False,
                        link_id=item.link_id or "0",
                        f_id=item.f_id or "0",
                        area_id=area.area_id,
                    ))
            if area.npc_id and str(area.npc_id) not in {n.npc_id for n in npcs if not n.is_global}:
                npcs.append(NpcInfo(
                    npc_id=str(area.npc_id),
                    title=f"Локальный NPC #{area.npc_id}",
                    url=f"npc.php?area_id={area.area_id}&npc_id={area.npc_id}",
                    is_global=False,
                    area_id=area.area_id,
                ))
        except Exception as exc:
            logger.debug("area NPC list error: %s", exc)

        # Global / event NPCs
        try:
            if hunt is None:
                hunt = await self._client.get_hunt_conf()
            for n in hunt.get("npcs", []):
                nid = str(n.get("npc_id", ""))
                if not nid or f"global:{nid}" in seen:
                    continue
                seen.add(f"global:{nid}")
                npcs.append(NpcInfo(
                    npc_id=nid,
                    title=n.get("title", ""),
                    url=n.get("url", ""),
                    time_left=int(n.get("time_left", 0) or 0),
                    is_global=True,
                ))
        except Exception as exc:
            logger.debug("hunt_conf NPC list error: %s", exc)

        return npcs

    async def resolve_current_npc(self) -> Optional[dict[str, Any]]:
        """
        Ask the server which NPC the story points to.

        Returns a dict with npc_id / url / quest_id when available.
        """
        try:
            resp = await self._client.common_action(CODE_NPC)
            raw = resp.raw or {}
            # Prefer structured npc|quests payload when present
            quests = raw.get("npc|quests") or {}
            if quests.get("npc_id"):
                return {
                    "npc_id": str(quests.get("npc_id")),
                    "global_npc": int(quests.get("global_npc", 1) or 1),
                    "quest_id": str(quests.get("quest_id") or ""),
                    "point_id": str(quests.get("point_id") or ""),
                    "url": "",
                }

            url = resp.redirect_url or ""
            if not url:
                return None
            decoded = urllib.parse.unquote(url)
            # ?url_close=npc.php%3F...
            m = re.search(r"npc\.php[^\"'\s]*", decoded)
            npc_url = m.group(0) if m else ""
            if not npc_url:
                return None
            qs = urllib.parse.parse_qs(urllib.parse.urlparse("http://x/" + npc_url).query)
            return {
                "npc_id": (qs.get("npc_id") or [""])[0],
                "global_npc": int((qs.get("global_npc") or ["1"])[0] or 1),
                "quest_id": (qs.get("quest_id") or [""])[0],
                "point_id": "",
                "url": npc_url,
                "f_id": (qs.get("f_id") or ["0"])[0],
                "link_id": (qs.get("link_id") or ["0"])[0],
                "area_id": (qs.get("area_id") or ["0"])[0],
            }
        except Exception as exc:
            logger.debug("resolve_current_npc error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # JSON dialogue walker
    # ------------------------------------------------------------------

    def _score_child(self, child: dict) -> int:
        title = str(child.get("title", "") or "").lower()
        score = 0
        if any(kw in title for kw in POSITIVE_KEYWORDS):
            score += 10
        if any(kw in title for kw in NEGATIVE_KEYWORDS):
            score -= 20
        if child.get("read_only"):
            score -= 5
        return score

    @staticmethod
    def _as_child_list(children: Any) -> list[dict]:
        """npc|point.child_list may be a list or a dict keyed by ord."""
        if isinstance(children, dict):
            return [v for v in children.values() if isinstance(v, dict)]
        if isinstance(children, list):
            return [c for c in children if isinstance(c, dict)]
        return []

    def _pick_child(self, children: list[dict]) -> Optional[dict]:
        kids = self._as_child_list(children)
        if not kids:
            return None
        ranked = sorted(kids, key=self._score_child, reverse=True)
        for ch in ranked:
            if self._score_child(ch) < 0:
                continue
            pid = str(ch.get("id", ""))
            if pid and pid in self._answered_points:
                continue
            return ch
        # Fallback: first unseen non-negative
        for ch in ranked:
            pid = str(ch.get("id", ""))
            if pid not in self._answered_points:
                return ch
        return None

    async def walk_npc_api(
        self,
        npc_id: str,
        *,
        global_npc: int = 0,
        link_id: str = "0",
        f_id: str = "0",
        area_id: str = "0",
        href: str = "",
        max_steps: int = 10,
    ) -> int:
        """
        Drive an NPC conversation through npc|quests → npc|answer.
        Returns the number of answers submitted.
        """
        steps = 0
        key = f"{global_npc}:{npc_id}:{link_id}"
        self._purge_expired_soft_bans()
        if key in self._exhausted_dialogues:
            return 0

        try:
            raw = await self._client.npc_quests(
                npc_id,
                global_npc=global_npc,
                link_id=link_id,
                f_id=f_id,
                area_id=area_id,
            )
        except Exception as exc:
            logger.debug("npc_quests(%s) error: %s", npc_id, exc)
            return 0

        quests_block = raw.get("npc|quests") or {}
        if int(quests_block.get("status", 0) or 0) not in (STATUS_OK, 0):
            err = quests_block.get("error") or quests_block.get("restriction_msg")
            if err:
                logger.info("NPC %s: %s", npc_id, err)
            self._exhausted_dialogues.add(key)
            return 0

        point_list = quests_block.get("point_list") or []
        else_text = str(quests_block.get("elsetext") or "")
        if else_text and not point_list:
            # Flavor-only NPC (e.g. arena greeter with no quests yet)
            clean = re.sub(r"<[^>]+>", "", else_text)
            logger.info("NPC %s: %s", npc_id, clean[:160])
            self._exhausted_dialogues.add(key)
            self.session.npcs_visited += 1
            return 0

        self.session.npcs_visited += 1
        npc_href = href or ""

        for _ in range(max_steps):
            pt = raw.get("npc|point") or {}
            point = pt.get("point") or {}
            children = self._as_child_list(pt.get("child_list"))
            title = point.get("title") or quests_block.get("quest_id") or ""
            desc = re.sub(r"<[^>]+>", " ", str(point.get("description") or ""))
            desc = re.sub(r"\s+", " ", desc).strip()

            if title:
                logger.info("NPC %s — %s", npc_id, str(title)[:100])
            if desc:
                logger.info("  %s", desc[:200])

            # Reading delay
            await asyncio.sleep(random.uniform(DELAY_DIALOGUE.min, DELAY_DIALOGUE.max))

            if pt.get("done") and not children:
                logger.debug("NPC %s point done — no more answers.", npc_id)
                break

            child = self._pick_child(children)
            if child is None:
                # Try next point from point_list that we haven't touched
                nxt = None
                for p in point_list:
                    pid = str(p.get("id", ""))
                    if pid and pid not in self._answered_points:
                        nxt = p
                        break
                if nxt is None:
                    break
                raw = await self._client.npc_point(
                    npc_id,
                    quest_id=nxt.get("quest_id") or quests_block.get("quest_id") or 0,
                    point_id=nxt.get("id"),
                    global_npc=global_npc,
                    link_id=link_id,
                    f_id=f_id,
                )
                continue

            child_id = str(child.get("id", ""))
            child_title = str(child.get("title") or child.get("message") or "")
            quest_id = (
                child.get("quest_id")
                or pt.get("quest_id")
                or quests_block.get("quest_id")
                or 0
            )
            logger.info("→ Выбираю: %s", child_title[:80])

            # Two child shapes exist:
            # 1) Message transition: {id, from_point_id, to_point_id, message}
            # 2) Sub-point:        {id, quest_id, parent_id, title, …}
            to_point = str(child.get("to_point_id") or "")
            from_point = str(child.get("from_point_id") or pt.get("point_id") or "")
            old_pid = str(pt.get("point_id") or "")
            ans: dict = {}
            ans_ok = False
            moved = False

            if to_point:
                # Message phrases: JSON npc|answer fails (status=2). Chain HTML
                # answers — award claim → next quest → accept objective → area.php.
                try:
                    html_steps, last_redir = await self._client.walk_npc_html(
                        npc_id,
                        global_npc=global_npc,
                        link_id=link_id,
                        f_id=f_id,
                        href=npc_href,
                        max_steps=max(4, max_steps - steps),
                    )
                except Exception as exc:
                    logger.debug("walk_npc_html(%s): %s", npc_id, exc)
                    html_steps, last_redir = 0, ""

                if html_steps:
                    logger.info(
                        "HTML-диалог NPC %s: %d шаг(ов), last=%s",
                        npc_id, html_steps, (last_redir or "")[:80],
                    )
                    self.session.dialogues_handled += html_steps
                    steps += html_steps
                    self.clear_hunt_gate()
                    if quest_id:
                        self.session.quests_accepted += 1
                    # Dialogue closed to area → objective is active (e.g. лава / раненые)
                    if last_redir and "npc.php" not in last_redir.lower():
                        kind = self.detect_world_objective_kind(
                            title, desc, child_title, str(child.get("message") or ""),
                        )
                        if kind == "heal_wounded":
                            self.set_world_objective(
                                kind=kind,
                                title=str(title or "Излечение ополченцев"),
                                npc_id=str(npc_id),
                                artikul_id="18209",
                                artifact_title="Деревенское снадобье",
                                quest_id=str(quest_id or ""),
                                area_id=str(area_id or ""),
                                ban_key=key,
                                ban_sec=1800.0,
                            )
                        elif kind:
                            self.set_world_objective(
                                kind=kind,
                                title=str(title or ""),
                                npc_id=str(npc_id),
                                quest_id=str(quest_id or ""),
                                area_id=str(area_id or ""),
                                ban_key=key,
                                ban_sec=900.0,
                            )
                        else:
                            self._exhausted_dialogues.add(key)
                            self._soft_ban_until[key] = time.time() + 180.0
                        logger.info(
                            "Квест принят / диалог закрыт → %s (цель в мире%s).",
                            last_redir.split("?")[0],
                            f"={kind}" if kind else "",
                        )
                        return steps
                    # Still on NPC — refresh JSON view and continue
                    raw = await self._client.npc_quests(
                        npc_id,
                        global_npc=global_npc,
                        link_id=link_id,
                        f_id=f_id,
                        area_id=area_id,
                    )
                    quests_block = raw.get("npc|quests") or {}
                    point_list = quests_block.get("point_list") or []
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    continue

                # Fallback: JSON answer variants (works for some points)
                raw = await self._client.npc_point(
                    npc_id,
                    quest_id=quest_id,
                    point_id=to_point,
                    global_npc=global_npc,
                    link_id=link_id,
                    f_id=f_id,
                    subpoint_id=to_point,
                )
                raw_ans = await self._client.npc_answer(
                    npc_id,
                    quest_id=quest_id,
                    point_id=to_point,
                    global_npc=global_npc,
                    link_id=link_id,
                    f_id=f_id,
                    subpoint_id=to_point,
                )
                if int((raw_ans.get("npc|answer") or {}).get("status", 0) or 0) != STATUS_OK:
                    raw_ans = await self._client.npc_answer(
                        npc_id,
                        quest_id=quest_id,
                        point_id=from_point or pt.get("point_id") or child_id,
                        global_npc=global_npc,
                        link_id=link_id,
                        f_id=f_id,
                        subpoint_id=child_id,
                    )
                ans = raw_ans.get("npc|answer") or {}
                ans_ok = int(ans.get("status", 0) or 0) == STATUS_OK
                if ans_ok and (raw_ans.get("npc|point") or {}).get("point"):
                    raw = raw_ans
                elif ans_ok:
                    raw = raw_ans
                else:
                    # JSON failed too — maybe a real kill gate
                    point_obj = pt.get("point") if isinstance(pt.get("point"), dict) else {}
                    mob_name = self._infer_hunt_mob_name(
                        child_title,
                        str(child.get("message") or ""),
                        str(point_obj.get("target_name") or ""),
                        str(point_obj.get("title") or ""),
                        str(point_obj.get("description") or ""),
                    )
                    if not mob_name:
                        logger.info(
                            "type=2 msg %s → %s не принят (HTML+JSON) — "
                            "'%s'; пробую другой ответ.",
                            child_id, to_point, child_title[:60],
                        )
                        self._answered_points.add(child_id)
                        self.clear_hunt_gate()
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        continue
                    logger.info(
                        "Ответ type=2 отклонён (msg %s → %s) — ищу моба '%s'…",
                        child_id, to_point, mob_name,
                    )
                    started = await self._attack_hunt_for_quest(mob_name)
                    if started:
                        self._pending_type2 = {
                            "npc_id": str(npc_id),
                            "quest_id": str(quest_id),
                            "from_point": str(from_point or pt.get("point_id") or ""),
                            "to_point": str(to_point),
                            "child_id": child_id,
                            "global_npc": int(global_npc or 0),
                            "link_id": str(link_id or "0"),
                            "f_id": str(f_id or "0"),
                            "href": npc_href,
                            "mob_name": mob_name,
                        }
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                        return max(1, steps)
                    self._exhausted_dialogues.add(key)
                    self._soft_ban_until[key] = time.time() + 45.0
                    break
            else:
                # Prefer answering the CURRENT point for subdialogs; answering the
                # child id itself often yields "Этап не может иметь корня!".
                current_pid = str(pt.get("point_id") or "")
                raw = await self._client.npc_answer(
                    npc_id,
                    quest_id=quest_id,
                    point_id=current_pid or child_id,
                    global_npc=global_npc,
                    link_id=link_id,
                    f_id=f_id,
                    subpoint_id=child_id if current_pid else child_id,
                )
                ans = raw.get("npc|answer") or {}
                if int(ans.get("status", 0) or 0) != STATUS_OK and current_pid:
                    # Fall back to treating the child as a navigable point
                    raw = await self._client.npc_point(
                        npc_id,
                        quest_id=quest_id,
                        point_id=child_id,
                        global_npc=global_npc,
                        link_id=link_id,
                        f_id=f_id,
                    )

            ans = raw.get("npc|answer") or {}
            if ans and int(ans.get("status", 0) or 0) not in (STATUS_OK, 0) and ans.get("error"):
                logger.info("Ответ отклонён: %s", ans.get("error"))
                self._answered_points.add(child_id)
                # Don't hard-stop — destination point may still have opened
                if not (raw.get("npc|point") or {}).get("point"):
                    break

            new_pt = raw.get("npc|point") or {}
            new_pid = str(new_pt.get("point_id") or "")
            moved = bool(new_pid) and new_pid != old_pid and not new_pt.get("done")
            ans_ok = int(ans.get("status", 0) or 0) == STATUS_OK

            if not moved and not ans_ok:
                logger.info(
                    "NPC %s: диалог не сдвинулся с точки %s.",
                    npc_id, old_pid or "?",
                )
                self._answered_points.add(child_id)
                self._exhausted_dialogues.add(key)
                break

            self._answered_points.add(child_id)
            self.session.dialogues_handled += 1
            steps += 1
            if quest_id and ans_ok:
                self.session.quests_accepted += 1
            # Successful dialogue step clears hunt gate (kill already turned in)
            if ans_ok or moved:
                self.clear_hunt_gate()

            awards = (raw.get("npc|point") or {}).get("award_list") or []
            for a in awards[:5]:
                logger.info("  Награда: %s", a)

            await asyncio.sleep(random.uniform(0.8, 2.0))

            # Refresh quests if the answer didn't return a new point
            if not new_pt.get("point") or new_pid == old_pid:
                raw = await self._client.npc_quests(
                    npc_id,
                    global_npc=global_npc,
                    link_id=link_id,
                    f_id=f_id,
                    area_id=area_id,
                )
                quests_block = raw.get("npc|quests") or {}
                point_list = quests_block.get("point_list") or []
                # If still on same point with same children — exhausted
                again = raw.get("npc|point") or {}
                if str(again.get("point_id") or "") == old_pid:
                    same_children = {
                        str(x.get("id"))
                        for x in self._as_child_list(again.get("child_list"))
                    }
                    if child_id in same_children or not self._as_child_list(
                        again.get("child_list")
                    ):
                        logger.info(
                            "NPC %s: диалог не сдвинулся (возможно, нужно выполнить цель квеста).",
                            npc_id,
                        )
                        self._exhausted_dialogues.add(key)
                        break

        if steps == 0:
            self._exhausted_dialogues.add(key)
        return steps

    # ------------------------------------------------------------------
    # Legacy HTML helpers (kept as fallback)
    # ------------------------------------------------------------------

    _GAME_PAGES = (
        "npc.php", "area.php", "entry_point.php", "quest.php",
        "user.php", "fight.php", "shop.php", "store.php",
    )
    _BLOCKED = (
        "/info/", "/news/", "/forum/", "/library/", "javascript:",
        "logout", "support.", "vkplay", "mailto:", ".jpg", ".png", ".gif",
    )

    def _is_game_url(self, href: str) -> bool:
        if not href or href.startswith("#"):
            return False
        low = href.lower()
        if low.startswith(("http://", "https://", "//")):
            return False
        if any(b in low for b in self._BLOCKED):
            return False
        return any(p in low for p in self._GAME_PAGES)

    async def read_quests(self) -> list[Quest]:
        quests: list[Quest] = []
        try:
            resp = await self._client._get("/user.php", params={"mode": "quest", "group": "4"})
            html = resp.text
            for m in re.finditer(
                r'quest_id["\']?\s*[=:]\s*["\']?(\d+)["\']?(.{0,400})', html, re.DOTALL
            ):
                qid, window = m.group(1), m.group(2)
                title_m = re.search(r">([А-ЯЁ][^<>]{4,80})<", window)
                title = title_m.group(1).strip() if title_m else f"Квест #{qid}"
                status = QuestStatus.ACTIVE
                wl = window.lower()
                if any(w in wl for w in ("выполнен", "сдать", "готов к сдаче")):
                    status = QuestStatus.COMPLETABLE
                elif "завершён" in wl or "завершен" in wl:
                    status = QuestStatus.COMPLETED
                if not any(q.quest_id == qid for q in quests):
                    quests.append(Quest(quest_id=qid, title=title, status=status))
        except Exception as exc:
            logger.debug("read_quests error: %s", exc)
        return quests

    async def complete_ready_quests(self) -> int:
        completed = 0
        for q in await self.read_quests():
            if q.status != QuestStatus.COMPLETABLE:
                continue
            try:
                resp = await self._client.common_action(
                    CODE_QUEST_COMPLETE, {"quest_id": q.quest_id}
                )
                err = str(resp.redirect_error or "")
                if not err or err.lower() in ("false", "none"):
                    completed += 1
                    self.session.quests_completed += 1
                    logger.info("Quest completed: '%s'.", q.title)
                await asyncio.sleep(random.uniform(1.0, 2.5))
            except Exception as exc:
                logger.debug("complete quest '%s' error: %s", q.title, exc)
        return completed

    # ------------------------------------------------------------------
    # High-level tick
    # ------------------------------------------------------------------

    async def quest_tick(self) -> int:
        actions = 0
        self._reset_exhausted_if_quests_changed(await self.read_quests())
        actions += await self.complete_ready_quests()

        # 1. Story NPC pointer
        story = await self.resolve_current_npc()
        if story and story.get("npc_id"):
            key = f"{story.get('global_npc', 1)}:{story['npc_id']}:{story.get('link_id', '0')}"
            if key not in self._exhausted_dialogues:
                logger.info(
                    "Story NPC #%s (global=%s)",
                    story["npc_id"], story.get("global_npc", 1),
                )
                steps = await self.walk_npc_api(
                story["npc_id"],
                global_npc=int(story.get("global_npc", 1) or 1),
                link_id=str(story.get("link_id") or "0"),
                f_id=str(story.get("f_id") or "0"),
                area_id=str(story.get("area_id") or "0"),
                href=str(story.get("url") or ""),
            )
            actions += steps

        # 2. Local area NPCs first (quest givers like Вождь Торгор), then events
        for npc in await self.list_available_npcs():
            visit_key = f"{int(npc.is_global)}:{npc.npc_id}:{npc.link_id}"
            if visit_key in self._exhausted_dialogues or visit_key in self._visited_npcs:
                continue
            if npc.time_left and npc.time_left < 60:
                continue
            # Skip pure arena greeters with no quests until local story is done —
            # still try once so we log their text.
            logger.info(
                "NPC: %s (id=%s, global=%s)",
                npc.title, npc.npc_id, npc.is_global,
            )
            self._visited_npcs.add(visit_key)
            steps = await self.walk_npc_api(
                npc.npc_id,
                global_npc=1 if npc.is_global else 0,
                link_id=npc.link_id or "0",
                f_id=npc.f_id or "0",
                area_id=npc.area_id or "0",
                href=npc.url or "",
            )
            actions += steps
            if steps:
                break  # one productive NPC per tick
            if not npc.is_global:
                break

        return actions
