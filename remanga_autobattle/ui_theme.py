"""
ui_theme.py — оформление сообщений и пресеты скорости Orion Autopilot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


BRAND = "Orion Autopilot"
BRAND_LINE = "Remanga × MangaBuff"


@dataclass(frozen=True)
class SpeedPreset:
    key: str
    title: str
    delay_min: float
    delay_max: float
    steps_min: int
    steps_max: int
    blurb: str


# Чуть выше среднего по умолчанию — «Живой»
SPEED_PRESETS: Dict[str, SpeedPreset] = {
    "turbo": SpeedPreset("turbo", "Турбо", 1.4, 2.8, 6, 9, "максимум темпа, всё ещё с паузами"),
    "fast": SpeedPreset("fast", "Быстрый", 2.0, 3.8, 7, 11, "уверенное чтение без суеты"),
    "lively": SpeedPreset("lively", "Живой", 2.8, 5.5, 8, 12, "чуть выше среднего · по умолчанию"),
    "normal": SpeedPreset("normal", "Норма", 4.0, 7.5, 10, 14, "спокойный человеческий ритм"),
    "slow": SpeedPreset("slow", "Неспешно", 6.0, 11.0, 12, 16, "максимально «живой» темп"),
    "crawl": SpeedPreset("crawl", "Медленно", 9.0, 16.0, 14, 18, "очень осторожное чтение"),
}

DEFAULT_SPEED_KEY = "lively"


def preset_by_delays(dmin: float, dmax: float) -> Optional[SpeedPreset]:
    for p in SPEED_PRESETS.values():
        if abs(p.delay_min - dmin) < 0.05 and abs(p.delay_max - dmax) < 0.05:
            return p
    return None


def progress_bar(value: int, maximum: int = 10, width: int = 10) -> str:
    if maximum <= 0:
        maximum = 1
    filled = max(0, min(width, int(round(width * (value % maximum) / maximum))))
    return "▓" * filled + "░" * (width - filled)


def hr() -> str:
    return "────────────────────"


def card(title: str, body: str) -> str:
    return f"<b>{title}</b>\n{hr()}\n{body}"


def welcome_text(remanga_on: bool, mb_on: bool, speed_label: str, chapters: int, battles: int) -> str:
    r = "● online" if remanga_on else "○ idle"
    m = "● online" if mb_on else "○ idle"
    return (
        f"<b>{BRAND}</b>\n"
        f"<i>{BRAND_LINE}</i>\n"
        f"{hr()}\n"
        f"Личный автопилот для двух миров.\n"
        f"Чистый интерфейс · живые паузы · полный контроль.\n\n"
        f"<b>Модули</b>\n"
        f"⚔️ Remanga   <code>{r}</code>\n"
        f"📚 MangaBuff <code>{m}</code>\n\n"
        f"<b>Сейчас</b>\n"
        f"🎚 Скорость: <b>{speed_label}</b>\n"
        f"📖 Глав за сессию: <b>{chapters}</b>\n"
        f"🗡 Боёв за сессию: <b>{battles}</b>\n\n"
        f"Выберите модуль ниже — или откройте «Пульс»."
    )


def speed_menu_text(dmin: float, dmax: float, preset_key: str = "") -> str:
    preset = SPEED_PRESETS.get(preset_key) or preset_by_delays(dmin, dmax)
    cur = preset.title if preset else f"Своя {dmin:.1f}–{dmax:.1f}с"
    lines = [
        "<b>🎚 Темп чтения</b>",
        hr(),
        f"Сейчас: <b>{cur}</b>",
        f"Пауза на шаг: <code>{dmin:.1f}–{dmax:.1f}</code> сек",
        "",
        "Пресеты подобраны под «живого» читателя.",
        "Рекомендация: <b>Живой</b> — чуть выше среднего.",
        "",
    ]
    for p in SPEED_PRESETS.values():
        mark = "›" if preset and preset.key == p.key else "·"
        lines.append(
            f"{mark} <b>{p.title}</b>  <code>{p.delay_min:.1f}–{p.delay_max:.1f}с</code>\n"
            f"   <i>{p.blurb}</i>"
        )
    lines += ["", "Или задайте свою: кнопка «Своя скорость»."]
    return "\n".join(lines)


def mangabuff_home(
    running: bool,
    dmin: float,
    dmax: float,
    chapters: int,
    pages: int,
    titles: int,
    rewards: int,
    cards: int,
    comments: int,
    cph: float,
    last_action: str,
    last_url: str,
    night: str,
    preset_title: str,
) -> str:
    state = "● фарм идёт" if running else "○ пауза"
    bar = progress_bar(chapters, 10)
    return (
        f"<b>📚 MangaBuff</b>\n"
        f"<i>авточтение · награды · ивенты</i>\n"
        f"{hr()}\n"
        f"Статус: <code>{state}</code>\n"
        f"Темп: <b>{preset_title}</b> · <code>{dmin:.1f}–{dmax:.1f}с</code>\n"
        f"Прогресс: <code>{bar}</code>  {chapters} гл.\n\n"
        f"📖 Главы     <b>{chapters}</b>\n"
        f"📄 Скроллы   <b>{pages}</b>\n"
        f"🏷 Тайтлы    <b>{titles}</b>\n"
        f"🎁 Награды   <b>{rewards}</b> · 🃏 <b>{cards}</b>\n"
        f"💬 Комменты  <b>{comments}</b>\n"
        f"📈 Темп      <b>{cph:.1f}</b> гл/час\n\n"
        f"🌙 Ночь: <code>{night or 'выкл до 01:00 МСК'}</code>\n"
        f"📝 {last_action or '—'}\n"
        f"🔗 <code>{(last_url or '—')[:100]}</code>"
    )


def remanga_home(running: bool, interval: int, wins: int, losses: int, draws: int, total: int) -> str:
    state = "● автобой" if running else "○ пауза"
    wr = (wins / total * 100) if total else 0.0
    return (
        f"<b>⚔️ Remanga</b>\n"
        f"<i>murim-cards · дуэли</i>\n"
        f"{hr()}\n"
        f"Статус: <code>{state}</code>\n"
        f"Интервал: <b>{interval}</b> сек\n\n"
        f"Боёв: <b>{total}</b>\n"
        f"🏆 {wins}   💀 {losses}   🤝 {draws}\n"
        f"Winrate: <b>{wr:.1f}%</b>"
    )


def pulse_text(
    remanga_on: bool,
    mb_on: bool,
    chapters: int,
    cph: float,
    battles: int,
    speed: str,
    last_mb: str,
) -> str:
    return (
        f"<b>✦ Пульс системы</b>\n"
        f"{hr()}\n"
        f"⚔️ Remanga   {'●' if remanga_on else '○'}\n"
        f"📚 MangaBuff {'●' if mb_on else '○'}\n\n"
        f"📖 {chapters} гл · {cph:.1f}/час\n"
        f"🗡 {battles} боёв за сессию\n"
        f"🎚 {speed}\n\n"
        f"<i>{last_mb or 'ожидание активности'}</i>"
    )


def help_text() -> str:
    return (
        f"<b>{BRAND}</b>\n"
        f"{hr()}\n"
        f"<b>Remanga</b> — автобои, статус, рейтинг, тонкие уведомления.\n"
        f"<b>MangaBuff</b> — фарм популярных тайтлов, награды, макеты, "
        f"редкие комментарии, ночной сон 01:00–05:00 МСК.\n\n"
        f"<b>Темп</b> регулируется пресетами или своей парой секунд.\n"
        f"<b>Пульс</b> — мгновенный снимок обоих модулей.\n"
        f"<b>Отчёт</b> — вехи каждые N глав в чат.\n\n"
        f"Сессии браузера раздельные и переживают перезапуск."
    )
