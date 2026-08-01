"""
Quest tracker — parses active quests and handles NPC dialogue trees.

Strategy
--------
* Scan the quest list for non-completed quests.
* If the current quest requires talking to an NPC, detect the dialogue box
  and pick the first available option (or the option matching keywords).
* If the quest is completable (turn-in), click the completion button.
* Log quest name, status, and choice made at every step.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from playwright.async_api import Page

from dwar_bot.config import DELAY_DIALOGUE, SELECTORS
from dwar_bot.core.anti_bot import (
    human_click,
    sleep_random,
    wait_for_selector_safe,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class QuestStatus(Enum):
    ACTIVE = auto()
    COMPLETABLE = auto()
    COMPLETED = auto()
    UNKNOWN = auto()


@dataclass
class Quest:
    title: str = ""
    status: QuestStatus = QuestStatus.UNKNOWN
    element_index: int = 0


# ---------------------------------------------------------------------------
# QuestTracker
# ---------------------------------------------------------------------------

class QuestTracker:
    """Reads, tracks, and advances quests on the game page."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._current_quest: Optional[Quest] = None
        self._dialogues_handled: int = 0

    # ------------------------------------------------------------------
    # Quest list
    # ------------------------------------------------------------------

    async def read_quests(self) -> list[Quest]:
        """Parse the quest panel and return all visible quests."""
        quests: list[Quest] = []
        try:
            items = await self._page.query_selector_all(SELECTORS.quest_item)
            for idx, item in enumerate(items):
                try:
                    title_el = await item.query_selector(SELECTORS.quest_title)
                    status_el = await item.query_selector(SELECTORS.quest_status)
                    title = (await title_el.inner_text()).strip() if title_el else f"Quest #{idx}"
                    status_raw = (await status_el.inner_text()).strip().lower() if status_el else ""
                    status = self._parse_status(status_raw)
                    quests.append(Quest(title=title, status=status, element_index=idx))
                except Exception as e:
                    logger.debug("Quest slot %d parse error: %s", idx, e)
        except Exception as exc:
            logger.warning("read_quests failed: %s", exc)
        return quests

    def _parse_status(self, raw: str) -> QuestStatus:
        if any(kw in raw for kw in ("выполн", "complete", "done", "сдать", "turn")):
            return QuestStatus.COMPLETABLE
        if any(kw in raw for kw in ("завершён", "finished", "closed")):
            return QuestStatus.COMPLETED
        if any(kw in raw for kw in ("актив", "active", "в процессе", "progress")):
            return QuestStatus.ACTIVE
        return QuestStatus.UNKNOWN

    async def try_complete_quests(self) -> int:
        """
        Click 'complete' on all completable quests.

        Returns the number of quests turned in.
        """
        completed = 0
        quests = await self.read_quests()
        for q in quests:
            if q.status == QuestStatus.COMPLETABLE:
                success = await self._turn_in_quest(q)
                if success:
                    completed += 1
        return completed

    async def _turn_in_quest(self, quest: Quest) -> bool:
        try:
            btn = await wait_for_selector_safe(
                self._page, SELECTORS.npc_complete_quest, timeout_ms=5_000
            )
            if btn is None:
                logger.debug("No complete button for quest '%s'.", quest.title)
                return False
            await sleep_random(DELAY_DIALOGUE.min / 2, DELAY_DIALOGUE.max / 2)
            await btn.click()
            logger.info("Quest turned in: '%s'.", quest.title)
            await sleep_random(1.0, 2.5)
            return True
        except Exception as exc:
            logger.warning("_turn_in_quest('%s') failed: %s", quest.title, exc)
            return False

    # ------------------------------------------------------------------
    # NPC dialogue
    # ------------------------------------------------------------------

    async def is_dialogue_open(self) -> bool:
        """Return True if an NPC dialogue box is currently visible."""
        el = await wait_for_selector_safe(
            self._page, SELECTORS.npc_dialogue_box, timeout_ms=1_500
        )
        return el is not None

    async def handle_dialogue(
        self,
        preferred_keywords: Optional[list[str]] = None,
    ) -> bool:
        """
        Process one NPC dialogue screen.

        Reads the dialogue text, logs it, then clicks an answer button.
        If *preferred_keywords* is set, the first choice whose text contains
        any keyword is selected; otherwise a random choice is made.

        Returns True if a choice was made, False if the dialogue was not open.
        """
        if not await self.is_dialogue_open():
            return False

        try:
            text_el = await self._page.query_selector(SELECTORS.npc_dialogue_text)
            dialogue_text = (await text_el.inner_text()).strip() if text_el else "(no text)"
            logger.info("NPC says: %s", dialogue_text[:120])

            # Simulate reading time
            read_time = min(len(dialogue_text) * 0.035, DELAY_DIALOGUE.max)
            await sleep_random(max(DELAY_DIALOGUE.min, read_time * 0.7), max(DELAY_DIALOGUE.min + 0.5, read_time))

            choices = await self._page.query_selector_all(SELECTORS.npc_choice_btns)
            if not choices:
                # No choice buttons — check for an accept/complete button
                for fallback in (SELECTORS.npc_accept_quest, SELECTORS.npc_complete_quest):
                    btn = await wait_for_selector_safe(self._page, fallback, timeout_ms=1_500)
                    if btn:
                        await btn.click()
                        self._dialogues_handled += 1
                        return True
                return False

            chosen = await self._select_choice(choices, preferred_keywords)
            if chosen is None:
                return False

            chosen_text = (await chosen.inner_text()).strip()
            logger.info("Choosing dialogue option: '%s'.", chosen_text[:80])
            await chosen.click()
            self._dialogues_handled += 1
            await sleep_random(0.5, 1.5)
            return True

        except Exception as exc:
            logger.warning("handle_dialogue failed: %s", exc)
            return False

    async def _select_choice(self, choices, preferred_keywords: Optional[list[str]]):
        """Pick a choice element, preferring keyword matches."""
        if preferred_keywords:
            for choice in choices:
                try:
                    text = (await choice.inner_text()).lower()
                    if any(kw.lower() in text for kw in preferred_keywords):
                        return choice
                except Exception:
                    pass
        # Fallback: pick a random enabled choice
        enabled = []
        for choice in choices:
            try:
                disabled = await choice.get_attribute("disabled")
                if disabled is None:
                    enabled.append(choice)
            except Exception:
                pass
        return random.choice(enabled) if enabled else (choices[0] if choices else None)

    async def drain_all_dialogues(
        self,
        max_rounds: int = 20,
        preferred_keywords: Optional[list[str]] = None,
    ) -> int:
        """
        Repeatedly handle dialogue until no more dialogue boxes appear.

        Returns total number of choices made.
        """
        handled = 0
        for _ in range(max_rounds):
            made = await self.handle_dialogue(preferred_keywords)
            if made:
                handled += 1
            else:
                break
        return handled
