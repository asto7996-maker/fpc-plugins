"""Конфигурация Dwar-бота.

Модуль содержит все статические параметры бота:
    * пути к каталогам и файлам;
    * URL-адреса игры и целевых страниц;
    * CSS/XPath-селекторы;
    * диапазоны задержек (для эмуляции человеческого поведения);
    * настройки Playwright, прокси, Telegram-уведомлений и логирования.

Приоритет получения значения: ENV → .env → значение по умолчанию (`_DEFAULT_*`).
Ничего, кроме констант, тут не выполняется — модуль безопасно импортируется
из любой точки проекта.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


# ---------------------------------------------------------------------------
# .env loader (мини-реализация, чтобы не тянуть python-dotenv жёстко)
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    """Загружает переменные из .env-файла в os.environ, если файл существует.

    Не переопределяет уже установленные переменные окружения. Формат:
    ``KEY=VALUE`` (значения в кавычках допускаются). Комментарии — с ``#``.
    """
    if not path.is_file():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        # Отсутствие .env не является ошибкой уровня приложения.
        return


# ---------------------------------------------------------------------------
# Базовые пути
# ---------------------------------------------------------------------------
BASE_DIR: Final[Path] = Path(__file__).resolve().parent
PROJECT_ROOT: Final[Path] = BASE_DIR.parent
LOG_DIR: Final[Path] = BASE_DIR / "logs"
SESSIONS_DIR: Final[Path] = BASE_DIR / "sessions"
COOKIES_DIR: Final[Path] = BASE_DIR / "sessions" / "cookies"
STORAGE_STATE_DIR: Final[Path] = BASE_DIR / "sessions" / "storage_state"
SCREENSHOTS_DIR: Final[Path] = LOG_DIR / "screenshots"

for _p in (LOG_DIR, SESSIONS_DIR, COOKIES_DIR, STORAGE_STATE_DIR, SCREENSHOTS_DIR):
    _p.mkdir(parents=True, exist_ok=True)

_load_dotenv(PROJECT_ROOT / ".env")
_load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# Вспомогательные функции доступа к ENV
# ---------------------------------------------------------------------------
def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    if value is None or value == "":
        return default
    return value


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


# ---------------------------------------------------------------------------
# URL-адреса игры
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GameURLs:
    """Все известные URL-эндпоинты Dwar."""

    base: str = "https://www.dwar.ru"
    login: str = "https://www.dwar.ru/"
    main: str = "https://www.dwar.ru/main.php"
    map_root: str = "https://www.dwar.ru/map.php"
    inventory: str = "https://www.dwar.ru/user_inventory.php"
    profile: str = "https://www.dwar.ru/user.php"
    quest_book: str = "https://www.dwar.ru/quest_book.php"
    fight: str = "https://www.dwar.ru/fight.php"
    novice: str = "https://www.dwar.ru/novice.php"
    messages: str = "https://www.dwar.ru/user_messages.php"
    news: str = "https://www.dwar.ru/user_news.php"
    professions: str = "https://www.dwar.ru/user_prof.php"
    bank: str = "https://www.dwar.ru/user_bank.php"
    market: str = "https://www.dwar.ru/market.php"

    @property
    def allowed_domains(self) -> tuple[str, ...]:
        """Домены, на которые боту разрешено уходить."""
        return ("dwar.ru", "www.dwar.ru")


URLS: Final[GameURLs] = GameURLs()


# ---------------------------------------------------------------------------
# CSS/XPath селекторы
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Selectors:
    """Селекторы DOM-элементов игры.

    Собраны в одном месте, чтобы упростить поддержку при изменениях верстки.
    """

    # ---- Авторизация ----
    login_form: str = "form[action*='login']"
    login_input: str = "input[name='login']"
    password_input: str = "input[name='password']"
    login_submit: str = "input[type='submit'], button[type='submit']"
    logout_link: str = "a[href*='logout']"
    logged_in_marker: str = "a[href*='user.php']"

    # ---- Профиль / статы ----
    profile_hp: str = "img[src*='life.gif'] + b, .stat_hp"
    profile_mp: str = "img[src*='mana.gif'] + b, .stat_mp"
    profile_energy: str = "img[src*='energy.gif'] + b, .stat_energy"
    profile_money_gold: str = "img[src*='gold.gif'] + b"
    profile_money_silver: str = "img[src*='silver.gif'] + b"
    profile_money_copper: str = "img[src*='copper.gif'] + b"
    profile_level: str = "td:has(> b:contains('Уровень')) + td"
    profile_experience: str = "td:has(> b:contains('Опыт')) + td"

    # ---- Уведомления в шапке ----
    header_notify: str = "div#notify, .notify_block"
    unread_messages: str = "a[href*='user_messages'] b"
    unread_news: str = "a[href*='user_news'] b"

    # ---- Инвентарь ----
    inventory_slot: str = "table.inventory td.slot, .inv_slot"
    inventory_item_use_btn: str = "input[value='Использовать']"
    inventory_item_tooltip: str = ".item_tooltip, .tooltip"

    # ---- Бой ----
    fight_form: str = "form[name='fight'], form[action*='fight']"
    fight_attack_zones: str = "input[name='hit1'], input[name='hit2'], input[name='hit3']"
    fight_defense_zones: str = "input[name='def1'], input[name='def2']"
    fight_submit: str = "input[type='submit'][value*='ти']"
    fight_log: str = ".fight_log, #fight_log, table.log"
    fight_result_win: str = "img[src*='win.gif'], .win"
    fight_result_lose: str = "img[src*='lose.gif'], .lose"
    fight_elixir_slot: str = "input[name^='elixir']"

    # ---- Квесты ----
    quest_dialog: str = ".dialog, .npc_dialog, table.dialog"
    quest_option: str = "a[href*='quest'], a[href*='dialog']"
    quest_accept_btn: str = "input[value*='Принять']"
    quest_complete_btn: str = "input[value*='Завершить']"

    # ---- Общее ----
    generic_error: str = ".error, .err_text"
    captcha_image: str = "img[src*='captcha']"
    captcha_input: str = "input[name='captcha']"


SELECTORS: Final[Selectors] = Selectors()


# ---------------------------------------------------------------------------
# Задержки для эмуляции человека
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DelayProfile:
    """Диапазоны случайных задержек (сек)."""

    micro: tuple[float, float] = (0.08, 0.22)        # между микро-действиями (движения мыши)
    click: tuple[float, float] = (0.35, 1.10)        # между кликами
    typing: tuple[float, float] = (0.04, 0.18)       # между вводом символов
    navigation: tuple[float, float] = (1.80, 3.60)   # между переходами страниц
    action: tuple[float, float] = (2.50, 5.20)       # между значимыми игровыми действиями
    long_pause: tuple[float, float] = (25.0, 90.0)   # редкий "человеческий отдых"
    poll_interval: tuple[float, float] = (8.0, 14.0) # опрос событий/уведомлений
    retry_backoff: tuple[float, float] = (4.0, 9.0)  # повтор при сетевых ошибках


DELAYS: Final[DelayProfile] = DelayProfile()


# ---------------------------------------------------------------------------
# Настройки браузера / Playwright
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BrowserSettings:
    """Параметры запуска браузера Playwright."""

    engine: str = _env_str("DWAR_BROWSER", "chromium")            # chromium | firefox | webkit
    headless: bool = _env_bool("DWAR_HEADLESS", True)
    slow_mo_ms: int = _env_int("DWAR_SLOWMO_MS", 0)
    viewport_width: int = _env_int("DWAR_VIEWPORT_W", 1366)
    viewport_height: int = _env_int("DWAR_VIEWPORT_H", 768)
    locale: str = _env_str("DWAR_LOCALE", "ru-RU")
    timezone: str = _env_str("DWAR_TZ", "Europe/Moscow")
    user_agent: str = _env_str(
        "DWAR_USER_AGENT",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    default_timeout_ms: int = _env_int("DWAR_DEFAULT_TIMEOUT_MS", 20_000)
    navigation_timeout_ms: int = _env_int("DWAR_NAV_TIMEOUT_MS", 30_000)
    proxy_server: str | None = os.environ.get("DWAR_PROXY_SERVER") or None
    proxy_username: str | None = os.environ.get("DWAR_PROXY_USERNAME") or None
    proxy_password: str | None = os.environ.get("DWAR_PROXY_PASSWORD") or None


BROWSER: Final[BrowserSettings] = BrowserSettings()


# ---------------------------------------------------------------------------
# Учётные данные и параметры сессий
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Credentials:
    """Учётные данные для авторизации.

    Никогда не логируйте эти поля целиком! `logger.py` маскирует их автоматически.
    """

    login: str = _env_str("DWAR_LOGIN", "")
    password: str = _env_str("DWAR_PASSWORD", "")
    cookies_file: str = _env_str("DWAR_COOKIES_FILE", "")
    cookies_dir: str = _env_str("DWAR_COOKIES_DIR", str(COOKIES_DIR))
    rotate_sessions: bool = _env_bool("DWAR_ROTATE_SESSIONS", True)
    session_ttl_hours: int = _env_int("DWAR_SESSION_TTL_HOURS", 8)


CREDS: Final[Credentials] = Credentials()


# ---------------------------------------------------------------------------
# Telegram-уведомления
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TelegramSettings:
    """Настройки Telegram-бота для уведомлений."""

    enabled: bool = _env_bool("DWAR_TG_ENABLED", False)
    bot_token: str = _env_str("DWAR_TG_TOKEN", "")
    chat_id: str = _env_str("DWAR_TG_CHAT_ID", "")
    parse_mode: str = _env_str("DWAR_TG_PARSE_MODE", "HTML")
    notify_on_error: bool = _env_bool("DWAR_TG_NOTIFY_ERROR", True)
    notify_on_captcha: bool = _env_bool("DWAR_TG_NOTIFY_CAPTCHA", True)
    notify_on_low_hp: bool = _env_bool("DWAR_TG_NOTIFY_LOW_HP", True)
    rate_limit_seconds: int = _env_int("DWAR_TG_RATE_LIMIT", 30)


TELEGRAM: Final[TelegramSettings] = TelegramSettings()


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LoggingSettings:
    """Параметры логгера."""

    log_file: Path = LOG_DIR / "bot.log"
    error_file: Path = LOG_DIR / "error.log"
    level: str = _env_str("DWAR_LOG_LEVEL", "INFO")
    max_bytes: int = _env_int("DWAR_LOG_MAX_BYTES", 5 * 1024 * 1024)
    backup_count: int = _env_int("DWAR_LOG_BACKUPS", 5)
    fmt: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt: str = "%Y-%m-%d %H:%M:%S"
    console: bool = _env_bool("DWAR_LOG_CONSOLE", True)


LOGGING: Final[LoggingSettings] = LoggingSettings()


# ---------------------------------------------------------------------------
# Боевой движок
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CombatSettings:
    """Настройки поведения combat_engine."""

    hp_elixir_threshold: float = _env_float("DWAR_HP_ELIXIR_THRESHOLD", 0.40)
    mp_elixir_threshold: float = _env_float("DWAR_MP_ELIXIR_THRESHOLD", 0.30)
    retreat_hp_threshold: float = _env_float("DWAR_RETREAT_HP_THRESHOLD", 0.15)
    max_rounds: int = _env_int("DWAR_MAX_ROUNDS", 25)
    attack_zones: tuple[str, ...] = ("head", "chest", "belly", "legs")
    defense_zones: tuple[str, ...] = ("head", "chest", "belly", "legs")
    prefer_random_pattern: bool = True


COMBAT: Final[CombatSettings] = CombatSettings()


# ---------------------------------------------------------------------------
# Тайминги профессий / энергии
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TimersSettings:
    """Задержки восстановления игровых ресурсов (сек)."""

    energy_regen_seconds: int = 240
    mana_regen_seconds: int = 60
    hp_regen_seconds: int = 60
    profession_cooldown_seconds: int = 3600
    quest_poll_seconds: int = _env_int("DWAR_QUEST_POLL_SECONDS", 300)


TIMERS: Final[TimersSettings] = TimersSettings()


# ---------------------------------------------------------------------------
# Feature-flags модулей
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModuleToggles:
    """Позволяет выключать/включать модули без правки кода."""

    enable_combat: bool = _env_bool("DWAR_ENABLE_COMBAT", True)
    enable_quests: bool = _env_bool("DWAR_ENABLE_QUESTS", True)
    enable_professions: bool = _env_bool("DWAR_ENABLE_PROFESSIONS", True)
    enable_stats_parser: bool = _env_bool("DWAR_ENABLE_STATS", True)
    enable_anti_bot: bool = _env_bool("DWAR_ENABLE_ANTIBOT", True)


MODULES: Final[ModuleToggles] = ModuleToggles()


# ---------------------------------------------------------------------------
# Публичный "плоский" интерфейс для удобного импорта
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AppConfig:
    """Агрегатор всех подсистем конфигурации."""

    urls: GameURLs = field(default_factory=lambda: URLS)
    selectors: Selectors = field(default_factory=lambda: SELECTORS)
    delays: DelayProfile = field(default_factory=lambda: DELAYS)
    browser: BrowserSettings = field(default_factory=lambda: BROWSER)
    creds: Credentials = field(default_factory=lambda: CREDS)
    telegram: TelegramSettings = field(default_factory=lambda: TELEGRAM)
    logging_: LoggingSettings = field(default_factory=lambda: LOGGING)
    combat: CombatSettings = field(default_factory=lambda: COMBAT)
    timers: TimersSettings = field(default_factory=lambda: TIMERS)
    modules: ModuleToggles = field(default_factory=lambda: MODULES)

    log_dir: Path = LOG_DIR
    sessions_dir: Path = SESSIONS_DIR
    cookies_dir: Path = COOKIES_DIR
    storage_state_dir: Path = STORAGE_STATE_DIR
    screenshots_dir: Path = SCREENSHOTS_DIR


CONFIG: Final[AppConfig] = AppConfig()


__all__ = [
    "URLS",
    "SELECTORS",
    "DELAYS",
    "BROWSER",
    "CREDS",
    "TELEGRAM",
    "LOGGING",
    "COMBAT",
    "TIMERS",
    "MODULES",
    "CONFIG",
    "AppConfig",
    "GameURLs",
    "Selectors",
    "DelayProfile",
    "BrowserSettings",
    "Credentials",
    "TelegramSettings",
    "LoggingSettings",
    "CombatSettings",
    "TimersSettings",
    "ModuleToggles",
    "BASE_DIR",
    "PROJECT_ROOT",
    "LOG_DIR",
    "SESSIONS_DIR",
    "COOKIES_DIR",
    "STORAGE_STATE_DIR",
    "SCREENSHOTS_DIR",
]
