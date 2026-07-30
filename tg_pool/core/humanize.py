"""
Human-like text stylistics + behavioral emulation (read → type → pause → send).

Hard rules
----------
* Messages start with a lowercase letter
* Zero-emoji policy
* No ad-speak / stacked exclamation marks
* Typing duration scales with reply length; Telegram typing is refreshed <5s
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Broad emoji / pictograph ranges + VS16 / ZWJ sequences leftovers
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002700-\U000027BF"  # dingbats
    "\U00002600-\U000026FF"  # misc symbols
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"  # ZWJ
    "\U0000203C\U00002049"  # ‼ ⁉
    "\U00002194-\U00002199"
    "\U00002B50\U00002B55"  # ⭐ ⭕
    "\U00003030\U0000303D"
    "\U00003297\U00003299"
    "]+",
    flags=re.UNICODE,
)

_MULTI_BANG = re.compile(r"!{2,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MULTI_NL = re.compile(r"\n{3,}")
_AD_CLICHES = re.compile(
    r"(?i)\b("
    r"лучший\s+vpn|лучший\s+впн|переходи\s+по\s+ссылке|"
    r"только\s+сегодня|успей\s+купить|жми\s+сюда|"
    r"гарантированн\w*|акция\s+дня"
    r")\b",
)


@dataclass(frozen=True)
class BehaviorPlan:
    """Timing plan for one outbound human-like reply."""

    read_sec: float
    typing_sec: float
    pause_sec: float
    text: str

    @property
    def total_sec(self) -> float:
        return self.read_sec + self.typing_sec + self.pause_sec


def _strip_emoji_chars(text: str) -> str:
    """Remove emoji / symbol glyphs (Unicode So/Sk + known emoji ranges)."""
    cleaned = _EMOJI_RE.sub("", text)
    out: list[str] = []
    for ch in cleaned:
        cat = unicodedata.category(ch)
        # So = Symbol, other; Sk = Symbol, modifier — drop (emoji, dingbats)
        if cat in {"So", "Sk"}:
            continue
        # Soft hyphen / weird formatters
        if cat == "Cf" and ch not in {"\u200c"}:  # keep ZWNJ if any; drop others
            if ord(ch) in {0x200B, 0x200C, 0x200D, 0xFEFF}:
                continue
        out.append(ch)
    return "".join(out)


def humanize_text_sync(raw_text: str) -> str:
    """Synchronous humanize — used by tests and hot paths."""
    if raw_text is None:
        return ""
    text = str(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_emoji_chars(text)
    text = _AD_CLICHES.sub("", text)
    text = _MULTI_BANG.sub("!", text)
    # Collapse ALL CAPS words longer than 2 chars (keep @usernames / URLs-ish)
    parts: list[str] = []
    for token in re.split(r"(\s+)", text):
        if (
            token.isalpha()
            and len(token) > 2
            and token.isupper()
            and not token.startswith("@")
        ):
            parts.append(token.lower())
        else:
            parts.append(token)
    text = "".join(parts)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    text = text.strip(" \t")
    # Trim leading/trailing empty lines but keep inner paragraph breaks light
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines).strip()
    if not text:
        return ""
    # Lowercase start — skip leading punctuation/quotes
    chars = list(text)
    for i, ch in enumerate(chars):
        if ch.isalpha():
            chars[i] = ch.lower()
            break
    text = "".join(chars)
    # Final space tidy
    text = _MULTI_SPACE.sub(" ", text)
    return text.strip()


async def humanize_text(raw_text: str) -> str:
    """
    Post-process generated copy into native chat style.

    * first alphabetic character → lowercase
    * strip all emoji / symbol pictographs
    * collapse double spaces / excess newlines
    * soften stacked ``!!!`` and ad clichés
    """
    return humanize_text_sync(raw_text)


def typing_duration_sec(
    text: str,
    *,
    cps_min: float = 0.15,
    cps_max: float = 0.35,
    rng: Optional[random.Random] = None,
) -> float:
    """
    Smartphone typing estimate: ``len(text) * random(0.15, 0.35)`` seconds.
    """
    rng = rng or random.Random()
    n = max(1, len(text or ""))
    lo, hi = (cps_min, cps_max) if cps_min <= cps_max else (cps_max, cps_min)
    return float(n) * rng.uniform(lo, hi)


class BehavioralEmulationEngine:
    """
    Orchestrates read latency → typing action (refreshed) → pre-send pause.
    """

    def __init__(
        self,
        *,
        read_min: float = 15.0,
        read_max: float = 45.0,
        pause_min: float = 1.5,
        pause_max: float = 3.5,
        typing_refresh_sec: float = 4.5,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.read_min = read_min
        self.read_max = read_max
        self.pause_min = pause_min
        self.pause_max = pause_max
        self.typing_refresh_sec = typing_refresh_sec
        self.rng = rng or random.Random()

    def plan(self, raw_text: str) -> BehaviorPlan:
        text = humanize_text_sync(raw_text)
        read = self.rng.uniform(self.read_min, self.read_max)
        typing = typing_duration_sec(text, rng=self.rng)
        pause = self.rng.uniform(self.pause_min, self.pause_max)
        return BehaviorPlan(
            read_sec=read,
            typing_sec=typing,
            pause_sec=pause,
            text=text,
        )

    async def emulate_reading(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def emulate_typing(
        self,
        client: "TelegramClient",
        chat_id: int,
        seconds: float,
    ) -> None:
        """
        Keep ``typing`` alive for `seconds`, refreshing before Telegram's ~5s drop.
        """
        remaining = max(0.0, float(seconds))
        if remaining <= 0:
            return
        while remaining > 0:
            chunk = min(self.typing_refresh_sec, remaining)
            try:
                async with client.action(chat_id, "typing"):
                    await asyncio.sleep(chunk)
            except Exception:  # noqa: BLE001
                # Fallback: SetTypingRequest once, then sleep
                try:
                    from telethon.tl.functions.messages import SetTypingRequest
                    from telethon.tl.types import SendMessageTypingAction

                    await client(SetTypingRequest(chat_id, SendMessageTypingAction()))
                except Exception:  # noqa: BLE001
                    logger.debug("typing action failed chat=%s", chat_id, exc_info=True)
                await asyncio.sleep(chunk)
            remaining -= chunk

    async def emulate_presend_pause(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def run_before_send(
        self,
        client: "TelegramClient",
        chat_id: int,
        raw_text: str,
        *,
        skip_reading: bool = False,
    ) -> str:
        """
        Full pipeline: humanize → read → type → pause. Returns final text.
        """
        plan = self.plan(raw_text)
        logger.info(
            "Human behavior plan chat=%s read=%.1fs typing=%.1fs pause=%.1fs chars=%s",
            chat_id,
            plan.read_sec,
            plan.typing_sec,
            plan.pause_sec,
            len(plan.text),
        )
        if not skip_reading:
            await self.emulate_reading(plan.read_sec)
        await self.emulate_typing(client, chat_id, plan.typing_sec)
        await self.emulate_presend_pause(plan.pause_sec)
        return plan.text
