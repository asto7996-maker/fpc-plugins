"""
Centralized configuration for the Dwar browser game bot.

All timeouts are in seconds unless stated otherwise.
All CSS/XPath selectors target the live game DOM as of 2024–2026.
"""

from __future__ import annotations

import os
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
GAME_LOGIN_URL: str = f"{GAME_BASE_URL}/login"
GAME_GAME_URL: str = f"{GAME_BASE_URL}/game"
GAME_PROFILE_URL: str = f"{GAME_BASE_URL}/game/profile"
GAME_BATTLE_URL: str = f"{GAME_BASE_URL}/game/battle"
GAME_INVENTORY_URL: str = f"{GAME_BASE_URL}/game/inventory"

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

# Required cookie names that must be present for a valid session
REQUIRED_COOKIE_NAMES: frozenset[str] = frozenset(
    ["PHPSESSID", "user_id", "auth_token"]
)

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
# Telegram notifications
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
# Minimum log level to forward to Telegram: "INFO", "WARNING", "ERROR", "CRITICAL"
TELEGRAM_MIN_LEVEL: str = os.getenv("TELEGRAM_MIN_LEVEL", "WARNING")
# Maximum messages per minute to avoid Telegram rate-limits
TELEGRAM_RATE_LIMIT: int = 20

# ---------------------------------------------------------------------------
# Misc bot behavior
# ---------------------------------------------------------------------------
# If True, take a screenshot on every unhandled exception
SCREENSHOT_ON_ERROR: bool = True
# If True, save page HTML on every unhandled exception (debug aid)
DUMP_HTML_ON_ERROR: bool = False
# How many days to keep old log files
LOG_RETENTION_DAYS: int = 7
