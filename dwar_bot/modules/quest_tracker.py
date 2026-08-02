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

    def _reset_exhausted_if_quests_changed(self, quests: list[Quest]) -> None:
        sig = "|".join(sorted(f"{q.quest_id}:{q.status.name}" for q in quests))
        if sig != self._last_quest_signature:
            if self._exhausted_dialogues:
                logger.debug("Quest state changed — re-enabling NPC dialogues.")
            self._exhausted_dialogues.clear()
            self._visited_npcs.clear()
            self._last_quest_signature = sig

    def exhausted_npc_ids(self) -> set[str]:
        """NPC ids whose dialogue was marked exhausted this session."""
        out: set[str] = set()
        for key in self._exhausted_dialogues:
            # key format: "{global_npc}:{npc_id}:{link_id}"
            parts = str(key).split(":")
            if len(parts) >= 2 and parts[1]:
                out.add(parts[1])
        return out

    def mark_npc_exhausted(
        self,
        npc_id: str,
        *,
        global_npc: int = 0,
        link_id: str = "0",
    ) -> None:
        self._exhausted_dialogues.add(f"{global_npc}:{npc_id}:{link_id}")

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

    def _pick_child(self, children: list[dict]) -> Optional[dict]:
        if not children:
            return None
        ranked = sorted(children, key=self._score_child, reverse=True)
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
        max_steps: int = 10,
    ) -> int:
        """
        Drive an NPC conversation through npc|quests → npc|answer.
        Returns the number of answers submitted.
        """
        steps = 0
        key = f"{global_npc}:{npc_id}:{link_id}"
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

        for _ in range(max_steps):
            pt = raw.get("npc|point") or {}
            point = pt.get("point") or {}
            children = pt.get("child_list") or []
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

            if to_point:
                # Advance by opening the destination point, then acknowledging it
                raw = await self._client.npc_point(
                    npc_id,
                    quest_id=quest_id,
                    point_id=to_point,
                    global_npc=global_npc,
                    link_id=link_id,
                    f_id=f_id,
                    subpoint_id=to_point,
                )
                nxt_pt = raw.get("npc|point") or {}
                # Also try answer on the destination / with message id
                raw_ans = await self._client.npc_answer(
                    npc_id,
                    quest_id=quest_id,
                    point_id=to_point,
                    global_npc=global_npc,
                    link_id=link_id,
                    f_id=f_id,
                    subpoint_id=to_point,
                )
                # Fallback: answer current point (works for some type=0 dialogs)
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
                raw = raw_ans if (raw_ans.get("npc|point") or {}).get("point") else raw
                if not (raw.get("npc|point") or {}).get("point") and (nxt_pt.get("point") or {}).get("title"):
                    raw = {"npc|point": nxt_pt, "npc|answer": {"status": STATUS_OK}}
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

            self._answered_points.add(child_id)
            self.session.dialogues_handled += 1
            steps += 1
            if quest_id:
                self.session.quests_accepted += 1

            awards = (raw.get("npc|point") or {}).get("award_list") or []
            for a in awards[:5]:
                logger.info("  Награда: %s", a)

            await asyncio.sleep(random.uniform(0.8, 2.0))

            # Refresh quests if the answer didn't return a new point
            new_pt = raw.get("npc|point") or {}
            new_pid = str(new_pt.get("point_id") or "")
            old_pid = str(pt.get("point_id") or "")
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
                        str(x.get("id")) for x in (again.get("child_list") or [])
                    }
                    if child_id in same_children or not (again.get("child_list") or []):
                        logger.info(
                            "NPC %s: диалог не сдвинулся (возможно, нужно выполнить цель квеста).",
                            npc_id,
                        )
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
            )
            actions += steps
            if steps:
                break  # one productive NPC per tick
            if not npc.is_global:
                break

        return actions
