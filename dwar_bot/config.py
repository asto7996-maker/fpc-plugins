"""Центральная конфигурация бота для игры «Легенда: Наследие Драконов» (Dwar).

Здесь собраны:
    * учётные данные и параметры окружения (читаются из ``.env`` / переменных среды);
    * CSS/XPath-селекторы игрового интерфейса (вынесены отдельно, чтобы их можно
      было править без изменения логики модулей);
    * диапазоны случайных задержек для имитации человеческого поведения;
    * тайминги калдаунов и системные параметры.

Все значения, которые могут меняться на стороне игры (селекторы, URL, тайминги),
намеренно вынесены сюда. Модули НЕ должны хардкодить селекторы.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Tuple


# --------------------------------------------------------------------------- #
#  Загрузка переменных окружения из .env (без обязательной зависимости)        #
# --------------------------------------------------------------------------- #
def _load_dotenv(path: Path) -> None:
    """Минималистичный загрузчик ``.env``.

    Используется, если пакет ``python-dotenv`` недоступен. Не переопределяет
    уже установленные переменные окружения, поддерживает комментарии и кавычки.
    """
    if not path.exists():
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
        # Проблемы чтения .env не должны валить запуск — просто игнорируем.
        pass


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR

# Пытаемся использовать python-dotenv, иначе — собственный загрузчик.
_ENV_FILE = BASE_DIR / ".env"
try:  # pragma: no cover - зависит от окружения
    from dotenv import load_dotenv as _dotenv_load

    _dotenv_load(_ENV_FILE)
except Exception:  # noqa: BLE001 - fallback всегда должен отработать
    _load_dotenv(_ENV_FILE)


# --------------------------------------------------------------------------- #
#  Утилиты чтения переменных окружения с приведением типов                      #
# --------------------------------------------------------------------------- #
def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
#  Основные параметры игры и окружения                                         #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GameConfig:
    """Базовые адреса и точки входа игры."""

    base_url: str = _env_str("DWAR_BASE_URL", "https://www.dwar.ru")
    # Основная «главная» страница персонажа после входа.
    main_path: str = _env_str("DWAR_MAIN_PATH", "/main.php")
    # Страница профиля/инвентаря.
    inventory_path: str = _env_str("DWAR_INVENTORY_PATH", "/inventory.php")
    # Страница карты/локаций.
    map_path: str = _env_str("DWAR_MAP_PATH", "/map.php")
    # Куки/строка, по наличию которой определяем, что мы залогинены.
    auth_cookie_names: Tuple[str, ...] = ("dwar_sid", "PHPSESSID", "sid")

    @property
    def main_url(self) -> str:
        return self.base_url.rstrip("/") + self.main_path

    @property
    def inventory_url(self) -> str:
        return self.base_url.rstrip("/") + self.inventory_path

    @property
    def map_url(self) -> str:
        return self.base_url.rstrip("/") + self.map_path


@dataclass(frozen=True)
class BrowserConfig:
    """Параметры запуска Playwright."""

    engine: str = _env_str("DWAR_BROWSER_ENGINE", "chromium")  # chromium|firefox|webkit
    headless: bool = _env_bool("DWAR_HEADLESS", True)
    slow_mo_ms: int = _env_int("DWAR_SLOW_MO_MS", 0)
    # User-Agent. Пустая строка -> используется дефолтный UA Playwright.
    user_agent: str = _env_str(
        "DWAR_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    locale: str = _env_str("DWAR_LOCALE", "ru-RU")
    timezone_id: str = _env_str("DWAR_TIMEZONE", "Europe/Moscow")
    viewport_width: int = _env_int("DWAR_VIEWPORT_W", 1366)
    viewport_height: int = _env_int("DWAR_VIEWPORT_H", 768)
    # Общий таймаут ожидания элементов (мс).
    default_timeout_ms: int = _env_int("DWAR_DEFAULT_TIMEOUT_MS", 20000)
    navigation_timeout_ms: int = _env_int("DWAR_NAV_TIMEOUT_MS", 30000)
    # Прокси в формате http://user:pass@host:port (опционально).
    proxy: str = _env_str("DWAR_PROXY", "")
    # Путь для сохранения persistent-профиля (пусто -> временный контекст).
    user_data_dir: str = _env_str("DWAR_USER_DATA_DIR", "")


@dataclass(frozen=True)
class TelegramConfig:
    """Настройки уведомлений в Telegram."""

    enabled: bool = _env_bool("DWAR_TG_ENABLED", False)
    bot_token: str = _env_str("DWAR_TG_TOKEN", "")
    chat_id: str = _env_str("DWAR_TG_CHAT_ID", "")
    # Минимальный уровень, при котором отправлять в Telegram.
    min_level: str = _env_str("DWAR_TG_MIN_LEVEL", "WARNING").upper()


@dataclass(frozen=True)
class DelayConfig:
    """Диапазоны случайных задержек (сек) для human-like поведения."""

    # Микропауза между простыми действиями (клики, чтение поля).
    action_min: float = _env_float("DWAR_DELAY_ACTION_MIN", 0.4)
    action_max: float = _env_float("DWAR_DELAY_ACTION_MAX", 1.4)
    # Пауза между «раундами» основного цикла.
    loop_min: float = _env_float("DWAR_DELAY_LOOP_MIN", 8.0)
    loop_max: float = _env_float("DWAR_DELAY_LOOP_MAX", 20.0)
    # Пауза «чтения страницы» после навигации.
    page_read_min: float = _env_float("DWAR_DELAY_READ_MIN", 1.2)
    page_read_max: float = _env_float("DWAR_DELAY_READ_MAX", 3.5)
    # Задержка между нажатиями клавиш при наборе текста.
    typing_min: float = _env_float("DWAR_DELAY_TYPE_MIN", 0.05)
    typing_max: float = _env_float("DWAR_DELAY_TYPE_MAX", 0.22)
    # Периодические «длинные» перерывы (имитация отхода от ПК).
    long_break_chance: float = _env_float("DWAR_LONG_BREAK_CHANCE", 0.05)
    long_break_min: float = _env_float("DWAR_LONG_BREAK_MIN", 60.0)
    long_break_max: float = _env_float("DWAR_LONG_BREAK_MAX", 300.0)


@dataclass(frozen=True)
class CombatConfig:
    """Параметры боевого движка."""

    # Порог здоровья (в %), ниже которого используем лечение/эликсир.
    heal_hp_percent: int = _env_int("DWAR_HEAL_HP_PERCENT", 45)
    # Порог маны (в %), ниже которого не пытаемся кастовать.
    min_mana_percent: int = _env_int("DWAR_MIN_MANA_PERCENT", 20)
    # Максимум раундов боя, после которых считаем бой зависшим.
    max_rounds: int = _env_int("DWAR_MAX_ROUNDS", 60)
    # Приоритетные зоны удара (порядок попыток).
    attack_zones: Tuple[str, ...] = ("head", "chest", "belly", "legs")
    # Приоритетные зоны блока.
    block_zones: Tuple[str, ...] = ("head", "chest")
    # Использовать ли автокаст боевых заклинаний.
    use_spells: bool = _env_bool("DWAR_USE_SPELLS", True)
    # Использовать ли боевые эликсиры.
    use_elixirs: bool = _env_bool("DWAR_USE_ELIXIRS", True)


@dataclass(frozen=True)
class RuntimeConfig:
    """Системные параметры рантайма."""

    # Каталог с файлами куки-сессий.
    cookies_dir: Path = Path(_env_str("DWAR_COOKIES_DIR", str(BASE_DIR / "sessions")))
    # Явный путь к одному файлу куки (приоритетнее, чем каталог).
    cookies_file: str = _env_str("DWAR_COOKIES_FILE", "")
    # Каталог логов.
    logs_dir: Path = Path(_env_str("DWAR_LOGS_DIR", str(BASE_DIR / "logs")))
    log_file: str = _env_str("DWAR_LOG_FILE", "bot.log")
    log_level: str = _env_str("DWAR_LOG_LEVEL", "INFO").upper()
    # Каталог для скриншотов при ошибках.
    screenshots_dir: Path = Path(
        _env_str("DWAR_SCREENSHOTS_DIR", str(BASE_DIR / "screenshots"))
    )
    # Максимальное число повторных попыток при сетевых сбоях.
    max_retries: int = _env_int("DWAR_MAX_RETRIES", 3)
    # Базовая задержка backoff между повторами (сек).
    retry_backoff_base: float = _env_float("DWAR_RETRY_BACKOFF", 4.0)
    # Делать ли скриншот при исключениях.
    screenshot_on_error: bool = _env_bool("DWAR_SCREENSHOT_ON_ERROR", True)

    def ensure_dirs(self) -> None:
        """Создаёт рабочие каталоги, если их ещё нет."""
        for directory in (
            self.cookies_dir,
            self.logs_dir,
            self.screenshots_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
#  Селекторы игрового интерфейса                                               #
#  ВНИМАНИЕ: точные селекторы зависят от вёрстки игры и могут требовать         #
#  подгонки. Все они собраны здесь, чтобы правки не затрагивали логику.        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Selectors:
    """CSS-селекторы. Поддерживается несколько вариантов через запятую."""

    # --- Проверка авторизации / выход --- #
    logged_in_marker: str = "#char_stats, .char-info, a[href*='logout']"
    login_form: str = "form[action*='login'], #login_form"
    logout_link: str = "a[href*='logout.php'], a[href*='logout']"

    # --- Профиль / статы --- #
    profile_name: str = "#char_name, .char-name, .nick"
    profile_level: str = "#char_level, .char-level, .level"
    hp_value: str = "#hp, .hp-value, .health"
    hp_bar: str = "#hp_bar, .hp-bar"
    mp_value: str = "#mp, .mp-value, .mana"
    mp_bar: str = "#mp_bar, .mp-bar"
    experience: str = "#exp, .exp-value, .experience"
    money_gold: str = "#gold, .gold, .money-gold"
    money_silver: str = "#silver, .silver"

    # --- Рюкзак / инвентарь --- #
    inventory_container: str = "#inventory, .backpack, .inv-grid"
    inventory_item: str = ".inv-item, .item-cell, td.item"
    item_name_attr: str = "title"  # атрибут, где лежит имя предмета
    item_count: str = ".item-count, .count"

    # --- Уведомления / системные сообщения --- #
    notifications_container: str = "#notifications, .notify-list, .messages"
    notification_item: str = ".notify-item, .message, li"

    # --- Бой --- #
    combat_container: str = "#fight, .battle, #battle"
    combat_log: str = "#fight_log, .battle-log, #battle_log"
    combat_log_row: str = ".log-row, .battle-log__row, p"
    attack_zone_prefix: str = "input[name='attack']"  # + [value='...']
    block_zone_prefix: str = "input[name='block']"  # + [value='...']
    attack_submit: str = "input[type='submit'][value*='Атаковать'], #attack_btn, button.attack"
    enemy_hp: str = "#enemy_hp, .enemy .hp-value"
    self_hp_in_combat: str = "#my_hp, .self .hp-value"
    spell_button_prefix: str = "a.spell, button.spell"  # уточняется data-id
    elixir_button_prefix: str = "a.elixir, button.elixir"
    combat_result_win: str = ".battle-win, #win, .victory"
    combat_result_lose: str = ".battle-lose, #lose, .defeat"

    # --- Квесты / диалоги NPC --- #
    dialog_container: str = "#npc_dialog, .dialog, .npc-talk"
    dialog_text: str = ".dialog-text, .npc-text, p"
    dialog_option: str = ".dialog-option a, .npc-answer a, a.reply"
    quest_accept: str = "a[href*='accept'], button.accept-quest"
    quest_complete: str = "a[href*='complete'], button.complete-quest"

    # --- Таймеры / профессии / энергия --- #
    energy_value: str = "#energy, .energy-value"
    timer_container: str = ".timer, .cooldown, [data-timer]"
    timer_label_attr: str = "data-name"
    timer_seconds_attr: str = "data-seconds"


# --------------------------------------------------------------------------- #
#  Тайминги калдаунов (в секундах). Используются timers_manager.               #
# --------------------------------------------------------------------------- #
DEFAULT_COOLDOWNS: Dict[str, float] = {
    "energy_regen": _env_float("DWAR_CD_ENERGY", 300.0),
    "profession": _env_float("DWAR_CD_PROFESSION", 900.0),
    "combat": _env_float("DWAR_CD_COMBAT", 30.0),
    "quest_poll": _env_float("DWAR_CD_QUEST", 120.0),
    "stats_refresh": _env_float("DWAR_CD_STATS", 60.0),
}


# --------------------------------------------------------------------------- #
#  Единая точка доступа к конфигурации                                         #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    """Агрегатор всех разделов конфигурации."""

    game: GameConfig = field(default_factory=GameConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    delays: DelayConfig = field(default_factory=DelayConfig)
    combat: CombatConfig = field(default_factory=CombatConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    selectors: Selectors = field(default_factory=Selectors)
    cooldowns: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_COOLDOWNS))


# Единственный разделяемый экземпляр конфигурации.
CONFIG = Config()
CONFIG.runtime.ensure_dirs()


__all__ = [
    "CONFIG",
    "Config",
    "GameConfig",
    "BrowserConfig",
    "TelegramConfig",
    "DelayConfig",
    "CombatConfig",
    "RuntimeConfig",
    "Selectors",
    "DEFAULT_COOLDOWNS",
]
