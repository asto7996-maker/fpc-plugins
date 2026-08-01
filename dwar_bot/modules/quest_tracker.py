"""
Quest tracker — story progression, NPC dialogue and quest branch selection
via the dwar.ru HTTP API.

Data sources
------------
* ``hunt_conf.php``            — XML list of currently available global NPCs
* ``area.php``                 — local NPC id + area navigation
* ``npc.php?npc_id=N&…``       — NPC dialogue page (HTML)
* ``user.php`` (Квесты tab)    — active quest list
* ``entry_point.php`` common|action?code=NPC — resolves the current NPC target
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from dwar_bot.config import DELAY_DIALOGUE
from dwar_bot.core.game_client import DwarGameClient, STATUS_OK

logger = logging.getLogger(__name__)

CODE_NPC = "NPC"
CODE_QUEST_ACCEPT = "QUEST_ACCEPT"
CODE_QUEST_COMPLETE = "QUEST_COMPLETE"

# Words that mark a dialogue option we want to click
POSITIVE_KEYWORDS = [
    "принять", "принимаю", "согласен", "согласна", "да", "конечно",
    "продолжить", "далее", "дальше", "получить", "взять", "хорошо",
    "готов", "готова", "начать", "выполнить", "сдать", "завершить",
    "награда", "забрать",
]
NEGATIVE_KEYWORDS = [
    "отказ", "нет", "позже", "уйти", "выйти", "закрыть", "отменить",
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


@dataclass
class DialogueOption:
    text: str = ""
    url: str = ""
    code: str = ""
    quest_id: str = ""

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


# ---------------------------------------------------------------------------
# QuestTracker
# ---------------------------------------------------------------------------

class QuestTracker:
    """Handles NPC interaction and quest progression over HTTP."""

    def __init__(self, client: DwarGameClient) -> None:
        self._client = client
        self.session = QuestStats()
        self._visited_npcs: set[str] = set()

    # ------------------------------------------------------------------
    # NPC discovery
    # ------------------------------------------------------------------

    async def list_available_npcs(self) -> list[NpcInfo]:
        """Return every NPC currently reachable (global events + local area)."""
        npcs: list[NpcInfo] = []

        # Global / event NPCs from hunt_conf.php
        try:
            hunt = await self._client.get_hunt_conf()
            for n in hunt.get("npcs", []):
                npcs.append(NpcInfo(
                    npc_id=str(n.get("npc_id", "")),
                    title=n.get("title", ""),
                    url=n.get("url", ""),
                    time_left=int(n.get("time_left", 0)),
                    is_global=True,
                ))
        except Exception as exc:
            logger.debug("hunt_conf NPC list error: %s", exc)

        # Local NPC from area.php
        try:
            area = await self._client.get_area_info()
            if area.npc_id:
                npcs.append(NpcInfo(
                    npc_id=str(area.npc_id),
                    title=f"Локальный NPC #{area.npc_id}",
                    url=f"npc.php?area_id={area.area_id}&npc_id={area.npc_id}",
                    is_global=False,
                ))
        except Exception as exc:
            logger.debug("area NPC error: %s", exc)

        return npcs

    async def resolve_current_npc(self) -> Optional[str]:
        """
        Ask the server which NPC the character should talk to next
        (``common|action?code=NPC`` returns a redirect URL containing npc_id).
        """
        try:
            resp = await self._client.common_action(CODE_NPC)
            url = resp.redirect_url or ""
            if not url:
                return None
            decoded = urllib.parse.unquote(url)
            m = re.search(r"npc\.php[^\"'\s]*", decoded)
            return m.group(0) if m else None
        except Exception as exc:
            logger.debug("resolve_current_npc error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Dialogue
    # ------------------------------------------------------------------

    async def open_npc(self, npc_url: str) -> str:
        """Fetch an NPC dialogue page. Returns raw HTML."""
        path = npc_url if npc_url.startswith("/") else f"/{npc_url}"
        try:
            resp = await self._client._get(path)
            self.session.npcs_visited += 1
            return resp.text
        except Exception as exc:
            logger.debug("open_npc('%s') error: %s", npc_url, exc)
            return ""

    def parse_dialogue(self, html: str) -> tuple[str, list[DialogueOption]]:
        """
        Extract the NPC's speech text and the list of clickable answer options.

        Returns (dialogue_text, options).
        """
        # Strip scripts/styles before text extraction
        clean = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<style.*?</style>", " ", clean, flags=re.DOTALL | re.IGNORECASE)

        # NPC speech — usually the longest text block on the page
        text_blocks = re.findall(r">([^<>]{40,800})<", clean)
        dialogue_text = ""
        if text_blocks:
            dialogue_text = max(
                (b.strip() for b in text_blocks),
                key=len,
                default="",
            )
            dialogue_text = re.sub(r"\s+", " ", dialogue_text)

        # Options: links and buttons inside the NPC page
        options: list[DialogueOption] = []
        for m in re.finditer(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', clean, re.DOTALL | re.IGNORECASE
        ):
            href, label = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not label or len(label) > 200:
                continue
            if any(skip in href.lower() for skip in ("javascript:", "#", "logout", "forum")):
                continue
            code_m = re.search(r"code=([A-Z_0-9]+)", urllib.parse.unquote(href))
            quest_m = re.search(r"quest_id=(\d+)", urllib.parse.unquote(href))
            options.append(DialogueOption(
                text=label,
                url=href,
                code=code_m.group(1) if code_m else "",
                quest_id=quest_m.group(1) if quest_m else "",
            ))

        # Buttons with onclick handlers
        for m in re.finditer(
            r'<(?:input|button)[^>]+(?:value|title)=["\']([^"\']{2,80})["\'][^>]*onclick=["\']([^"\']+)["\']',
            clean, re.IGNORECASE
        ):
            label, onclick = m.group(1).strip(), m.group(2)
            url_m = re.search(r"['\"]([^'\"]*\.php[^'\"]*)['\"]", onclick)
            options.append(DialogueOption(
                text=label,
                url=url_m.group(1) if url_m else "",
                code="",
            ))

        return dialogue_text, options

    def choose_option(self, options: list[DialogueOption]) -> Optional[DialogueOption]:
        """
        Pick the best dialogue option:
        prefer positive/progress answers, never pick a refusal.
        """
        if not options:
            return None

        positives = [o for o in options if o.is_positive and not o.is_negative]
        if positives:
            return positives[0]

        neutral = [o for o in options if not o.is_negative]
        if neutral:
            return neutral[0]

        return None

    async def handle_dialogue(self, npc_url: str, max_steps: int = 12) -> int:
        """
        Walk an NPC dialogue tree, always choosing the progress-forward option.

        Returns the number of dialogue steps completed.
        """
        steps = 0
        current_url = npc_url

        for _ in range(max_steps):
            html = await self.open_npc(current_url)
            if not html or "404" in html[:200]:
                break

            text, options = self.parse_dialogue(html)
            if text:
                logger.info("NPC: %s", text[:150])

            # Simulate reading time proportional to text length
            read_time = min(len(text) * 0.02, DELAY_DIALOGUE.max)
            await asyncio.sleep(random.uniform(DELAY_DIALOGUE.min, max(DELAY_DIALOGUE.min + 0.5, read_time)))

            choice = self.choose_option(options)
            if choice is None:
                logger.debug("No further dialogue option — ending conversation.")
                break

            logger.info("→ Выбираю: %s", choice.text[:80])
            self.session.dialogues_handled += 1
            steps += 1

            if choice.quest_id:
                self.session.quests_accepted += 1
                logger.info("Quest #%s accepted.", choice.quest_id)

            if not choice.url:
                break
            current_url = choice.url

            await asyncio.sleep(random.uniform(0.8, 2.0))

        return steps

    # ------------------------------------------------------------------
    # Quest list
    # ------------------------------------------------------------------

    async def read_quests(self) -> list[Quest]:
        """Parse the active quest list from the Квесты tab of user.php."""
        quests: list[Quest] = []
        try:
            resp = await self._client._get("/user.php", params={"mode": "quest", "group": "4"})
            html = resp.text

            # Quest blocks usually carry a quest_id attribute
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
        """Turn in every quest that is ready to be completed."""
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
                    if resp.bonus_text:
                        for t in resp.bonus_text:
                            logger.info("  Награда: %s", t)
                await asyncio.sleep(random.uniform(1.0, 2.5))
            except Exception as exc:
                logger.debug("complete quest '%s' error: %s", q.title, exc)
        return completed

    # ------------------------------------------------------------------
    # High-level tick
    # ------------------------------------------------------------------

    async def quest_tick(self) -> int:
        """
        One quest-progression cycle:
          1. Complete any finished quests
          2. Resolve the next NPC the story points to and talk to them
          3. Visit any time-limited event NPCs

        Returns the number of meaningful actions performed.
        """
        actions = 0

        # 1. Turn in ready quests
        actions += await self.complete_ready_quests()

        # 2. Story NPC
        npc_url = await self.resolve_current_npc()
        if npc_url:
            logger.info("Story NPC found: %s", npc_url[:90])
            actions += await self.handle_dialogue(npc_url)

        # 3. Event NPCs (arena entrances, seasonal events…)
        for npc in await self.list_available_npcs():
            if not npc.url or npc.npc_id in self._visited_npcs:
                continue
            if npc.time_left and npc.time_left < 60:
                continue  # about to expire
            logger.info(
                "Event NPC: %s (id=%s, %ds left)",
                npc.title, npc.npc_id, npc.time_left,
            )
            self._visited_npcs.add(npc.npc_id)
            actions += await self.handle_dialogue(npc.url, max_steps=6)
            break  # one NPC per tick to stay human-like

        return actions
