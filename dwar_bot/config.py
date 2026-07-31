"""
Конфигурация бота «Легенда: Наследие Драконов».

Значения по умолчанию можно переопределять через переменные окружения с префиксом DWAR_.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Mapping, Sequence, Tuple

# Корневая директория проекта (dwar_bot/)
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
COOKIES_DIR: Final[Path] = DATA_DIR / "cookies"
LOGS_DIR: Final[Path] = DATA_DIR / "logs"

DEFAULT_LOG_FILE: Final[Path] = LOGS_DIR / "bot.log"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return int(raw)


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if raw is None:
        return default
    return Path(raw).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class DelayConfig:
    """Диапазоны случайных задержек (секунды) для human-like поведения."""

    click_min: float = 0.35
    click_max: float = 1.2
    navigation_min: float = 1.5
    navigation_max: float = 4.0
    action_min: float = 0.8
    action_max: float = 2.5
    combat_min: float = 0.5
    combat_max: float = 1.8
    typing_min: float = 0.04
    typing_max: float = 0.14
    idle_min: float = 2.0
    idle_max: float = 6.0
    session_rotate_min: float = 300.0
    session_rotate_max: float = 900.0

    def __post_init__(self) -> None:
        for pair_name, min_val, max_val in (
            ("click", self.click_min, self.click_max),
            ("navigation", self.navigation_min, self.navigation_max),
            ("action", self.action_min, self.action_max),
            ("combat", self.combat_min, self.combat_max),
            ("typing", self.typing_min, self.typing_max),
            ("idle", self.idle_min, self.idle_max),
            ("session_rotate", self.session_rotate_min, self.session_rotate_max),
        ):
            if min_val < 0 or max_val < 0:
                raise ValueError(f"Задержки {pair_name} не могут быть отрицательными")
            if min_val > max_val:
                raise ValueError(
                    f"Минимальная задержка {pair_name} ({min_val}) больше максимальной ({max_val})"
                )


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    """Настройки Playwright."""

    headless: bool = True
    slow_mo_ms: int = 0
    viewport_width: int = 1366
    viewport_height: int = 768
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    locale: str = "ru-RU"
    timezone_id: str = "Europe/Moscow"
    default_timeout_ms: int = 30_000
    navigation_timeout_ms: int = 60_000
    ignore_https_errors: bool = False
    # Dwar использует Flash/HTML-фреймы; иногда нужен разрешённый autoplay
    permissions: Tuple[str, ...] = ("geolocation",)

    def __post_init__(self) -> None:
        if self.viewport_width < 320 or self.viewport_height < 240:
            raise ValueError("Некорректный размер viewport")
        if self.default_timeout_ms <= 0 or self.navigation_timeout_ms <= 0:
            raise ValueError("Таймауты браузера должны быть положительными")


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Опциональные уведомления в Telegram."""

    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    notify_on_error: bool = True
    notify_on_session_rotate: bool = True
    notify_on_auth_failure: bool = True

    def __post_init__(self) -> None:
        if self.enabled and (not self.bot_token or not self.chat_id):
            raise ValueError(
                "Для Telegram-уведомлений задайте DWAR_TELEGRAM_BOT_TOKEN и DWAR_TELEGRAM_CHAT_ID"
            )


@dataclass(frozen=True, slots=True)
class CookieConfig:
    """Параметры загрузки и валидации cookie-сессий."""

    cookies_dir: Path = COOKIES_DIR
    # Имена файлов сессий (JSON Cookie Editor или Netscape .txt)
    session_files: Tuple[str, ...] = ("session_primary.json", "session_backup.json")
    # Обязательные cookie для авторизованной сессии dwar.ru
    required_cookie_names: Tuple[str, ...] = ("PHPSESSID",)
    # Дополнительные cookie, повышающие доверие к сессии (не обязательны)
    preferred_cookie_names: Tuple[str, ...] = ("dwar_id", "uid", "sid", "remember")
    allowed_domains: Tuple[str, ...] = (".dwar.ru", "dwar.ru", "w1.dwar.ru", "w2.dwar.ru")
    validation_url_path: str = "/"
    validation_timeout_sec: float = 20.0
    validation_max_redirects: int = 8
    # Считаем cookie просроченным за N секунд до фактического expires
    expiry_skew_sec: int = 120
    rotate_on_validation_failure: bool = True
    persist_playwright_state: bool = True
    playwright_state_file: Path = DATA_DIR / "browser_state.json"

    def __post_init__(self) -> None:
        if not self.required_cookie_names:
            raise ValueError("Список required_cookie_names не может быть пустым")
        if self.validation_timeout_sec <= 0:
            raise ValueError("validation_timeout_sec должен быть положительным")


@dataclass(frozen=True, slots=True)
class GameServerConfig:
    """Игровые сервера и базовые URL."""

    server: str = "w2"  # w1 (Прайм) или w2 (Минор)
    base_url: str = "https://w2.dwar.ru"
    game_entry_url: str = "https://w2.dwar.ru/game.php"
    profile_url: str = "https://w2.dwar.ru/user.php"
    login_url: str = "https://w2.dwar.ru/"
    lang: str = "ru_RU"

    def __post_init__(self) -> None:
        if self.server not in {"w1", "w2"}:
            raise ValueError("server должен быть 'w1' или 'w2'")

    @classmethod
    def for_server(cls, server: str, lang: str = "ru_RU") -> "GameServerConfig":
        server = server.strip().lower()
        if server not in {"w1", "w2"}:
            raise ValueError("server должен быть 'w1' или 'w2'")
        base = f"https://{server}.dwar.ru"
        return cls(
            server=server,
            base_url=base,
            game_entry_url=f"{base}/game.php",
            profile_url=f"{base}/user.php",
            login_url=f"{base}/",
            lang=lang,
        )


@dataclass(frozen=True, slots=True)
class Selectors:
    """
    CSS/XPath селекторы интерфейса Dwar.

    Игра использует фреймы и динамическую разметку; селекторы вынесены в конфиг
    для быстрой адаптации без правки бизнес-логики модулей.
    """

    # Авторизация / лендинг
    login_form: str = "form[action*='login'], #login_form, .login-form"
    login_username: str = "input[name='username'], input[name='login'], #login"
    login_password: str = "input[name='password'], input[type='password'], #password"
    login_submit: str = "button[type='submit'], input[type='submit'], .btn-login"
    logged_in_marker: str = (
        "#game_container, .game-frame, iframe[src*='game'], "
        "a[href*='logout'], .user-nick, #user_name"
    )
    logout_link: str = "a[href*='logout'], .logout-link"

    # Игровой фрейм
    game_iframe: str = "iframe#game_frame, iframe[src*='game'], iframe.game-frame"
    main_frame: str = "frame[name='main'], #main_frame"

    # Профиль / статы
    profile_nickname: str = ".nick, .user-nick, #nick, [data-role='nickname']"
    profile_level: str = ".level, #level, [data-role='level']"
    profile_hp: str = ".hp, #hp, [data-stat='hp']"
    profile_mp: str = ".mp, #mp, [data-stat='mp']"
    profile_energy: str = ".energy, #energy, [data-stat='energy']"
    profile_gold: str = ".gold, .money, #gold, [data-currency='gold']"
    profile_silver: str = ".silver, #silver, [data-currency='silver']"
    profile_brass: str = ".brass, #brass, [data-currency='brass']"

    # Рюкзак
    inventory_panel: str = "#inventory, .inventory, [data-panel='inventory']"
    inventory_item: str = ".inv-item, .inventory-item, [data-item-id]"
    inventory_item_name: str = ".item-name, .inv-item-name"
    inventory_item_count: str = ".item-count, .inv-item-count"

    # Бой
    combat_panel: str = "#fight, .combat, [data-panel='combat']"
    combat_log: str = "#fight_log, .fight-log, .combat-log"
    combat_attack_buttons: str = (
        ".attack-btn, button[data-action='attack'], .fight-actions button"
    )
    combat_elixir_slot: str = ".elixir-slot, [data-slot='elixir']"
    combat_spell_slot: str = ".spell-slot, [data-slot='spell']"
    combat_turn_indicator: str = ".turn-indicator, .your-turn, [data-turn='player']"

    # Квесты / NPC
    quest_panel: str = "#quests, .quest-panel, [data-panel='quests']"
    quest_active: str = ".quest-active, .quest-item.active"
    npc_dialog: str = ".npc-dialog, .dialog-window, #dialog"
    npc_dialog_text: str = ".dialog-text, .npc-text"
    npc_dialog_choices: str = ".dialog-choice, .npc-choice, .dialog-options button"
    npc_dialog_continue: str = ".dialog-continue, button[data-action='continue']"

    # Уведомления / таймеры
    notification_popup: str = ".notification, .popup-notice, .alert"
    notification_close: str = ".notification .close, .popup-close, button.close"
    cooldown_timer: str = ".cooldown, .timer, [data-cooldown]"
    profession_timer: str = ".profession-timer, [data-profession-timer]"

    # Общие UI-элементы
    modal_overlay: str = ".modal, .overlay, .popup-overlay"
    modal_close: str = ".modal .close, .popup-close"
    loading_indicator: str = ".loading, .spinner, #loader"
    error_message: str = ".error, .alert-danger, .message-error"


@dataclass(frozen=True, slots=True)
class CredentialsConfig:
    """
    Учётные данные (опционально, если cookie недоступны).

    Рекомендуется хранить в переменных окружения, а не в коде.
    """

    username: str = ""
    password: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Настройки логирования."""

    log_file: Path = DEFAULT_LOG_FILE
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5
    console: bool = True
    json_errors: bool = False


@dataclass(frozen=True, slots=True)
class BotConfig:
    """Сводная конфигурация бота."""

    server: GameServerConfig = field(default_factory=GameServerConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    delays: DelayConfig = field(default_factory=DelayConfig)
    cookies: CookieConfig = field(default_factory=CookieConfig)
    selectors: Selectors = field(default_factory=Selectors)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    loop_interval_sec: float = 5.0
    max_consecutive_errors: int = 10
    graceful_shutdown_timeout_sec: float = 30.0

    def ensure_directories(self) -> None:
        """Создаёт рабочие директории, если их нет."""
        for path in (
            DATA_DIR,
            self.cookies.cookies_dir,
            LOGS_DIR,
            self.logging.log_file.parent,
            self.cookies.playwright_state_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


# Маркеры неавторизованной страницы (нижний регистр для поиска в HTML)
AUTH_FAILURE_MARKERS: Final[Tuple[str, ...]] = (
    "войти",
    "авториза",
    "login",
    "sign in",
    "неверный пароль",
    "session expired",
    "сессия истекла",
)

AUTH_SUCCESS_MARKERS: Final[Tuple[str, ...]] = (
    "game.php",
    "играть",
    "выйти",
    "logout",
    "user.php",
    "персонаж",
)


def load_config() -> BotConfig:
    """
    Загружает конфигурацию из переменных окружения.

    Переменные:
        DWAR_SERVER              — w1 или w2 (по умолчанию w2)
        DWAR_HEADLESS            — true/false
        DWAR_COOKIES_DIR         — путь к каталогу с cookie-файлами
        DWAR_SESSION_FILES       — имена файлов через запятую
        DWAR_LOG_LEVEL           — DEBUG/INFO/WARNING/ERROR
        DWAR_LOG_FILE            — путь к bot.log
        DWAR_TELEGRAM_ENABLED    — true/false
        DWAR_TELEGRAM_BOT_TOKEN
        DWAR_TELEGRAM_CHAT_ID
        DWAR_USERNAME / DWAR_PASSWORD — fallback-логин
    """
    server_name = os.getenv("DWAR_SERVER", "w2").strip().lower()
    server = GameServerConfig.for_server(server_name)

    session_files_raw = os.getenv("DWAR_SESSION_FILES", "")
    session_files: Tuple[str, ...]
    if session_files_raw.strip():
        session_files = tuple(
            part.strip() for part in session_files_raw.split(",") if part.strip()
        )
    else:
        session_files = CookieConfig().session_files

    cookies_dir = _env_path("DWAR_COOKIES_DIR", COOKIES_DIR)
    log_file = _env_path("DWAR_LOG_FILE", DEFAULT_LOG_FILE)

    config = BotConfig(
        server=server,
        browser=BrowserConfig(
            headless=_env_bool("DWAR_HEADLESS", True),
            slow_mo_ms=_env_int("DWAR_SLOW_MO_MS", 0),
            default_timeout_ms=_env_int("DWAR_DEFAULT_TIMEOUT_MS", 30_000),
            navigation_timeout_ms=_env_int("DWAR_NAVIGATION_TIMEOUT_MS", 60_000),
        ),
        delays=DelayConfig(
            click_min=_env_float("DWAR_DELAY_CLICK_MIN", 0.35),
            click_max=_env_float("DWAR_DELAY_CLICK_MAX", 1.2),
            navigation_min=_env_float("DWAR_DELAY_NAV_MIN", 1.5),
            navigation_max=_env_float("DWAR_DELAY_NAV_MAX", 4.0),
            action_min=_env_float("DWAR_DELAY_ACTION_MIN", 0.8),
            action_max=_env_float("DWAR_DELAY_ACTION_MAX", 2.5),
        ),
        cookies=CookieConfig(
            cookies_dir=cookies_dir,
            session_files=session_files,
            validation_timeout_sec=_env_float("DWAR_COOKIE_VALIDATION_TIMEOUT", 20.0),
            rotate_on_validation_failure=_env_bool("DWAR_COOKIE_ROTATE_ON_FAIL", True),
            playwright_state_file=_env_path(
                "DWAR_PLAYWRIGHT_STATE_FILE", DATA_DIR / "browser_state.json"
            ),
        ),
        credentials=CredentialsConfig(
            username=os.getenv("DWAR_USERNAME", ""),
            password=os.getenv("DWAR_PASSWORD", ""),
        ),
        logging=LoggingConfig(
            log_file=log_file,
            level=os.getenv("DWAR_LOG_LEVEL", "INFO").upper(),
            console=_env_bool("DWAR_LOG_CONSOLE", True),
        ),
        telegram=TelegramConfig(
            enabled=_env_bool("DWAR_TELEGRAM_ENABLED", False),
            bot_token=os.getenv("DWAR_TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("DWAR_TELEGRAM_CHAT_ID", ""),
        ),
        loop_interval_sec=_env_float("DWAR_LOOP_INTERVAL_SEC", 5.0),
        max_consecutive_errors=_env_int("DWAR_MAX_CONSECUTIVE_ERRORS", 10),
    )
    config.ensure_directories()
    return config


# Singleton для импорта: from dwar_bot.config import config
config: BotConfig = load_config()


def get_delay_range(kind: str) -> Tuple[float, float]:
    """Возвращает (min, max) для типа задержки."""
    mapping: Mapping[str, Tuple[float, float]] = {
        "click": (config.delays.click_min, config.delays.click_max),
        "navigation": (config.delays.navigation_min, config.delays.navigation_max),
        "action": (config.delays.action_min, config.delays.action_max),
        "combat": (config.delays.combat_min, config.delays.combat_max),
        "typing": (config.delays.typing_min, config.delays.typing_max),
        "idle": (config.delays.idle_min, config.delays.idle_max),
        "session_rotate": (
            config.delays.session_rotate_min,
            config.delays.session_rotate_max,
        ),
    }
    if kind not in mapping:
        raise KeyError(f"Неизвестный тип задержки: {kind}")
    return mapping[kind]


def get_selector_groups() -> Mapping[str, Sequence[str]]:
    """Группы селекторов для модулей (удобно для wait_for_selector с fallback)."""
    s = config.selectors
    return {
        "auth_logged_in": (s.logged_in_marker,),
        "profile_stats": (
            s.profile_nickname,
            s.profile_level,
            s.profile_hp,
            s.profile_mp,
            s.profile_energy,
        ),
        "currencies": (s.profile_gold, s.profile_silver, s.profile_brass),
        "combat": (s.combat_panel, s.combat_log, s.combat_attack_buttons),
        "quests": (s.quest_panel, s.npc_dialog, s.npc_dialog_choices),
        "timers": (s.cooldown_timer, s.profession_timer),
    }
