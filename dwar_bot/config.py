"""
Centralized configuration for the Dwar browser game bot.

All timeouts are in seconds unless stated otherwise.
All CSS/XPath selectors target the live game DOM as of 2024–2026.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).parent.resolve()
COOKIES_DIR: Path = BASE_DIR / "cookies"
LOGS_DIR: Path = BASE_DIR / "logs"
SCREENSHOTS_DIR: Path = BASE_DIR / "screenshots"

for _d in (COOKIES_DIR, LOGS_DIR, SCREENSHOTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

LOG_FILE: Path = LOGS_DIR / "bot.log"
STATE_FILE: Path = BASE_DIR / "state.json"

# ---------------------------------------------------------------------------
# Game endpoints
# ---------------------------------------------------------------------------
GAME_BASE_URL: str = os.getenv("DWAR_BASE_URL", "https://dwar.ru")
# World-specific URLs (e.g. https://w1.dwar.ru)
_world = os.getenv("DWAR_WORLD", "w1")
GAME_WORLD_URL: str = os.getenv("DWAR_WORLD_URL", f"https://{_world}.dwar.ru")
GAME_LOGIN_URL: str = f"{GAME_WORLD_URL}/index.php"
GAME_GAME_URL: str = f"{GAME_WORLD_URL}/game.php"
GAME_PROFILE_URL: str = f"{GAME_WORLD_URL}/user.php"
GAME_BATTLE_URL: str = f"{GAME_WORLD_URL}/fight.php"
GAME_INVENTORY_URL: str = f"{GAME_WORLD_URL}/bag.php"

# Network request patterns to monitor/intercept
NETWORK_WATCH_PATTERNS: list[str] = [
    "**/ajax/**",
    "**/api/**",
    "**/game/action*",
    "**/battle*",
]

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
DWAR_USERNAME: str = os.getenv("DWAR_USERNAME", "")
DWAR_PASSWORD: str = os.getenv("DWAR_PASSWORD", "")

# Cookie file path — can be overridden via env var
DEFAULT_COOKIE_FILE: Path = COOKIES_DIR / os.getenv(
    "DWAR_COOKIE_FILE", "session_cookies.json"
)

# Required cookie names that must be present for a valid session.
# dwar.ru authentication requires:
#   sess_sid  — game server PHP session ID
#   mycom     — Astrum Play / mycom OAuth access+refresh token pair
# Override via env var (comma-separated): DWAR_REQUIRED_COOKIES=sess_sid,mycom
_req_env = os.getenv("DWAR_REQUIRED_COOKIES", "sess_sid,mycom")
REQUIRED_COOKIE_NAMES: frozenset[str] = frozenset(
    c.strip() for c in _req_env.split(",") if c.strip()
)

# The game world subdomain (w1, w2, w3, …).  Override via DWAR_WORLD env var.
DWAR_WORLD: str = os.getenv("DWAR_WORLD", "w1")

# Maximum cookie age in seconds before forced re-login (24 h)
COOKIE_MAX_AGE_SECONDS: int = 86_400

# How many sessions to keep in rotation
SESSION_ROTATION_POOL_SIZE: int = 3

# ---------------------------------------------------------------------------
# Browser / Playwright settings
# ---------------------------------------------------------------------------
HEADLESS: bool = os.getenv("DWAR_HEADLESS", "true").lower() == "true"
BROWSER_EXECUTABLE_PATH: Optional[str] = os.getenv("DWAR_BROWSER_PATH") or None

VIEWPORT: dict[str, int] = {"width": 1366, "height": 768}

# Realistic user-agent string (Chromium 124 on Linux)
USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

LOCALE: str = "ru-RU"
TIMEZONE: str = "Europe/Moscow"

# Maximum time (ms) to wait for a page/element action
PAGE_TIMEOUT_MS: int = 30_000
NAVIGATION_TIMEOUT_MS: int = 45_000

# Playwright launch options forwarded verbatim
BROWSER_LAUNCH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--window-size=1366,768",
    "--lang=ru-RU",
]

# Extra HTTP headers injected on every request
EXTRA_HTTP_HEADERS: dict[str, str] = {
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "DNT": "1",
}

# ---------------------------------------------------------------------------
# Anti-bot / human-like behavior
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DelayRange:
    """Inclusive [min, max] range (seconds) for asyncio.sleep calls."""
    min: float
    max: float


# Delays between generic actions (navigation, clicks, form fills)
DELAY_ACTION: DelayRange = DelayRange(0.8, 2.4)
# Delay between individual keystrokes when typing
DELAY_TYPING: DelayRange = DelayRange(0.05, 0.18)
# Delay between combat rounds / skill activations
DELAY_COMBAT: DelayRange = DelayRange(1.2, 3.5)
# Delay when reading NPC dialogue screens
DELAY_DIALOGUE: DelayRange = DelayRange(2.0, 5.0)
# Delay between crafting/profession ticks
DELAY_PROFESSION: DelayRange = DelayRange(3.0, 8.0)
# Long idle pause simulating a human stepping away
DELAY_IDLE: DelayRange = DelayRange(30.0, 120.0)
# Delay between full main-loop iterations
DELAY_MAIN_LOOP: DelayRange = DelayRange(5.0, 15.0)
# Delay before retrying a failed action
DELAY_RETRY: DelayRange = DelayRange(3.0, 7.0)

# Maximum number of retries for any single action
MAX_RETRIES: int = 5

# Probability (0–1) that the bot will trigger an idle pause per loop iteration
IDLE_PAUSE_PROBABILITY: float = 0.05

# Mouse movement: number of intermediate points when moving to a target
MOUSE_STEPS_RANGE: tuple[int, int] = (8, 25)

# ---------------------------------------------------------------------------
# DOM selectors  (CSS preferred; XPath only where CSS is insufficient)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Selectors:
    # --- Login page ---
    login_username: str = "#login-form input[name='login']"
    login_password: str = "#login-form input[name='password']"
    login_submit: str = "#login-form button[type='submit'], #login-form input[type='submit']"
    login_error: str = ".login-error, .error-message"

    # --- Navigation / global UI ---
    nav_profile: str = "a[href*='/profile'], .menu-profile"
    nav_inventory: str = "a[href*='/inventory'], .menu-inventory"
    nav_battle: str = "a[href*='/battle'], .menu-battle"
    nav_quests: str = "a[href*='/quest'], .menu-quests"
    loader_overlay: str = ".loader, .loading-overlay, #preloader"

    # --- Profile / stats ---
    char_name: str = ".char-name, #char-name, .profile-name"
    char_level: str = ".char-level, #char-level, .profile-level"
    char_hp_current: str = ".hp-current, #hp-current, .health-current"
    char_hp_max: str = ".hp-max, #hp-max, .health-max"
    char_mp_current: str = ".mp-current, #mp-current, .mana-current"
    char_mp_max: str = ".mp-max, #mp-max, .mana-max"
    char_exp_bar: str = ".exp-bar, #exp-bar, .experience-bar"
    char_exp_percent: str = ".exp-percent, .experience-percent"
    char_gold: str = ".gold-amount, #gold, .currency-gold"
    char_silver: str = ".silver-amount, #silver, .currency-silver"
    char_energy: str = ".energy-current, #energy, .stamina-current"
    char_energy_max: str = ".energy-max, .stamina-max"

    # --- Inventory / backpack ---
    inventory_slot: str = ".inventory-slot, .backpack-slot, .item-slot"
    inventory_item_name: str = ".item-name, .item-title"
    inventory_item_count: str = ".item-count, .item-quantity"
    elixir_hp: str = ".item[data-type='elixir-hp'], .potion-hp"
    elixir_mp: str = ".item[data-type='elixir-mp'], .potion-mp"

    # --- Combat ---
    combat_attack_btn: str = ".combat-attack, #btn-attack, button[data-action='attack']"
    combat_skill_btns: str = ".combat-skill, .skill-button, [data-action='skill']"
    combat_use_elixir: str = ".use-elixir, .use-potion, [data-action='use-item']"
    combat_enemy_hp: str = ".enemy-hp, .mob-health, #enemy-health"
    combat_enemy_name: str = ".enemy-name, .mob-name, #enemy-name"
    combat_log: str = ".combat-log, #battle-log, .fight-log"
    combat_log_entry: str = ".log-entry, .battle-entry, li.log-item"
    combat_result_win: str = ".battle-win, .combat-victory, #result-win"
    combat_result_lose: str = ".battle-lose, .combat-defeat, #result-lose"
    combat_result_btn: str = ".battle-result button, .combat-result .btn-ok"

    # --- Quests / NPC dialogue ---
    quest_list: str = ".quest-list, #quest-list, .missions-list"
    quest_item: str = ".quest-item, .mission-item"
    quest_title: str = ".quest-title, .mission-title"
    quest_status: str = ".quest-status, .mission-status"
    npc_dialogue_box: str = ".dialogue-box, #npc-dialog, .npc-speech"
    npc_dialogue_text: str = ".dialogue-text, .npc-text, .speech-text"
    npc_choice_btns: str = ".dialogue-choice, .npc-choice, .dialog-option"
    npc_accept_quest: str = "button[data-action='accept-quest'], .btn-accept"
    npc_complete_quest: str = "button[data-action='complete-quest'], .btn-complete"

    # --- Timers / professions ---
    timer_craft: str = ".craft-timer, #craft-cooldown, .profession-timer"
    timer_energy_restore: str = ".energy-timer, .stamina-regen-timer"
    profession_panel: str = ".profession-panel, #professions, .craft-panel"
    profession_item: str = ".profession-item, .craft-item"
    profession_craft_btn: str = ".btn-craft, .craft-start, [data-action='craft']"

    # --- Notifications ---
    notification_area: str = ".notifications, #notify-area, .alerts-container"
    notification_item: str = ".notification-item, .alert-item"
    notification_close: str = ".notification-close, .alert-close, .btn-dismiss"


SELECTORS: Selectors = Selectors()

# ---------------------------------------------------------------------------
# Combat configuration
# ---------------------------------------------------------------------------
@dataclass
class CombatConfig:
    # HP threshold (percent 0–100) below which the bot drinks an HP elixir
    hp_elixir_threshold: float = 40.0
    # MP threshold (percent 0–100) below which the bot drinks an MP elixir
    mp_elixir_threshold: float = 30.0
    # If True, prefer special skills over basic attack when available
    prefer_skills: bool = True
    # Maximum consecutive battles before forcing a rest cycle
    max_consecutive_battles: int = 20
    # HP percent below which the bot retreats and does not fight
    hp_retreat_threshold: float = 15.0
    # Whether to auto-loot after each battle
    auto_loot: bool = True
    # DwarBOT-adapted physical hit sequence (names or 1/2/3 zone ids).
    # Default matches DwarBOT config.ini: forward, down, down, up, forward
    hit_list: str = "forward, down, down, up, forward"
    # Prefer longest combo from fight|conf when available
    prefer_fight_combo: bool = True
    # Enter block (FS_PF_DEFENDED) when HP% falls below this
    hp_block_threshold: float = 45.0
    # Leave block when HP% rises above this
    hp_unblock_threshold: float = 60.0
    # Exit block before the last hit of the combo cycle (суперудар)
    unblock_before_finisher: bool = True
    # Heal with potion / food after a finished fight (DwarBOT post_battle_refresh)
    post_battle_heal: bool = True
    # BotMek.ru share macros (Легенда) — burst / down-hit / prebuff patterns
    botmek_enabled: bool = True
    # Optional preset name hint (e.g. "верка", "Ракхари"); empty = auto by level
    botmek_preset: str = ""
    # Drink BotMek-style combat elixirs (гнев/мощь) before hunt attack
    botmek_prebuff: bool = True
    # SUIS (dwar.browsergamebots.com) — apply public UI defaults + formula
    suis_enabled: bool = True
    # Optional SUIS fight string, e.g. «Б+ГНБ-Г» or «ГНТНГ» (empty = auto)
    suis_sequence: str = ""
    # Prefer SUIS hunt mob names for current level when picking targets
    suis_hunt_priority: bool = True
    # Session soft limits from SUIS (0 = disabled)
    suis_session_minutes: int = 60
    suis_session_kill_limit: int = 50
    # Post-battle food ladder (Еда после боя)
    suis_post_battle_food: bool = True
    # Skip food when HP% above this (SUIS «Не употреблять еду, если HP > N%»)
    suis_food_skip_above: float = 75.0
    # GameBots / Оповещатор v8 (yougame.biz/threads/184351) — skip occupied targets
    gamebots_enabled: bool = True
    # «Скрывать занятые» — only attack free hunt bots
    gamebots_skip_occupied: bool = True
    # Retry next free mob when server says target is busy
    gamebots_occupied_retry: int = 2
    # RF-Cheats t=403608 — session hygiene after autoban discussion
    rfcheats_hygiene_enabled: bool = True
    rfcheats_max_continuous_minutes: int = 240
    rfcheats_max_daily_minutes: int = 720
    rfcheats_burst_minutes: int = 60


COMBAT: CombatConfig = CombatConfig()

# ---------------------------------------------------------------------------
# Timers / profession cooldowns  (all values in seconds)
# ---------------------------------------------------------------------------
@dataclass
class TimerConfig:
    energy_regen_poll_interval: float = 60.0
    energy_full_threshold: float = 95.0   # percent
    craft_poll_interval: float = 30.0
    # How long to wait before re-checking a running profession
    profession_recheck_interval: float = 120.0
    # Global bot heartbeat — logs "still alive" at this interval
    heartbeat_interval: float = 300.0


TIMERS: TimerConfig = TimerConfig()

# ---------------------------------------------------------------------------
# Telegram notifications / multi-admin ACL
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
# Comma/space-separated Telegram user IDs allowed to control the bot.
# Example: TELEGRAM_ADMIN_IDS=111111111,222222222
# If empty, TELEGRAM_CHAT_ID is used as the sole admin (backward compatible).
TELEGRAM_ADMIN_IDS: str = os.getenv("TELEGRAM_ADMIN_IDS", "")
# Optional extra chat IDs that receive notifications (errors/reports), but
# without control rights unless also listed in TELEGRAM_ADMIN_IDS / CHAT_ID.
TELEGRAM_NOTIFY_CHAT_IDS: str = os.getenv("TELEGRAM_NOTIFY_CHAT_IDS", "")
# Allow group chats (default: private only). Admins are still checked by from.id.
TELEGRAM_ALLOW_GROUPS: bool = os.getenv("TELEGRAM_ALLOW_GROUPS", "0").lower() in (
    "1", "true", "yes", "on",
)
# Minimum log level to forward to Telegram: "INFO", "WARNING", "ERROR", "CRITICAL"
TELEGRAM_MIN_LEVEL: str = os.getenv("TELEGRAM_MIN_LEVEL", "WARNING")

# ---------------------------------------------------------------------------
# AI healing (Gemini auditor + Cursor executor)
# ---------------------------------------------------------------------------
CURSOR_API_KEY: str = os.getenv("CURSOR_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
HEALING_AUDIT_INTERVAL_SEC: int = int(os.getenv("HEALING_AUDIT_INTERVAL_SEC", "120"))
# Maximum messages per minute to avoid Telegram rate-limits
TELEGRAM_RATE_LIMIT: int = 20


def parse_telegram_ids(*raw_values: str) -> list[str]:
    """Parse comma/space/semicolon-separated Telegram chat/user IDs."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_values:
        if not raw:
            continue
        for part in re.split(r"[,;\s]+", str(raw).strip()):
            pid = part.strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            out.append(pid)
    return out


def resolve_telegram_admins(
    chat_id: str = "",
    admin_ids: str = "",
) -> list[str]:
    """
    Authorized controller IDs: TELEGRAM_ADMIN_IDS ∪ TELEGRAM_CHAT_ID.

    Empty admin list falls back to a single TELEGRAM_CHAT_ID for compatibility.
    """
    ids = parse_telegram_ids(admin_ids, chat_id)
    if ids:
        return ids
    return parse_telegram_ids(chat_id)


def resolve_telegram_notify_chats(
    chat_id: str = "",
    admin_ids: str = "",
    notify_ids: str = "",
) -> list[str]:
    """Chats that receive bot notifications (boot, errors, gameplay alerts)."""
    explicit = parse_telegram_ids(notify_ids)
    if explicit:
        return explicit
    return resolve_telegram_admins(chat_id=chat_id, admin_ids=admin_ids)

# ---------------------------------------------------------------------------
# Misc bot behavior
# ---------------------------------------------------------------------------
# If True, take a screenshot on every unhandled exception
SCREENSHOT_ON_ERROR: bool = True
# If True, save page HTML on every unhandled exception (debug aid)
DUMP_HTML_ON_ERROR: bool = False
# How many days to keep old log files
LOG_RETENTION_DAYS: int = 7
