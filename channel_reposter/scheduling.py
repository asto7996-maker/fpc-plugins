"""
scheduling.py — расчёт расписания автопостинга (без Telegram и без БД).

Модуль намеренно чистый: только функции над числами и строками, чтобы
логику «когда следующий цикл» можно было покрыть тестами.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Причины следующего запуска (для логов и статуса в панели)
REASON_INTERVAL = "interval"
REASON_CATCHUP = "catchup"
REASON_IDLE = "idle"
REASON_ERROR = "error"
REASON_FLOOD = "flood"

MIN_DELAY = 5.0
MIN_INTERVAL = 5.0
MAX_INTERVAL = 30 * 24 * 3600.0

# Пока новых постов нет — опрашиваем источник всё реже
IDLE_STEPS = (15.0, 30.0, 60.0)
ERROR_STEP = 30.0
MAX_ERROR_DELAY = 300.0
FLOOD_EXTRA = 5.0

_UNITS: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "с": 1.0,
    "сек": 1.0,
    "секунд": 1.0,
    "секунда": 1.0,
    "секунды": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "м": 60.0,
    "мин": 60.0,
    "минут": 60.0,
    "минута": 60.0,
    "минуты": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "ч": 3600.0,
    "час": 3600.0,
    "часа": 3600.0,
    "часов": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "д": 86400.0,
    "дн": 86400.0,
    "день": 86400.0,
    "дня": 86400.0,
    "дней": 86400.0,
    "сут": 86400.0,
    "сутки": 86400.0,
}

_DURATION_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zА-Яа-яЁё]*)", re.UNICODE
)


def parse_duration(raw: str, *, default_unit: float = 60.0) -> float:
    """
    Разобрать интервал, введённый человеком, в секунды.

    Поддерживает «30s», «15 мин», «2ч», «1.5h», «1д 6ч», «90».
    Число без суффикса трактуется как ``default_unit`` (по умолчанию минуты).

    Raises:
        ValueError: если строку не удалось разобрать или значение ≤ 0.
    """
    text = (raw or "").strip().lower().replace(",", ".")
    if not text:
        raise ValueError("Пустое значение интервала")

    leftover = _DURATION_RE.sub(" ", text)
    if leftover.strip(" .и,"):
        raise ValueError(
            f"Не понимаю «{raw.strip()}». Примеры: 30с, 15мин, 2ч, 1д, 1ч 30мин"
        )

    total = 0.0
    matched = False
    for m in _DURATION_RE.finditer(text):
        value = float(m.group("value"))
        unit_raw = (m.group("unit") or "").strip()
        if unit_raw:
            factor = _UNITS.get(unit_raw)
            if factor is None:
                # «мин.» / «часов.» и прочие хвосты
                factor = _UNITS.get(unit_raw.rstrip("."))
            if factor is None:
                raise ValueError(
                    f"Непонятная единица «{unit_raw}». "
                    "Используйте с / мин / ч / д (например 30мин, 2ч, 1д)"
                )
        else:
            factor = default_unit
        total += value * factor
        matched = True

    if not matched:
        raise ValueError("Не удалось разобрать интервал. Примеры: 30мин, 2ч, 1д")
    if total <= 0:
        raise ValueError("Интервал должен быть больше нуля")
    if total < MIN_INTERVAL:
        raise ValueError(f"Слишком маленький интервал (минимум {MIN_INTERVAL:.0f} сек)")
    if total > MAX_INTERVAL:
        raise ValueError("Слишком большой интервал (максимум 30 дней)")
    return total


def humanize_duration(seconds: float) -> str:
    """Секунды → «2 ч 30 мин» / «45 сек» / «1 д 6 ч»."""
    total = int(round(max(0.0, float(seconds))))
    if total < 60:
        return f"{total} сек"

    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes and not days:
        parts.append(f"{minutes} мин")
    if not parts:
        parts.append(f"{secs} сек")
    return " ".join(parts)


@dataclass(frozen=True)
class NextRun:
    """Сколько ждать до следующего цикла и почему."""

    delay: float
    reason: str

    def describe(self) -> str:
        titles = {
            REASON_INTERVAL: "по интервалу",
            REASON_CATCHUP: "догон очереди",
            REASON_IDLE: "нет новых постов",
            REASON_ERROR: "после ошибки",
            REASON_FLOOD: "ожидание Telegram (flood)",
        }
        return f"{humanize_duration(self.delay)} ({titles.get(self.reason, self.reason)})"


def plan_next_delay(
    *,
    published: int,
    interval_seconds: float,
    catchup: bool = False,
    catchup_seconds: float = 60.0,
    backlog: int = 0,
    idle_streak: int = 0,
    error_streak: int = 0,
    flood_seconds: float = 0.0,
) -> NextRun:
    """
    Посчитать паузу до следующего цикла.

    Приоритет: flood-ожидание → ошибка → успешная публикация → простой.
    В режиме «догон» (catchup) при накопленной очереди используется свой
    короткий интервал, иначе всегда соблюдается пользовательский интервал.
    """
    interval = max(MIN_INTERVAL, float(interval_seconds or MIN_INTERVAL))

    if flood_seconds and flood_seconds > 0:
        return NextRun(max(MIN_DELAY, float(flood_seconds) + FLOOD_EXTRA), REASON_FLOOD)

    if error_streak > 0:
        delay = min(ERROR_STEP * error_streak, MAX_ERROR_DELAY)
        return NextRun(max(MIN_DELAY, delay), REASON_ERROR)

    if published > 0:
        if catchup and backlog > 0:
            catch = max(MIN_DELAY, float(catchup_seconds or MIN_DELAY))
            # Догон только ускоряет: медленнее пользовательского интервала не ждём
            return NextRun(min(catch, interval), REASON_CATCHUP)
        return NextRun(interval, REASON_INTERVAL)

    # Ничего не опубликовали: чаще опрашиваем источник, но не реже интервала
    step = IDLE_STEPS[min(max(idle_streak, 1), len(IDLE_STEPS)) - 1]
    return NextRun(max(MIN_DELAY, min(step, interval)), REASON_IDLE)
