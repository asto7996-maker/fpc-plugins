"""
Inbound message filters for the draft / auto-reply pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass
class MessageFilter:
    """
    Trigger / stop-word / length / bot-protection checks.
    """

    trigger_words: Sequence[str] = field(default_factory=tuple)
    stop_words: Sequence[str] = field(default_factory=tuple)
    min_length: int = 1
    max_length: int = 2000
    ignore_bots: bool = True

    def normalize(self, text: str) -> str:
        return (text or "").strip()

    def matched_trigger(self, text: str) -> Optional[str]:
        body = self.normalize(text).lower()
        if not body:
            return None
        for word in self.trigger_words:
            w = word.strip().lower()
            if not w:
                continue
            if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", body, flags=re.IGNORECASE):
                return word
        return None

    def has_stop_word(self, text: str) -> bool:
        body = self.normalize(text).lower()
        for word in self.stop_words:
            w = word.strip().lower()
            if w and w in body:
                return True
        return False

    def length_ok(self, text: str) -> bool:
        n = len(self.normalize(text))
        return self.min_length <= n <= self.max_length

    def should_skip_sender(self, *, is_bot: bool = False, is_self: bool = False) -> bool:
        if is_self:
            return True
        if self.ignore_bots and is_bot:
            return True
        return False

    def accept(
        self,
        text: str,
        *,
        is_bot: bool = False,
        is_self: bool = False,
    ) -> tuple[bool, str]:
        """
        Returns (accepted, reason).
        """
        if self.should_skip_sender(is_bot=is_bot, is_self=is_self):
            return False, "sender_skipped"
        if not self.length_ok(text):
            return False, "length"
        if self.has_stop_word(text):
            return False, "stop_word"
        if self.trigger_words and self.matched_trigger(text) is None:
            return False, "no_trigger"
        return True, "ok"
