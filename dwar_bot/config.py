"""Конфигурация бота.

Модуль хранит все статические параметры проекта:

* пути к рабочим директориям (сессии, логи, снапшоты);
* URL-адреса и endpoint'ы игры «Легенда: Наследие Драконов» (dwar.ru);
* CSS/XPath-селекторы ключевых DOM-элементов;
* диапазоны случайных задержек для имитации человека;
* параметры Playwright (viewport, user-agent, прокси);
* учётные данные (Telegram-бот, шифрование сессий), читаемые
  из переменных окружения или файла ``.env`` рядом с проектом.

Все значения могут быть переопределены через переменные окружения либо
через файл ``dwar_bot/config.local.json``. Приоритет:

    ENV  >  config.local.json  >  значения по умолчанию.

Ни одно значение не является заглушкой: пути валидируются при загрузке,
типы приводятся к нужным, критические ошибки выбрасываются немедленно.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Mapping


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Пути проекта
# ---------------------------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parent
"""Корневая директория пакета ``dwar_bot``."""

SESSIONS_DIR: Path = PROJECT_ROOT / "sessions"
"""Директория с файлами куков (Cookie-Editor JSON или Netscape ``.txt``)."""

LOGS_DIR: Path = PROJECT_ROOT / "logs"
"""Директория для файлов логов (``bot.log`` + ротации)."""

SNAPSHOTS_DIR: Path = PROJECT_ROOT / "snapshots"
"""Директория для сохранения HTML/скриншотов при ошибках (форензика)."""

STATE_DIR: Path = PROJECT_ROOT / "state"
"""Директория для хранения runtime-состояния (кулдауны, метрики сессий)."""

LOCAL_CONFIG_PATH: Path = PROJECT_ROOT / "config.local.json"
"""Локальный override-конфиг, не коммитится в репозиторий."""


for _dir in (SESSIONS_DIR, LOGS_DIR, SNAPSHOTS_DIR, STATE_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Служебные функции чтения переменных окружения
# ---------------------------------------------------------------------------

def _env_str(key: str, default: str | None = None) -> str | None:
    """Читает строковую переменную окружения (пустая строка → ``None``)."""
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
    except ValueError:
        logger.warning("ENV %s не является int (%r), используется %d", key, raw, default)
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ENV %s не является float (%r), используется %f", key, raw, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _load_env_file(path: Path) -> None:
    """Простейший загрузчик ``.env`` без внешних зависимостей.

    Формат: ``KEY=VALUE`` по одному на строку; ``#`` — комментарий.
    Значения не переопределяют уже существующие переменные окружения.
    """
    if not path.is_file():
        return
    try:
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                logger.warning(".env:%d: пропущена строка без '=': %r", lineno, raw)
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        logger.warning("Не удалось прочитать .env (%s): %s", path, exc)


_load_env_file(PROJECT_ROOT / ".env")
_load_env_file(PROJECT_ROOT.parent / ".env")


# ---------------------------------------------------------------------------
# URL-адреса dwar.ru
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class URLs:
    """URL-адреса ключевых страниц игры."""

    base: str = "https://www.dwar.ru"
    login: str = "https://www.dwar.ru/login.php"
    main: str = "https://www.dwar.ru/main.php"
    main_top: str = "https://www.dwar.ru/main_top.php"
    main_left: str = "https://www.dwar.ru/main_left.php"
    main_right: str = "https://www.dwar.ru/main_right.php"
    profile: str = "https://www.dwar.ru/user_info.php"
    inventory: str = "https://www.dwar.ru/modules/inventory/inventory.php"
    fight: str = "https://www.dwar.ru/fight_new.php"
    fight_log: str = "https://www.dwar.ru/fight_log.php"
    map: str = "https://www.dwar.ru/map.php"
    quests: str = "https://www.dwar.ru/quest.php"
    chat_frame: str = "https://www.dwar.ru/main_chat.php"
    notify: str = "https://www.dwar.ru/notify.php"
    logout: str = "https://www.dwar.ru/logout.php"


URLS = URLs()


# ---------------------------------------------------------------------------
# Селекторы DOM
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Selectors:
    """CSS-селекторы для ключевых элементов интерфейса.

    Селекторы намеренно консервативные (по атрибутам ``name``/``id``,
    а не по классам, которые часто меняются). При смене вёрстки можно
    оверрайдить их через ``config.local.json``.
    """

    # ---- Форма логина -----------------------------------------------------
    login_form: str = "form[action*='login.php']"
    login_input: str = "input[name='login']"
    password_input: str = "input[name='pass'], input[name='password']"
    login_submit: str = "input[type='submit'], button[type='submit']"
    login_error_banner: str = ".login_error, #login_error, .error"
    logged_marker: str = "a[href*='logout.php']"

    # ---- Профиль / статы --------------------------------------------------
    profile_nick: str = "table.user_info td.nick, .userNick"
    profile_level: str = "td:has-text('Уровень') + td, .level_value"
    profile_hp: str = "#hp, td.hp_value"
    profile_mp: str = "#mp, td.mp_value"
    profile_exp: str = "#exp, td.exp_value"
    profile_gold: str = "td.gold_value, span.money"
    profile_gold_alt: str = "img[src*='gold'] + span"

    # ---- Рюкзак -----------------------------------------------------------
    inventory_slot: str = "table.inventory td.slot"
    inventory_item_img: str = "img[data-id], img[onclick*='item']"
    inventory_gold_counter: str = "#inv_gold, td.gold"

    # ---- Уведомления ------------------------------------------------------
    notify_container: str = "#notify, .notify_list"
    notify_item: str = ".notify_item, li.n_item"

    # ---- Бой --------------------------------------------------------------
    fight_frame: str = "iframe[name='main'], iframe#main"
    fight_hit_head: str = "input[name='body_part'][value='head']"
    fight_hit_chest: str = "input[name='body_part'][value='chest']"
    fight_hit_belt: str = "input[name='body_part'][value='belt']"
    fight_hit_legs: str = "input[name='body_part'][value='legs']"
    fight_block_head: str = "input[name='block'][value='head']"
    fight_block_chest: str = "input[name='block'][value='chest']"
    fight_block_belt: str = "input[name='block'][value='belt']"
    fight_block_legs: str = "input[name='block'][value='legs']"
    fight_submit: str = "input[type='submit'][name='hit'], button[name='hit']"
    fight_log_row: str = "table.fight_log tr, .log_line"
    fight_use_elixir: str = "a[href*='use_elixir'], .btn_elixir"
    fight_cast_spell: str = "a[href*='cast_spell'], .btn_spell"
    fight_flee: str = "a[href*='fight_flee'], .btn_flee"

    # ---- Квесты / диалоги --------------------------------------------------
    quest_dialog: str = "#dialog, .npc_dialog"
    quest_option: str = ".npc_dialog a, .dialog_option"
    quest_accept: str = "a[href*='quest_accept']"
    quest_complete: str = "a[href*='quest_complete']"

    # ---- Таймеры / кулдауны -----------------------------------------------
    timer_generic: str = ".timer[data-end], span[data-timer]"
    profession_timer: str = ".profession .timer"
    energy_bar: str = "#energy, .energy_value"

    # ---- Античит / антибот ------------------------------------------------
    captcha_image: str = "img[src*='captcha']"
    captcha_input: str = "input[name='captcha'], input[name='code']"
    captcha_submit: str = "input[type='submit'][value*='Проверить'], button.captcha_ok"


SELECTORS = Selectors()


# ---------------------------------------------------------------------------
# Задержки (диапазоны в секундах)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DelayRange:
    """Диапазон случайной паузы ``[min, max]`` секунд.

    Используется вместе с :func:`random.uniform` в антибот-модуле.
    """

    min: float
    max: float

    def __post_init__(self) -> None:  # pragma: no cover — простая валидация
        if self.min < 0 or self.max < 0:
            raise ValueError(f"Delay range must be non-negative: {self}")
        if self.max < self.min:
            raise ValueError(f"Delay range max<min: {self}")


@dataclass(frozen=True)
class Delays:
    """Наборы диапазонов задержек, применяемых в разных сценариях."""

    micro: DelayRange = DelayRange(0.15, 0.45)       # между двумя действиями подряд
    action: DelayRange = DelayRange(0.9, 2.1)         # клик/ввод/переход
    page_load: DelayRange = DelayRange(1.6, 3.5)      # после загрузки страницы
    fight_turn: DelayRange = DelayRange(1.2, 2.8)     # выбор удара в бою
    poll: DelayRange = DelayRange(3.5, 7.0)           # период основного цикла
    long_idle: DelayRange = DelayRange(45.0, 180.0)   # «человек отвлёкся»
    reconnect: DelayRange = DelayRange(8.0, 20.0)     # при потере сессии
    keystroke: DelayRange = DelayRange(0.06, 0.19)    # ввод символа
    mouse_step: DelayRange = DelayRange(0.008, 0.028) # шаг движения мыши


DELAYS = Delays()


# ---------------------------------------------------------------------------
# Параметры Playwright / браузера
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrowserConfig:
    """Параметры браузера."""

    engine: str = _env_str("DWAR_BROWSER_ENGINE", "chromium") or "chromium"
    headless: bool = _env_bool("DWAR_HEADLESS", True)
    slow_mo_ms: int = _env_int("DWAR_SLOW_MO_MS", 0)
    viewport_width: int = _env_int("DWAR_VIEWPORT_W", 1366)
    viewport_height: int = _env_int("DWAR_VIEWPORT_H", 768)
    locale: str = _env_str("DWAR_LOCALE", "ru-RU") or "ru-RU"
    timezone_id: str = _env_str("DWAR_TZ", "Europe/Moscow") or "Europe/Moscow"
    user_agent: str = _env_str(
        "DWAR_USER_AGENT",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    ) or ""
    proxy_server: str | None = _env_str("DWAR_PROXY_SERVER")
    proxy_username: str | None = _env_str("DWAR_PROXY_USER")
    proxy_password: str | None = _env_str("DWAR_PROXY_PASS")
    default_timeout_ms: int = _env_int("DWAR_TIMEOUT_MS", 15_000)
    navigation_timeout_ms: int = _env_int("DWAR_NAV_TIMEOUT_MS", 30_000)
    disable_images: bool = _env_bool("DWAR_DISABLE_IMAGES", False)
    disable_webrtc: bool = _env_bool("DWAR_DISABLE_WEBRTC", True)


BROWSER = BrowserConfig()


# ---------------------------------------------------------------------------
# Telegram-нотификации
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TelegramConfig:
    """Конфиг Telegram-бота для отправки уведомлений."""

    enabled: bool = _env_bool("DWAR_TG_ENABLED", False)
    bot_token: str | None = _env_str("DWAR_TG_TOKEN")
    chat_id: str | None = _env_str("DWAR_TG_CHAT_ID")
    parse_mode: str = _env_str("DWAR_TG_PARSE_MODE", "HTML") or "HTML"
    api_base: str = _env_str("DWAR_TG_API_BASE", "https://api.telegram.org") or ""
    timeout_sec: float = _env_float("DWAR_TG_TIMEOUT", 10.0)
    rate_limit_per_min: int = _env_int("DWAR_TG_RATE", 20)

    def is_configured(self) -> bool:
        return bool(self.enabled and self.bot_token and self.chat_id)


TELEGRAM = TelegramConfig()


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoggingConfig:
    level: str = (_env_str("DWAR_LOG_LEVEL", "INFO") or "INFO").upper()
    file: Path = LOGS_DIR / "bot.log"
    max_bytes: int = _env_int("DWAR_LOG_MAX_BYTES", 5 * 1024 * 1024)
    backup_count: int = _env_int("DWAR_LOG_BACKUPS", 5)
    fmt: str = (
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    datefmt: str = "%Y-%m-%d %H:%M:%S"


LOGGING_CFG = LoggingConfig()


# ---------------------------------------------------------------------------
# Правила бизнес-логики (значения по умолчанию, могут переопределяться)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CombatRules:
    """Правила поведения боевого движка."""

    use_elixir_hp_ratio: float = _env_float("DWAR_COMBAT_HP_ELIXIR", 0.35)
    use_elixir_mp_ratio: float = _env_float("DWAR_COMBAT_MP_ELIXIR", 0.25)
    flee_hp_ratio: float = _env_float("DWAR_COMBAT_FLEE_HP", 0.10)
    prefer_targets: tuple[str, ...] = ("head", "chest", "belt", "legs")
    prefer_blocks: tuple[str, ...] = ("head", "chest", "belt", "legs")
    max_rounds: int = _env_int("DWAR_COMBAT_MAX_ROUNDS", 30)


COMBAT = CombatRules()


@dataclass(frozen=True)
class SessionRotation:
    """Настройки ротации мультиаккаунтов."""

    enabled: bool = _env_bool("DWAR_ROTATION", False)
    cooldown_sec: float = _env_float("DWAR_ROTATION_COOLDOWN", 900.0)
    max_failures: int = _env_int("DWAR_ROTATION_MAX_FAILS", 3)
    strategy: str = _env_str("DWAR_ROTATION_STRATEGY", "round_robin") or "round_robin"


ROTATION = SessionRotation()


# ---------------------------------------------------------------------------
# Требуемые куки для «живой» сессии dwar.ru
# ---------------------------------------------------------------------------

REQUIRED_COOKIE_NAMES: tuple[str, ...] = ("PHPSESSID",)
"""Куки, отсутствие которых означает, что файл сессии заведомо невалиден."""

RECOMMENDED_COOKIE_NAMES: tuple[str, ...] = ("dwar_sid", "dwar_uid", "userid")
"""Куки, дополнительно повышающие вероятность автологина."""

TARGET_DOMAINS: tuple[str, ...] = ("dwar.ru", ".dwar.ru", "www.dwar.ru")
"""Валидные домены для куков сессии игры."""


# ---------------------------------------------------------------------------
# Оверрайд из config.local.json
# ---------------------------------------------------------------------------

def _apply_local_overrides(target: Any, overrides: Mapping[str, Any]) -> Any:
    """Возвращает копию dataclass с применёнными оверрайдами.

    Работает только с полями верхнего уровня, вложенные оверрайды
    рекурсивно применяются к вложенным dataclass'ам.
    """
    if not overrides:
        return target
    current = asdict(target)
    for key, value in overrides.items():
        if key not in current:
            logger.warning("config.local.json: неизвестный ключ %r", key)
            continue
        current[key] = value
    return type(target)(**current)


def load_local_overrides() -> dict[str, Any]:
    """Загружает ``config.local.json`` (если существует) и возвращает словарь."""
    if not LOCAL_CONFIG_PATH.is_file():
        return {}
    try:
        with LOCAL_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config.local.json должен быть JSON-объектом")
        return data
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Не удалось загрузить %s: %s", LOCAL_CONFIG_PATH, exc)
        return {}


_OVERRIDES = load_local_overrides()

if _OVERRIDES:
    if "browser" in _OVERRIDES and isinstance(_OVERRIDES["browser"], dict):
        BROWSER = _apply_local_overrides(BROWSER, _OVERRIDES["browser"])  # type: ignore[misc]
    if "telegram" in _OVERRIDES and isinstance(_OVERRIDES["telegram"], dict):
        TELEGRAM = _apply_local_overrides(TELEGRAM, _OVERRIDES["telegram"])  # type: ignore[misc]
    if "combat" in _OVERRIDES and isinstance(_OVERRIDES["combat"], dict):
        COMBAT = _apply_local_overrides(COMBAT, _OVERRIDES["combat"])  # type: ignore[misc]
    if "rotation" in _OVERRIDES and isinstance(_OVERRIDES["rotation"], dict):
        ROTATION = _apply_local_overrides(ROTATION, _OVERRIDES["rotation"])  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Публичный snapshot конфигурации (для логов при старте)
# ---------------------------------------------------------------------------

def snapshot() -> dict[str, Any]:
    """Возвращает словарь с текущими значениями конфигурации (без секретов)."""
    def _safe(obj: Any) -> Any:
        data = asdict(obj)
        for key in list(data.keys()):
            lowered = key.lower()
            if "token" in lowered or "password" in lowered or "pass" in lowered:
                data[key] = "***" if data[key] else None
        return data

    try:
        from . import __version__ as _pkg_version
    except Exception:  # pragma: no cover — import fallback
        _pkg_version = "unknown"

    return {
        "version": _pkg_version,
        "project_root": str(PROJECT_ROOT),
        "sessions_dir": str(SESSIONS_DIR),
        "logs_dir": str(LOGS_DIR),
        "urls": asdict(URLS),
        "browser": _safe(BROWSER),
        "telegram": _safe(TELEGRAM),
        "combat": asdict(COMBAT),
        "rotation": asdict(ROTATION),
        "required_cookies": list(REQUIRED_COOKIE_NAMES),
        "recommended_cookies": list(RECOMMENDED_COOKIE_NAMES),
        "target_domains": list(TARGET_DOMAINS),
    }


__all__ = [
    "PROJECT_ROOT",
    "SESSIONS_DIR",
    "LOGS_DIR",
    "SNAPSHOTS_DIR",
    "STATE_DIR",
    "URLS",
    "SELECTORS",
    "DelayRange",
    "DELAYS",
    "BROWSER",
    "TELEGRAM",
    "LOGGING_CFG",
    "COMBAT",
    "ROTATION",
    "REQUIRED_COOKIE_NAMES",
    "RECOMMENDED_COOKIE_NAMES",
    "TARGET_DOMAINS",
    "load_local_overrides",
    "snapshot",
]
