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
    # delay = пауза между шагами скролла (не время на всю главу).
    # blurb = ожидаемое время полной главы с переходом (скролл + next).
    "turbo": SpeedPreset("turbo", "Турбо", 0.03, 0.08, 2, 4, "~4–7 с/глава · ~500–900 гл/ч"),
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


def card(title: str, body: str) -> str:
    return f"<b>{title}</b>\n{hr()}\n{body}"


def welcome_text(
    remanga_on: bool,
    mb_on: bool,
    speed_label: str,
    chapters: int,
    battles: int,
    chapters_total: int = 0,
) -> str:
    r = "● online" if remanga_on else "○ idle"
    m = "● online" if mb_on else "○ idle"
    total_line = (
        f"📚 Всего прочитано: <b>{chapters_total}</b>\n" if chapters_total > 0 else ""
    )
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
        f"{total_line}"
        f"🗡 Боёв за сессию: <b>{battles}</b>\n\n"
        f"Выберите модуль ниже — или откройте «Пульс»."
    )


def speed_menu_text(dmin: float, dmax: float, preset_key: str = "") -> str:
    preset = SPEED_PRESETS.get(preset_key) or preset_by_delays(dmin, dmax)
    cur = preset.title if preset else f"Своя {dmin:.2f}–{dmax:.2f}с"
    lines = [
        "<b>🎚 Темп чтения</b>",
        hr(),
        f"Сейчас: <b>{cur}</b>",
        f"Пауза шага скролла: <code>{dmin:.2f}–{dmax:.2f}</code> сек",
        "",
        "В описании — время <b>целой главы</b> (скролл + переход).",
        "Число в пресете — пауза между шагами скролла.",
        "Рекомендация: <b>Турбо</b> для фарма, <b>Живой</b> для баланса.",
        "",
    ]
    for p in SPEED_PRESETS.values():
        mark = "›" if preset and preset.key == p.key else "·"
        lines.append(
            f"{mark} <b>{p.title}</b>  шаг <code>{p.delay_min:.2f}–{p.delay_max:.2f}с</code>"
            f" · {p.steps_min}–{p.steps_max} шаг.\n"
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
    chapters_total: int = 0,
    sec_per_chapter: float = 0.0,
    session_titles: int = 0,
) -> str:
    state = "● фарм идёт" if running else "○ пауза"
    # прогресс до следующей «вехи» из 10 глав сессии
    bar = progress_bar(chapters, 10)
    if sec_per_chapter > 0:
        pace = f"~{sec_per_chapter:.1f} с/гл"
    elif cph > 0:
        pace = f"~{3600.0 / cph:.1f} с/гл"
    else:
        pace = "замер…"
    total = chapters_total if chapters_total > 0 else chapters
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
        f"📖 За сессию <b>{chapters}</b> <i>(зачтено сайтом)</i>\n"
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
) -> str:
    return (
        f"<b>🃏 Карты · Эвенты</b>\n"
        f"<i>дропы · сундуки · паки · дейлики</i>\n"
        f"{hr()}\n"
        f"Автофарм: <code>{'● on' if events_on else '○ off'}</code>\n"
        f"Чтение: <code>{'● on' if read_on else '○ off'}</code>\n"
        f"Уведомления карт: <code>{'●' if notify_cards else '○'}</code>\n\n"
        f"🃏 Карт всего <b>{cards_total}</b> · сессия <b>{cards_session}</b>\n"
        f"📜 Свитки <b>{scrolls}</b>\n"
        f"📦 Сундуки <b>{chests}</b> · Паки <b>{packs}</b>\n"
        f"🎯 Эвенты <b>{events}</b> · 🎁 Награды <b>{rewards}</b>\n\n"
        f"Последний дроп: <i>{last_drop or '—'}</i>\n"
        f"📝 {last_action or '—'}\n\n"
        f"<i>Карты за чтение — из /notifications (до 10/сутки).\n"
        f"Сундуки/дейлики — /battle · паки — /cards/pack.</i>"
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
        f"⚔️ Remanga     {'●' if remanga_on else '○'}\n"
        f"📚 Чтение      {'●' if mb_on else '○'}\n"
        f"🃏 Карты/эвент {'●' if events_on else '○'}\n\n"
        f"📖 {chapters} гл{total} · {cph:.0f}/ч{pace}\n"
        f"🃏 Карт за сессию: <b>{cards_session}</b>\n"
        f"🗡 Боёв: <b>{battles}</b>\n"
        f"🎚 {speed}\n\n"
        f"<i>{last_mb or 'ожидание'}</i>"
    )


def help_text() -> str:
    return (
        f"<b>{BRAND}</b>\n"
        f"{hr()}\n"
        f"<b>Remanga</b> — автобои и уведомления.\n"
        f"<b>MangaBuff</b> — быстрое чтение тайтлов до 90%.\n"
        f"<b>Карты · Эвенты</b> — сундуки, паки, дейлики, дропы карт.\n\n"
        f"Карты за чтение смотрим в ленте /notifications.\n"
        f"Лимит: до 10 карт/сутки · свитки до 5/сутки (~1ч).\n"
        f"В Telegram — сразу при новом уведомлении о карте.\n\n"
        f"Ночь 01:00–05:00 МСК — пауза фарма."
    )
