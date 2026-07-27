"""
ui_theme.py — оформление и пресеты скорости MangaBuff Autopilot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


BRAND = "MangaBuff Autopilot"
BRAND_LINE = "чтение · карты · эвенты"


@dataclass(frozen=True)
class SpeedPreset:
    key: str
    title: str
    delay_min: float
    delay_max: float
    steps_min: int
    steps_max: int
    blurb: str


SPEED_PRESETS: Dict[str, SpeedPreset] = {
    "turbo": SpeedPreset("turbo", "Турбо", 0.03, 0.08, 1, 3, "~5–7 с/глава · ~500–700 гл/ч"),
    "fast": SpeedPreset("fast", "Быстрый", 0.08, 0.18, 3, 6, "~7–12 с/глава · ~300–500 гл/ч"),
    "lively": SpeedPreset("lively", "Живой", 0.20, 0.45, 5, 8, "~12–20 с/глава · ~180–300 гл/ч"),
    "normal": SpeedPreset("normal", "Норма", 0.50, 1.00, 7, 11, "~20–35 с/глава · ~100–180 гл/ч"),
    "slow": SpeedPreset("slow", "Неспешно", 1.00, 2.00, 9, 14, "~35–60 с/глава · ~60–100 гл/ч"),
    "crawl": SpeedPreset("crawl", "Медленно", 1.80, 3.50, 12, 18, "~60–100 с/глава · ~35–60 гл/ч"),
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


def welcome_text(
    mb_on: bool,
    speed_label: str,
    chapters: int,
    chapters_total: int = 0,
    events_on: bool = False,
    cards_session: int = 0,
) -> str:
    m = "● online" if mb_on else "○ idle"
    e = "● on" if events_on else "○ off"
    total_line = (
        f"📚 Всего: <b>{chapters_total}</b>\n" if chapters_total > 0 else ""
    )
    return (
        f"<b>{BRAND}</b>\n"
        f"<i>{BRAND_LINE}</i>\n"
        f"{hr()}\n"
        f"Чтение: <code>{m}</code> · Карты: <code>{e}</code>\n"
        f"🎚 {speed_label}\n"
        f"📖 Сессия: <b>{chapters}</b> гл · 🃏 <b>{cards_session}</b>\n"
        f"{total_line}"
    )


def speed_menu_text(dmin: float, dmax: float, preset_key: str = "") -> str:
    preset = SPEED_PRESETS.get(preset_key) or preset_by_delays(dmin, dmax)
    cur = preset.title if preset else f"Своя {dmin:.2f}–{dmax:.2f}с"
    lines = [
        "<b>🎚 Темп чтения</b>",
        hr(),
        f"Сейчас: <b>{cur}</b>",
        f"Пауза шага: <code>{dmin:.2f}–{dmax:.2f}</code> сек",
        "",
        "В описании — время целой главы (скролл + переход).",
        "",
    ]
    for p in SPEED_PRESETS.values():
        mark = "›" if preset and preset.key == p.key else "·"
        lines.append(
            f"{mark} <b>{p.title}</b>  шаг <code>{p.delay_min:.2f}–{p.delay_max:.2f}с</code>"
            f" · {p.steps_min}–{p.steps_max} шаг.\n"
            f"   <i>{p.blurb}</i>"
        )
    lines += ["", "Своя скорость — кнопка ниже."]
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
    chapters_total: int = 0,
    sec_per_chapter: float = 0.0,
    session_titles: int = 0,
    chapters_pending: int = 0,
) -> str:
    state = "● фарм идёт" if running else "○ пауза"
    bar = progress_bar(chapters, 10)
    if sec_per_chapter > 0:
        pace = f"~{sec_per_chapter:.1f} с/гл"
    elif cph > 0:
        pace = f"~{3600.0 / cph:.1f} с/гл"
    else:
        pace = "замер…"
    total = chapters_total if chapters_total > 0 else chapters
    pending_line = (
        f" · ⏳ <b>{chapters_pending}</b> в очереди"
        if chapters_pending > 0
        else ""
    )
    titles_line = (
        f"🏷 Тайтлы    <b>{session_titles}</b> за сессию · всего <b>{titles}</b>\n"
        if session_titles > 0
        else f"🏷 Тайтлы    <b>{titles}</b>\n"
    )
    return (
        f"<b>📚 MangaBuff</b>\n"
        f"<i>авточтение · награды · ивенты</i>\n"
        f"{hr()}\n"
        f"Статус: <code>{state}</code>\n"
        f"Пресет: <b>{preset_title}</b>\n"
        f"Пауза шага: <code>{dmin:.2f}–{dmax:.2f}с</code>\n"
        f"Факт: <b>{cph:.0f}</b> гл/час · {pace}\n"
        f"Сессия: <code>{bar}</code>  {chapters} гл.\n\n"
        f"📖 За сессию <b>{chapters}</b> <i>(зачтено сайтом)</i>{pending_line}\n"
        f"📚 Всего     <b>{total}</b>\n"
        f"📄 Скроллы   <b>{pages}</b>\n"
        f"{titles_line}"
        f"🎁 Награды   <b>{rewards}</b> · 🃏 <b>{cards}</b>\n"
        f"💬 Комменты  <b>{comments}</b>\n\n"
        f"🌙 Ночь: <code>{night or 'выкл до 01:00 МСК'}</code>\n"
        f"📝 {last_action or '—'}\n"
        f"🔗 <code>{(last_url or '—')[:100]}</code>"
    )


def cards_events_home(
    events_on: bool,
    read_on: bool,
    cards_total: int,
    cards_session: int,
    scrolls: int,
    chests: int,
    packs: int,
    events: int,
    rewards: int,
    notify_cards: bool,
    last_drop: str,
    last_action: str,
    auto_market: bool = False,
) -> str:
    return (
        f"<b>🃏 Карты · Эвенты</b>\n"
        f"<i>дропы · сундуки · паки · площадка</i>\n"
        f"{hr()}\n"
        f"Автофарм: <code>{'● on' if events_on else '○ off'}</code>\n"
        f"Чтение: <code>{'● on' if read_on else '○ off'}</code>\n"
        f"Уведомления карт: <code>{'●' if notify_cards else '○'}</code>\n"
        f"Авто-лоты: <code>{'●' if auto_market else '○'}</code>\n\n"
        f"🃏 Карт всего <b>{cards_total}</b> · сессия <b>{cards_session}</b>\n"
        f"📜 Свитки <b>{scrolls}</b>\n"
        f"📦 Сундуки <b>{chests}</b> · Паки <b>{packs}</b>\n"
        f"🎯 Эвенты <b>{events}</b> · 🎁 Награды <b>{rewards}</b>\n\n"
        f"Последний дроп: <i>{last_drop or '—'}</i>\n"
        f"📝 {last_action or '—'}\n\n"
        f"<i>Дроп — с редкостью · на площадку только топ-10 дорогих.\n"
        f"Лот: 1× ранг выше (X → 2×X); раз в сутки ↔ 2× та же.\n"
        f"Сундуки — /battle · паки — /cards/pack.</i>"
    )


def pulse_text(
    mb_on: bool,
    chapters: int,
    cph: float,
    speed: str,
    last_mb: str,
    chapters_total: int = 0,
    sec_per_chapter: float = 0.0,
    events_on: bool = False,
    cards_session: int = 0,
) -> str:
    pace = f" · ~{sec_per_chapter:.1f} с/гл" if sec_per_chapter > 0 else ""
    total = f" / всего {chapters_total}" if chapters_total > 0 else ""
    return (
        f"<b>✦ Пульс</b>\n"
        f"{hr()}\n"
        f"📚 Чтение      {'●' if mb_on else '○'}\n"
        f"🃏 Карты/эвент {'●' if events_on else '○'}\n\n"
        f"📖 {chapters} гл{total} · {cph:.0f}/ч{pace}\n"
        f"🃏 Карт за сессию: <b>{cards_session}</b>\n"
        f"🎚 {speed}\n\n"
        f"<i>{last_mb or 'ожидание'}</i>"
    )


def help_text() -> str:
    return (
        f"<b>{BRAND}</b>\n"
        f"{hr()}\n"
        f"<b>Фарм</b> — чтение тайтлов до 90%, главы через addHistory.\n"
        f"<b>Карты · Эвенты</b> — сундуки, паки, дейлики, дропы, площадка.\n\n"
        f"Карты — лента /notifications · редкость в уведомлении.\n"
        f"Площадка — топ-10 самых дорогих; 1× выше / X→2×X; сутки ↔ 2× та же.\n"
        f"Свитки — до 5/сутки (~1ч).\n"
        f"Ночь 01:00–05:00 МСК — пауза."
    )
