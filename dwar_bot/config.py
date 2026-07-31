"""
config.py
=========

Централизованная конфигурация бота для игры "Легенда: Наследие Драконов" (Dwar).

Здесь собрано всё, что может меняться между окружениями и запусками:

* базовые URL и эндпоинты игры;
* CSS/XPath-селекторы игрового интерфейса;
* диапазоны случайных задержек (human-like behavior);
* параметры браузера и анти-бот слоя;
* пути к cookie-файлам и настройки ротации сессий;
* конфигурация логирования и Telegram-уведомлений.

Значения по умолчанию заданы прямо в коде, но КАЖДОЕ из них может быть
переопределено переменной окружения (или файлом ``.env`` в корне проекта,
если установлен ``python-dotenv``). Это позволяет хранить креды вне репозитория.

Пример ``.env``::

    DWAR_BASE_URL=https://www.legendofdragons.ru
    DWAR_LOGIN=my_login
    DWAR_PASSWORD=super_secret
    DWAR_TELEGRAM_TOKEN=123456:AA...
    DWAR_TELEGRAM_CHAT_ID=987654321
    DWAR_HEADLESS=1
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Необязательная поддержка .env через python-dotenv.
# Пакет не является жёсткой зависимостью: при его отсутствии просто читаем
# переменные окружения как есть.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - тривиальная ветка импорта
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 - python-dotenv не установлен, это допустимо
    pass


# ---------------------------------------------------------------------------
# Базовые пути проекта.
# ---------------------------------------------------------------------------
BASE_DIR: Final[Path] = Path(__file__).resolve().parent
COOKIES_DIR: Final[Path] = BASE_DIR / "cookies"
LOGS_DIR: Final[Path] = BASE_DIR / "logs"
STATE_DIR: Final[Path] = BASE_DIR / "state"

for _directory in (COOKIES_DIR, LOGS_DIR, STATE_DIR):
    _directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Хелперы чтения переменных окружения с приведением типов.
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str) -> str:
    """Строковая переменная окружения с дефолтом."""
    value = os.getenv(name)
    return value if value is not None and value != "" else default


def _env_int(name: str, default: int) -> int:
    """Целочисленная переменная окружения с безопасным фоллбэком."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Вещественная переменная окружения с безопасным фоллбэком."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Булева переменная окружения ('1/true/yes/on' → True)."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


# ===========================================================================
# СЕТЕВАЯ / ИГРОВАЯ КОНФИГУРАЦИЯ
# ===========================================================================
@dataclass(frozen=True, slots=True)
class GameConfig:
    """Базовые адреса и маршруты игрового сервера."""

    base_url: str = _env_str("DWAR_BASE_URL", "https://www.legendofdragons.ru")
    # Домен, которому должны принадлежать валидные cookie.
    cookie_domain: str = _env_str("DWAR_COOKIE_DOMAIN", ".legendofdragons.ru")

    # Ключевые страницы игрового клиента.
    path_main: str = _env_str("DWAR_PATH_MAIN", "/main.php")
    path_login: str = _env_str("DWAR_PATH_LOGIN", "/index.php")
    path_map: str = _env_str("DWAR_PATH_MAP", "/map.php")
    path_inventory: str = _env_str("DWAR_PATH_INVENTORY", "/inventory.php")
    path_profile: str = _env_str("DWAR_PATH_PROFILE", "/user.php")
    path_battle: str = _env_str("DWAR_PATH_BATTLE", "/battle.php")
    path_quest: str = _env_str("DWAR_PATH_QUEST", "/quest.php")

    def url(self, path: str) -> str:
        """Собрать абсолютный URL из базового адреса и относительного пути."""
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    @property
    def main_url(self) -> str:
        return self.url(self.path_main)

    @property
    def login_url(self) -> str:
        return self.url(self.path_login)

    @property
    def map_url(self) -> str:
        return self.url(self.path_map)

    @property
    def inventory_url(self) -> str:
        return self.url(self.path_inventory)

    @property
    def profile_url(self) -> str:
        return self.url(self.path_profile)

    @property
    def battle_url(self) -> str:
        return self.url(self.path_battle)

    @property
    def quest_url(self) -> str:
        return self.url(self.path_quest)


# ===========================================================================
# УЧЁТНЫЕ ДАННЫЕ
# ===========================================================================
@dataclass(frozen=True, slots=True)
class Credentials:
    """
    Учётные данные для входа.

    Для Dwar основным способом авторизации бота являются cookie
    (см. :mod:`dwar_bot.auth.cookie_manager`). Логин/пароль хранятся на случай
    необходимости повторной авторизации через форму входа.
    """

    login: str = _env_str("DWAR_LOGIN", "")
    password: str = _env_str("DWAR_PASSWORD", "")

    @property
    def has_form_credentials(self) -> bool:
        """Есть ли логин и пароль для входа через форму."""
        return bool(self.login and self.password)


# ===========================================================================
# COOKIE / СЕССИИ
# ===========================================================================
@dataclass(frozen=True, slots=True)
class CookieConfig:
    """Настройки хранения, валидации и ротации cookie-сессий."""

    # Директория, где лежат экспортированные cookie (Cookie-Editor JSON /
    # Netscape cookies.txt). Каждый файл = отдельная сессия/аккаунт.
    directory: Path = COOKIES_DIR

    # Cookie, без которых сессия считается невалидной. Названия специфичны для
    # игрового движка; переопределяются переменной окружения
    # DWAR_REQUIRED_COOKIES="a,b,c".
    required_cookies: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            name.strip()
            for name in _env_str("DWAR_REQUIRED_COOKIES", "PHPSESSID,dwar_uid").split(",")
            if name.strip()
        )
    )

    # Запас по времени (в секундах): cookie, истекающая раньше, чем через
    # это количество секунд, считается "почти протухшей" и в ротацию не берётся.
    expiry_leeway_seconds: int = _env_int("DWAR_COOKIE_EXPIRY_LEEWAY", 300)

    # Разрешить ли автоматическую ротацию на следующий валидный профиль при
    # разлогине / бане текущей сессии.
    rotation_enabled: bool = _env_bool("DWAR_COOKIE_ROTATION", True)

    # Маска файлов cookie в директории.
    file_glob: str = _env_str("DWAR_COOKIE_GLOB", "*.json,*.txt")

    @property
    def file_patterns(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.file_glob.split(",") if p.strip())


# ===========================================================================
# ЗАДЕРЖКИ (HUMAN-LIKE BEHAVIOR)
# ===========================================================================
@dataclass(frozen=True, slots=True)
class DelayConfig:
    """
    Диапазоны случайных задержек в секундах.

    Все действия бота обёрнуты в ``asyncio.sleep(random.uniform(min, max))``
    именно с этими границами, чтобы имитировать поведение живого игрока и
    затруднить детект античитом.
    """

    # Микро-пауза между отдельными кликами/вводом внутри одной страницы.
    action_min: float = _env_float("DWAR_DELAY_ACTION_MIN", 0.4)
    action_max: float = _env_float("DWAR_DELAY_ACTION_MAX", 1.6)

    # Пауза между сменой страниц / крупными действиями.
    navigation_min: float = _env_float("DWAR_DELAY_NAV_MIN", 1.5)
    navigation_max: float = _env_float("DWAR_DELAY_NAV_MAX", 4.0)

    # Пауза между полными итерациями главного цикла.
    loop_min: float = _env_float("DWAR_DELAY_LOOP_MIN", 8.0)
    loop_max: float = _env_float("DWAR_DELAY_LOOP_MAX", 20.0)

    # Пауза между ударами в бою.
    combat_min: float = _env_float("DWAR_DELAY_COMBAT_MIN", 0.8)
    combat_max: float = _env_float("DWAR_DELAY_COMBAT_MAX", 2.5)

    # "Долгий отдых" — редкая длинная пауза, имитирующая отход от компьютера.
    idle_min: float = _env_float("DWAR_DELAY_IDLE_MIN", 120.0)
    idle_max: float = _env_float("DWAR_DELAY_IDLE_MAX", 600.0)
    # Вероятность (0..1) уйти в "долгий отдых" на очередной итерации.
    idle_probability: float = _env_float("DWAR_IDLE_PROBABILITY", 0.05)

    def __post_init__(self) -> None:
        # Гарантируем, что min <= max для всех пар, иначе random.uniform
        # выбросит логическую ошибку в рантайме.
        pairs = (
            ("action", self.action_min, self.action_max),
            ("navigation", self.navigation_min, self.navigation_max),
            ("loop", self.loop_min, self.loop_max),
            ("combat", self.combat_min, self.combat_max),
            ("idle", self.idle_min, self.idle_max),
        )
        for name, low, high in pairs:
            if low < 0 or high < 0:
                raise ValueError(f"Задержка '{name}' не может быть отрицательной.")
            if low > high:
                raise ValueError(
                    f"Некорректный диапазон задержки '{name}': min={low} > max={high}."
                )


# ===========================================================================
# БРАУЗЕР / PLAYWRIGHT
# ===========================================================================
@dataclass(frozen=True, slots=True)
class BrowserConfig:
    """Параметры запуска браузера Playwright."""

    engine: str = _env_str("DWAR_BROWSER_ENGINE", "chromium")  # chromium|firefox|webkit
    headless: bool = _env_bool("DWAR_HEADLESS", True)
    slow_mo_ms: int = _env_int("DWAR_SLOW_MO_MS", 0)

    # Тайм-ауты Playwright (миллисекунды).
    default_timeout_ms: int = _env_int("DWAR_DEFAULT_TIMEOUT_MS", 15_000)
    navigation_timeout_ms: int = _env_int("DWAR_NAV_TIMEOUT_MS", 30_000)

    # Вьюпорт и локаль.
    viewport_width: int = _env_int("DWAR_VIEWPORT_WIDTH", 1366)
    viewport_height: int = _env_int("DWAR_VIEWPORT_HEIGHT", 768)
    locale: str = _env_str("DWAR_LOCALE", "ru-RU")
    timezone_id: str = _env_str("DWAR_TIMEZONE", "Europe/Moscow")

    # Пользовательский агент. Пустая строка → использовать дефолт движка.
    user_agent: str = _env_str(
        "DWAR_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )

    # Прокси в формате "http://user:pass@host:port" или "socks5://host:port".
    proxy: str = _env_str("DWAR_PROXY", "")

    # Путь для сохранения screenshot'ов при ошибках.
    screenshot_dir: Path = LOGS_DIR / "screenshots"

    def __post_init__(self) -> None:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)


# ===========================================================================
# ЛОГИРОВАНИЕ / TELEGRAM
# ===========================================================================
@dataclass(frozen=True, slots=True)
class LogConfig:
    """Настройки файлового логирования и уведомлений в Telegram."""

    level: str = _env_str("DWAR_LOG_LEVEL", "INFO")
    file: Path = LOGS_DIR / _env_str("DWAR_LOG_FILE", "bot.log")
    max_bytes: int = _env_int("DWAR_LOG_MAX_BYTES", 5 * 1024 * 1024)
    backup_count: int = _env_int("DWAR_LOG_BACKUPS", 5)

    telegram_enabled: bool = _env_bool("DWAR_TELEGRAM_ENABLED", False)
    telegram_token: str = _env_str("DWAR_TELEGRAM_TOKEN", "")
    telegram_chat_id: str = _env_str("DWAR_TELEGRAM_CHAT_ID", "")
    # Минимальный уровень события, при котором шлём в Telegram.
    telegram_level: str = _env_str("DWAR_TELEGRAM_LEVEL", "ERROR")

    @property
    def telegram_ready(self) -> bool:
        """Достаточно ли данных, чтобы реально отправлять сообщения в Telegram."""
        return bool(self.telegram_enabled and self.telegram_token and self.telegram_chat_id)


# ===========================================================================
# СЕЛЕКТОРЫ ИГРОВОГО ИНТЕРФЕЙСА
# ===========================================================================
# Селекторы вынесены в отдельный словарь, потому что вёрстка игры может
# меняться. Здесь заданы разумные значения по умолчанию для DOM Dwar; при
# необходимости их правят точечно, не трогая логику модулей.
SELECTORS: Final[dict[str, str]] = {
    # --- Авторизация / общее состояние ---
    "login_form": "form[name='login'], form#login-form",
    "login_input": "input[name='login']",
    "password_input": "input[name='password']",
    "login_submit": "button[type='submit'], input[type='submit']",
    "logged_in_marker": "#game, .game-frame, a[href*='logout']",
    "logout_link": "a[href*='logout']",
    # --- Профиль / статистика ---
    "profile_hp": ".stat-hp, #hp_value, [data-stat='hp']",
    "profile_mp": ".stat-mp, #mp_value, [data-stat='mp']",
    "profile_energy": ".stat-energy, #energy_value, [data-stat='energy']",
    "profile_level": ".stat-level, #level_value, [data-stat='level']",
    "profile_exp": ".stat-exp, #exp_value, [data-stat='exp']",
    "profile_money": ".money, #gold_value, [data-stat='gold']",
    # --- Инвентарь / рюкзак ---
    "inventory_grid": ".inventory, #backpack, .backpack-grid",
    "inventory_item": ".inventory .item, .backpack-grid .cell[data-item]",
    "inventory_item_name": ".item-name, [data-item-name]",
    "inventory_item_qty": ".item-qty, [data-item-qty]",
    # --- Бой ---
    "battle_frame": "#battle, .battle-screen",
    "battle_log": "#battle_log, .battle-log",
    "battle_enemy_hp": ".enemy .hp-bar, #enemy_hp",
    "battle_self_hp": ".player .hp-bar, #player_hp",
    "attack_head": "a[href*='attack=head'], [data-strike='head']",
    "attack_chest": "a[href*='attack=chest'], [data-strike='chest']",
    "attack_belly": "a[href*='attack=belly'], [data-strike='belly']",
    "attack_legs": "a[href*='attack=legs'], [data-strike='legs']",
    "block_head": "[data-block='head']",
    "block_chest": "[data-block='chest']",
    "block_belly": "[data-block='belly']",
    "block_legs": "[data-block='legs']",
    "attack_submit": "button[name='attack'], #attack_button",
    "use_elixir": "[data-action='use-elixir'], a[href*='use=elixir']",
    "cast_spell": "[data-action='cast'], a[href*='cast=']",
    "battle_result": ".battle-result, #battle_result",
    "battle_continue": "a[href*='continue'], .btn-continue",
    # --- Квесты / диалоги NPC ---
    "quest_dialog": ".npc-dialog, #dialog_box",
    "quest_dialog_text": ".npc-dialog .text, #dialog_text",
    "quest_option": ".npc-dialog a.option, #dialog_box .answer",
    "quest_accept": "a[href*='quest_accept'], .btn-accept",
    "quest_complete": "a[href*='quest_complete'], .btn-complete",
    # --- Таймеры / кулдауны / профессии ---
    "cooldown_timer": ".cooldown, [data-cooldown]",
    "profession_timer": ".profession-timer, [data-profession-cd]",
    "energy_timer": ".energy-timer, [data-energy-regen]",
    # --- Уведомления ---
    "notification": ".notification, #notifications .item",
    "notification_close": ".notification .close, #notifications .close",
}


# ===========================================================================
# АГРЕГИРУЮЩИЙ ОБЪЕКТ КОНФИГУРАЦИИ
# ===========================================================================
@dataclass(frozen=True, slots=True)
class Settings:
    """Единая точка доступа ко всей конфигурации бота."""

    game: GameConfig = field(default_factory=GameConfig)
    credentials: Credentials = field(default_factory=Credentials)
    cookies: CookieConfig = field(default_factory=CookieConfig)
    delays: DelayConfig = field(default_factory=DelayConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    logging: LogConfig = field(default_factory=LogConfig)
    selectors: dict[str, str] = field(default_factory=lambda: dict(SELECTORS))

    def selector(self, key: str) -> str:
        """
        Получить селектор по ключу с внятной ошибкой, если ключ отсутствует.

        Использование ``settings.selector("attack_head")`` вместо прямого
        обращения к словарю защищает от опечаток на этапе выполнения.
        """
        try:
            return self.selectors[key]
        except KeyError as exc:  # pragma: no cover - защитная ветка
            raise KeyError(
                f"Неизвестный селектор '{key}'. Доступные ключи: "
                f"{', '.join(sorted(self.selectors))}"
            ) from exc


# Глобальный singleton-экземпляр, который импортируют остальные модули:
#     from dwar_bot.config import settings
settings: Final[Settings] = Settings()


__all__ = [
    "BASE_DIR",
    "COOKIES_DIR",
    "LOGS_DIR",
    "STATE_DIR",
    "SELECTORS",
    "GameConfig",
    "Credentials",
    "CookieConfig",
    "DelayConfig",
    "BrowserConfig",
    "LogConfig",
    "Settings",
    "settings",
]
