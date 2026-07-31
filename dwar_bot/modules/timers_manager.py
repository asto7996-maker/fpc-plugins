"""
Менеджер кулдаунов и таймеров игровых действий.

Регистрирует таймеры эликсиров, переходов, профессий и считает
оптимальный sleep между итерациями главного цикла.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from dwar_bot.config import BotConfig, config

logger = logging.getLogger(__name__)

# Стандартные имена таймеров
TIMER_POTION = "potion"
TIMER_TRAVEL = "travel"
TIMER_PROFESSION = "profession"
TIMER_COMBAT_ACTION = "combat_action"
TIMER_IDLE_PAUSE = "idle_pause"
TIMER_ANTI_BOT = "anti_bot"
TIMER_QUEST_STEP = "quest_step"


@dataclass(slots=True)
class TimerEntry:
    name: str
    ready_at: float
    duration_sec: float
    meta: Dict[str, str] = field(default_factory=dict)

    @property
    def remaining(self) -> float:
        return max(0.0, self.ready_at - time.monotonic())

    @property
    def is_ready(self) -> bool:
        return time.monotonic() >= self.ready_at


class TimersManager:
    """Реестр кулдаунов с расчётом оптимального сна между действиями."""

    def __init__(self, bot_config: Optional[BotConfig] = None) -> None:
        self._config = bot_config or config
        self._timers: Dict[str, TimerEntry] = {}

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def set_cooldown(
        self,
        timer_name: str,
        seconds: float,
        *,
        meta: Optional[Mapping[str, str]] = None,
    ) -> TimerEntry:
        """Ставит/обновляет кулдаун ``timer_name`` на ``seconds`` секунд."""
        name = (timer_name or "").strip()
        if not name:
            raise ValueError("timer_name не может быть пустым")
        duration = max(0.0, float(seconds))
        entry = TimerEntry(
            name=name,
            ready_at=time.monotonic() + duration,
            duration_sec=duration,
            meta=dict(meta or {}),
        )
        self._timers[name] = entry
        logger.debug(
            "Cooldownймер '%s' = %.2f сек (ready_at=+%.2f)",
            name,
            duration,
            duration,
        )
        return entry

    def is_ready(self, timer_name: str) -> bool:
        """True, если таймер отсутствует или уже истёк."""
        entry = self._timers.get(timer_name)
        if entry is None:
            return True
        ready = entry.is_ready
        if ready:
            # Ленивая очистка
            self._timers.pop(timer_name, None)
        return ready

    def remaining(self, timer_name: str) -> float:
        entry = self._timers.get(timer_name)
        if entry is None:
            return 0.0
        return entry.remaining

    def clear(self, timer_name: str) -> None:
        self._timers.pop(timer_name, None)

    def clear_all(self) -> None:
        self._timers.clear()

    def active_timers(self) -> List[TimerEntry]:
        now = time.monotonic()
        active = [t for t in self._timers.values() if t.ready_at > now]
        active.sort(key=lambda t: t.ready_at)
        return active

    def extend(self, timer_name: str, extra_seconds: float) -> TimerEntry:
        """Продлевает существующий кулдаун или создаёт новый."""
        entry = self._timers.get(timer_name)
        if entry is None or entry.is_ready:
            return self.set_cooldown(timer_name, extra_seconds)
        new_remaining = entry.remaining + max(0.0, extra_seconds)
        return self.set_cooldown(timer_name, new_remaining, meta=entry.meta)

    def set_from_wall_clock(
        self, timer_name: str, unix_ready_at: float
    ) -> TimerEntry:
        """Задаёт таймер по абсолютному unix-timestamp готовности."""
        seconds = max(0.0, unix_ready_at - time.time())
        return self.set_cooldown(timer_name, seconds)

    # ------------------------------------------------------------------
    # Оптимальный sleep
    # ------------------------------------------------------------------

    def next_wake_delay(
        self,
        *,
        min_sleep: Optional[float] = None,
        max_sleep: Optional[float] = None,
        consider: Optional[Iterable[str]] = None,
        idle_jitter: bool = True,
    ) -> float:
        """
        Считает, сколько спать до следующего осмысленного действия.

        Берёт ближайший активный кулдаун (из ``consider`` или всех),
        ограничивает диапазоном [min_sleep, max_sleep] и добавляет jitter,
        чтобы не крутить холостой CPU-цикл.
        """
        delays = self._config.delays
        low = (
            delays.idle_min * 0.5
            if min_sleep is None
            else max(0.05, float(min_sleep))
        )
        high = (
            max(low, self._config.loop_interval_sec)
            if max_sleep is None
            else max(low, float(max_sleep))
        )

        candidates = self.active_timers()
        if consider is not None:
            allow = set(consider)
            candidates = [t for t in candidates if t.name in allow]

        if not candidates:
            base = random.uniform(low, high) if idle_jitter else high
            return max(low, min(high, base))

        nearest = candidates[0].remaining
        # Не спим дольше high (чтобы периодически ресканить UI),
        # но и не меньше low.
        if nearest <= low:
            delay = low
        elif nearest >= high:
            delay = high
        else:
            delay = nearest

        if idle_jitter:
            delay *= random.uniform(0.9, 1.1)
            delay = max(low, min(high, delay))

        logger.debug(
            "next_wake_delay=%.2f (nearest=%s remaining=%.2f)",
            delay,
            candidates[0].name,
            candidates[0].remaining,
        )
        return delay

    def optimal_sleep(
        self,
        *,
        has_pending_tasks: bool = False,
        in_combat: bool = False,
        hp_critical: bool = False,
    ) -> float:
        """
        Высокоуровневый расчёт паузы для main_loop.

        - В бою / при критическом HP — короткие интервалы.
        - При активных задачах — loop_interval с джиттером.
        - Иначе — ждём ближайший кулдаун в пределах idle-диапазона.
        """
        delays = self._config.delays
        if in_combat or hp_critical:
            return random.uniform(delays.combat_min, delays.combat_max)

        if has_pending_tasks:
            base = self._config.loop_interval_sec
            return random.uniform(max(0.5, base * 0.6), base * 1.2)

        return self.next_wake_delay(
            min_sleep=delays.idle_min * 0.4,
            max_sleep=max(delays.idle_max, self._config.loop_interval_sec * 2),
            idle_jitter=True,
        )

    def snapshot(self) -> List[Dict[str, float | str]]:
        """Сериализация активных таймеров (для graceful shutdown)."""
        result: List[Dict[str, float | str]] = []
        for entry in self.active_timers():
            result.append(
                {
                    "name": entry.name,
                    "remaining": round(entry.remaining, 3),
                    "duration_sec": entry.duration_sec,
                }
            )
        return result

    def restore(self, items: Iterable[Mapping[str, object]]) -> None:
        """Восстанавливает таймеры из snapshot (remaining → cooldown)."""
        for item in items:
            name = str(item.get("name") or "")
            remaining = float(item.get("remaining") or 0.0)
            if name and remaining > 0:
                self.set_cooldown(name, remaining)

    def status_line(self) -> str:
        active = self.active_timers()
        if not active:
            return "timers: none"
        parts = [f"{t.name}={t.remaining:.1f}s" for t in active[:8]]
        return "timers: " + ", ".join(parts)
