"""
ui_theme.py — оформление и пресеты скорости MangaBuff Autopilot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


BRAND = "MangaBuff Autopilot"
BRAND_LINE = "чтение · карты · бои · обмены"


@dataclass(frozen=True)
class SpeedPreset:
    key: str
    title: str
    delay_min: float
    delay_max: float
    steps_min: int
    steps_max: int
    blurb: str


# blurb = реальная скорость с учётом addHistory (сайт лимитирует POST)
SPEED_PRESETS: Dict[str, SpeedPreset] = {
    "turbo": SpeedPreset(
        "turbo", "Турбо", 0.02, 0.05, 1, 1,
        "~5–8 с/гл · пакет 4 · ~450–700 гл/ч",
    ),
    "fast": SpeedPreset(
        "fast", "Быстрый", 0.08, 0.18, 2, 4,
        "~9–14 с/гл · ~250–400 гл/ч",
    ),
    "lively": SpeedPreset(
        "lively", "Живой", 0.20, 0.45, 4, 6,
        "~14–20 с/гл · ~180–260 гл/ч",
    ),
    "normal": SpeedPreset(
        "normal", "Норма", 0.50, 1.00, 6, 9,
        "~22–35 с/гл · ~100–160 гл/ч",
    ),
    "slow": SpeedPreset(
        "slow", "Неспешно", 1.00, 2.00, 8, 12,
        "~40–60 с/гл · ~60–90 гл/ч",
    ),
    "crawl": SpeedPreset(
        "crawl", "Медленно", 1.80, 3.50, 10, 16,
        "~70–100 с/гл · ~35–50 гл/ч",
    ),
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
    return "────────────────────────"


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
        f"Всего зачтено · <b>{chapters_total}</b>\n" if chapters_total > 0 else ""
    )
    return (
        f"<b>{BRAND}</b>\n"
        f"<i>{BRAND_LINE}</i>\n"
        f"{hr()}\n"
        f"Чтение  <code>{m}</code>\n"
        f"Карты   <code>{e}</code>\n"
        f"Темп    <b>{speed_label}</b>\n"
        f"{hr()}\n"
        f"Сессия  <b>{chapters}</b> гл · <b>{cards_session}</b> карт\n"
        f"{total_line}"
    )


def speed_menu_text(dmin: float, dmax: float, preset_key: str = "") -> str:
    preset = SPEED_PRESETS.get(preset_key) or preset_by_delays(dmin, dmax)
    cur = preset.title if preset else f"Своя {dmin:.2f}–{dmax:.2f}с"
    lines = [
        "<b>Темп чтения</b>",
        hr(),
        f"Сейчас · <b>{cur}</b>",
        f"Шаг скролла · <code>{dmin:.2f}–{dmax:.2f}с</code>",
        "",
        "<i>В описании — реальная скорость главы</i>",
        "<i>(скролл + addHistory на сайте).</i>",
        "",
    ]
    for p in SPEED_PRESETS.values():
        mark = "▸" if preset and preset.key == p.key else "·"
        lines.append(
            f"{mark} <b>{p.title}</b>\n"
            f"   <code>{p.delay_min:.2f}–{p.delay_max:.2f}с</code>"
            f" · {p.steps_min}–{p.steps_max} шаг\n"
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
    battles_won: int = 0,
    trades_sent: int = 0,
) -> str:
    state = "● фарм" if running else "○ пауза"
    bar = progress_bar(chapters, 10)
    if sec_per_chapter > 0:
        pace = f"{sec_per_chapter:.1f} с/гл"
    elif cph > 0:
        pace = f"{3600.0 / cph:.1f} с/гл"
    else:
        pace = "замер…"
    total = chapters_total if chapters_total > 0 else chapters
    pending = (
        f"\nОчередь  <b>{chapters_pending}</b> гл"
        if chapters_pending > 0
        else ""
    )
    titles_bit = (
        f"{session_titles} / {titles}" if session_titles > 0 else str(titles)
    )
    return (
        f"<b>MangaBuff · статус</b>\n"
        f"{hr()}\n"
        f"Состояние  <code>{state}</code>\n"
        f"Пресет     <b>{preset_title}</b>\n"
        f"Шаг        <code>{dmin:.2f}–{dmax:.2f}с</code>\n"
        f"Темп       <b>{cph:.0f}</b> гл/ч · {pace}\n"
        f"{hr()}\n"
        f"<code>{bar}</code>  {chapters}\n"
        f"Сессия     <b>{chapters}</b> <i>зачтено сайтом</i>{pending}\n"
        f"Всего      <b>{total}</b>\n"
        f"Тайтлы     <b>{titles_bit}</b>\n"
        f"Скроллы    <b>{pages}</b>\n"
        f"{hr()}\n"
        f"Карты      <b>{cards}</b> · награды <b>{rewards}</b>\n"
        f"Комменты   <b>{comments}</b>\n"
        f"Бои        <b>{battles_won}</b> побед · обмены <b>{trades_sent}</b>\n"
        f"{hr()}\n"
        f"Ночь  <code>{night or 'выкл до 01:00 МСК'}</code>\n"
        f"{last_action or '—'}\n"
        f"<code>{(last_url or '—')[:90]}</code>"
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
    auto_battle: bool = False,
    auto_trade: bool = False,
    battles_won: int = 0,
    battles_total: int = 0,
    trades_sent: int = 0,
) -> str:
    return (
        f"<b>Карты · бои · обмены</b>\n"
        f"{hr()}\n"
        f"Автофарм   <code>{'●' if events_on else '○'}</code>\n"
        f"Чтение     <code>{'●' if read_on else '○'}</code>\n"
        f"Бои        <code>{'●' if auto_battle else '○'}</code>\n"
        f"Обмены     <code>{'●' if auto_trade else '○'}</code>\n"
        f"Лоты       <code>{'●' if auto_market else '○'}</code>\n"
        f"Алерты     <code>{'●' if notify_cards else '○'}</code>\n"
        f"{hr()}\n"
        f"Карты      <b>{cards_total}</b> · сессия <b>{cards_session}</b>\n"
        f"Свитки     <b>{scrolls}</b>\n"
        f"Сундуки    <b>{chests}</b> · паки <b>{packs}</b>\n"
        f"Эвенты     <b>{events}</b> · награды <b>{rewards}</b>\n"
        f"Бои        <b>{battles_won}</b> / {battles_total}\n"
        f"Обмены     <b>{trades_sent}</b> отправлено\n"
        f"{hr()}\n"
        f"Дроп · <i>{last_drop or '—'}</i>\n"
        f"{last_action or '—'}\n\n"
        f"<i>Бои — пробуждение, поиск, итоги.\n"
        f"Обмен — A → просим S у разных игроков.\n"
        f"Улучшение / заточка — авто в цикле карт.</i>"
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
    pace = f" · {sec_per_chapter:.1f} с/гл" if sec_per_chapter > 0 else ""
    total = f" / {chapters_total}" if chapters_total > 0 else ""
    return (
        f"<b>Пульс</b>\n"
        f"{hr()}\n"
        f"Чтение     {'●' if mb_on else '○'}\n"
        f"Карты      {'●' if events_on else '○'}\n"
        f"{hr()}\n"
        f"<b>{chapters}</b> гл{total} · <b>{cph:.0f}</b>/ч{pace}\n"
        f"Карты сессии · <b>{cards_session}</b>\n"
        f"Темп · {speed}\n\n"
        f"<i>{last_mb or 'ожидание'}</i>"
    )


def help_text() -> str:
    return (
        f"<b>{BRAND}</b>\n"
        f"{hr()}\n"
        f"<b>Фарм</b> — чтение до ~90%, зачёт через addHistory.\n"
        f"<b>Карты</b> — сундуки, паки, дропы, площадка.\n"
        f"<b>Бои</b> — пробуждение, поиск боя, победы, дейлики.\n"
        f"<b>Обмены</b> — предложения A→S разным игрокам.\n"
        f"<b>Улучшение</b> — прокачка и заточка дублей.\n\n"
        f"Статистика глав — только то, что принял сайт.\n"
        f"Ночь 01:00–05:00 МСК — пауза."
    )
