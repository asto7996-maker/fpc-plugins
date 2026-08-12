"""
Hygiene / anti-detect lessons from RF-Cheats thread
https://www.rf-cheats.ru/forum/showthread.php?t=403608

Thread: «АвтоБан за самописного бота в Дваре» (tubux, 01.02.2021).
A Delphi+Chromium pixel bot (BitBlt / SetCursorPos / mouse_event) ran
1–12h/day for months, then autoban — despite Random() click jitter:

    SetCursorPos(502 + Random(15), 560 + Random(3));
    Sleep(300 + Random(600));

Community notes (dark / others):
  • frontend can track mousemove — click-without-move is a giveaway
  • heatmaps / timing patterns matter more than tiny Random()
  • simulate human breaks (eat/drink/away), avoid marathon sessions
  • prefer ordinary browser/client over custom Chromium wrappers

We do NOT copy pixel bots. Our HTTP/WS client already skips BitBlt;
this module encodes the *session hygiene* and delay floors from that
discussion so farm bursts stay shorter and more human-like.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

RFCHEATS_THREAD = "https://www.rf-cheats.ru/forum/showthread.php?t=403608"
RFCHEATS_TITLE = "АвтоБан за самописного бота в Дваре"


@dataclass
class RfCheatsDefaults:
    """Soft limits inspired by the ban post + community advice."""
    # OP ran 1–12h/day → stay under marathon, but 8h hard-stop was killing farm
    max_continuous_minutes: int = 240   # 4h unbroken then short break
    max_daily_minutes: int = 720        # 12h active / calendar day
    # Burst farm then mandatory break (community: don't grind flat 24h)
    burst_minutes: int = 60
    break_min_minutes: float = 5.0
    break_max_minutes: float = 12.0
    # Their failed anti-detect delay floor (ms) → seconds for API pacing
    action_delay_min: float = 0.3
    action_delay_max: float = 0.9
    # Occasional longer "stare / think" before a pull
    think_pause_chance: float = 0.12
    think_pause_min: float = 2.0
    think_pause_max: float = 8.0
    # After hitting daily budget — cool down until next calendar day (not 45m loops)
    daily_exhausted_break_minutes: float = 30.0


RFCHEATS_DEFAULTS = RfCheatsDefaults()

LESSONS: tuple[str, ...] = (
    "BitBlt + SetCursorPos pixel bots are high-risk even with Random()",
    "Sleep(300+Random(600)) alone is not enough for long sessions",
    "Frontend may track mousemove — click-without-move is a tell",
    "Avoid 5–12h unbroken farm; take human breaks",
    "Prefer normal client/API patterns over custom Chromium wrappers",
    "Simulate operator pauses (away from PC), not only micro-jitter",
)


@dataclass
class HygieneDecision:
    should_pause: bool
    reason: str = ""
    sleep_sec: float = 0.0


class HygieneTracker:
    """
    Tracks continuous / daily activity and returns when a break is due.
    Optionally persists daily counters across process restarts.
    """

    def __init__(
        self,
        *,
        defaults: Optional[RfCheatsDefaults] = None,
        state_path: Optional[Path] = None,
    ) -> None:
        self.d = defaults or RFCHEATS_DEFAULTS
        self.state_path = state_path or (
            Path(__file__).resolve().parents[1] / "data" / "rfcheats_hygiene_state.json"
        )
        self.day: str = date.today().isoformat()
        self.daily_active_sec: float = 0.0
        self.continuous_started: float = 0.0
        self.burst_started: float = 0.0
        self.break_until: float = 0.0
        self.last_activity: float = 0.0
        self._load()

    def _load(self) -> None:
        try:
            if not self.state_path.is_file():
                return
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("day") == date.today().isoformat():
                self.day = data["day"]
                self.daily_active_sec = float(data.get("daily_active_sec") or 0)
                self.break_until = float(data.get("break_until") or 0)
        except Exception as exc:
            logger.debug("rfcheats hygiene load: %s", exc)

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {
                        "day": self.day,
                        "daily_active_sec": self.daily_active_sec,
                        "break_until": self.break_until,
                        "updated_at": time.time(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("rfcheats hygiene save: %s", exc)

    def _rollover_day(self) -> None:
        today = date.today().isoformat()
        if self.day != today:
            self.day = today
            self.daily_active_sec = 0.0
            self.continuous_started = 0.0
            self.burst_started = 0.0
            self._save()

    def note_activity(self, seconds: float = 1.0) -> None:
        """Call after meaningful farm/combat work."""
        self._rollover_day()
        now = time.time()
        if self.break_until and now < self.break_until:
            return
        if not self.continuous_started:
            self.continuous_started = now
        if not self.burst_started:
            self.burst_started = now
        # Gap > 10 min resets continuous / burst clocks
        if self.last_activity and (now - self.last_activity) > 600:
            self.continuous_started = now
            self.burst_started = now
        # Cap per-call so fight stubs can't inflate daily budget
        self.daily_active_sec += min(90.0, max(0.0, float(seconds)))
        self.last_activity = now
        self._save()

    def note_break(self, sleep_sec: float = 0.0) -> None:
        now = time.time()
        # Never extend an already-active break (prevents spin re-arm)
        if self.break_until and now < self.break_until:
            return
        self.break_until = now + max(0.0, float(sleep_sec))
        self.continuous_started = 0.0
        self.burst_started = 0.0
        self.last_activity = now
        self._save()

    def remaining_break_sec(self) -> float:
        self._rollover_day()
        now = time.time()
        if self.break_until and now < self.break_until:
            return max(0.0, self.break_until - now)
        return 0.0

    def clear_break(self) -> None:
        """Operator / self-heal: resume farm immediately."""
        self.break_until = 0.0
        self._save()

    @staticmethod
    def _seconds_until_local_midnight() -> float:
        from datetime import datetime, timedelta
        now = datetime.now()
        nxt = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0,
        )
        return max(60.0, (nxt - now).total_seconds())

    def check(self) -> HygieneDecision:
        """Return whether the bot should pause before more farm work."""
        self._rollover_day()
        now = time.time()
        if self.break_until and now < self.break_until:
            return HygieneDecision(
                True,
                "active break",
                max(1.0, self.break_until - now),
            )

        daily_limit = float(self.d.max_daily_minutes) * 60.0
        if daily_limit > 0 and self.daily_active_sec >= daily_limit:
            # Pause until next calendar day (rollover clears counter).
            # Old 45-min re-arm loop zeroed farm for hours with empty hunt ticks.
            pause = self._seconds_until_local_midnight()
            return HygieneDecision(
                True,
                f"daily budget {self.d.max_daily_minutes}min → until midnight",
                pause,
            )

        if self.continuous_started:
            cont = (now - self.continuous_started) / 60.0
            if self.d.max_continuous_minutes > 0 and cont >= self.d.max_continuous_minutes:
                pause = random.uniform(
                    self.d.break_min_minutes * 60.0,
                    self.d.break_max_minutes * 60.0,
                )
                return HygieneDecision(
                    True,
                    f"continuous {cont:.0f}≥{self.d.max_continuous_minutes}min",
                    pause,
                )

        if self.burst_started:
            burst = (now - self.burst_started) / 60.0
            if self.d.burst_minutes > 0 and burst >= self.d.burst_minutes:
                pause = random.uniform(
                    self.d.break_min_minutes * 60.0,
                    self.d.break_max_minutes * 60.0,
                )
                return HygieneDecision(
                    True,
                    f"burst {burst:.0f}≥{self.d.burst_minutes}min",
                    pause,
                )

        return HygieneDecision(False)

    def action_delay_sec(self) -> float:
        return random.uniform(self.d.action_delay_min, self.d.action_delay_max)

    def maybe_think_pause_sec(self) -> float:
        if random.random() < float(self.d.think_pause_chance):
            return random.uniform(self.d.think_pause_min, self.d.think_pause_max)
        return 0.0

    def status_dict(self) -> dict[str, Any]:
        self._rollover_day()
        now = time.time()
        cont = (
            (now - self.continuous_started) / 60.0 if self.continuous_started else 0.0
        )
        burst = (now - self.burst_started) / 60.0 if self.burst_started else 0.0
        return {
            "day": self.day,
            "daily_active_min": round(self.daily_active_sec / 60.0, 1),
            "daily_limit_min": self.d.max_daily_minutes,
            "continuous_min": round(cont, 1),
            "burst_min": round(burst, 1),
            "break_remaining_sec": max(0.0, self.break_until - now),
        }


def catalog_dict() -> dict[str, Any]:
    return {
        "source": RFCHEATS_THREAD,
        "title": RFCHEATS_TITLE,
        "fetched_at": time.time(),
        "defaults": asdict(RFCHEATS_DEFAULTS),
        "lessons": list(LESSONS),
        "op_failed_anti_detect_snippet": (
            "SetCursorPos(502 + Random(15), 560 + Random(3)); "
            "Sleep(300 + Random(600));"
        ),
        "op_runtime_profile": "1–12 hours/day for ~3–4 months before autoban",
        "community_tells": [
            "mousemove tracking / click-without-move",
            "click heatmaps",
            "custom Chromium fingerprint vs game client",
            "google-analytics in game client (suspected)",
        ],
        "integration": {
            "hygiene_tracker": True,
            "no_pixel_bot": True,
            "no_crack": True,
        },
    }


def save_catalog(path: Optional[Path] = None) -> Path:
    target = Path(path) if path else (
        Path(__file__).resolve().parents[1] / "data" / "rfcheats_catalog.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(catalog_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def load_catalog(path: Optional[Path] = None) -> dict[str, Any]:
    target = Path(path) if path else (
        Path(__file__).resolve().parents[1] / "data" / "rfcheats_catalog.json"
    )
    if target.is_file():
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("rfcheats catalog: %s", exc)
    return catalog_dict()
