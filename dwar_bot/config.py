from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Environment variable {name} must be boolean, got: {raw!r}")


def _env_int(name: str, default: int, min_value: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"Environment variable {name} must be integer, got: {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ValueError(f"Environment variable {name} must be >= {min_value}, got: {value}")
    return value


def _env_float(name: str, default: float, min_value: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ValueError(f"Environment variable {name} must be float, got: {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ValueError(f"Environment variable {name} must be >= {min_value}, got: {value}")
    return value


def _env_list(name: str, default: Iterable[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return [item.strip() for item in default if item.strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True, slots=True)
class DelayRange:
    min_seconds: float
    max_seconds: float

    def __post_init__(self) -> None:
        if self.min_seconds < 0 or self.max_seconds < 0:
            raise ValueError("DelayRange values must be >= 0")
        if self.max_seconds < self.min_seconds:
            raise ValueError("DelayRange.max_seconds must be >= min_seconds")

    def sample(self) -> float:
        return random.uniform(self.min_seconds, self.max_seconds)


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    headless: bool
    slow_mo_ms: int
    navigation_timeout_ms: int
    action_timeout_ms: int
    context_locale: str
    timezone_id: str
    user_agent: str

    def __post_init__(self) -> None:
        if self.slow_mo_ms < 0:
            raise ValueError("slow_mo_ms must be >= 0")
        if self.navigation_timeout_ms <= 0 or self.action_timeout_ms <= 0:
            raise ValueError("browser timeouts must be > 0")
        if not self.context_locale.strip():
            raise ValueError("context_locale cannot be empty")
        if not self.timezone_id.strip():
            raise ValueError("timezone_id cannot be empty")
        if not self.user_agent.strip():
            raise ValueError("user_agent cannot be empty")


@dataclass(frozen=True, slots=True)
class DelaySettings:
    page_load: DelayRange
    between_actions: DelayRange
    after_click: DelayRange
    cookie_apply: DelayRange
    session_verify: DelayRange


@dataclass(frozen=True, slots=True)
class SelectorSettings:
    auth_success_selector: str
    auth_failure_selector: str
    profile_money_selector: str
    notifications_selector: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("auth_success_selector", self.auth_success_selector),
            ("auth_failure_selector", self.auth_failure_selector),
            ("profile_money_selector", self.profile_money_selector),
            ("notifications_selector", self.notifications_selector),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} cannot be empty")


@dataclass(frozen=True, slots=True)
class Credentials:
    login: str
    password: str

    @property
    def is_configured(self) -> bool:
        return bool(self.login and self.password)


@dataclass(frozen=True, slots=True)
class CookieSettings:
    cookie_dir: Path
    cookie_files: tuple[Path, ...]
    allowed_domains: tuple[str, ...]
    required_cookie_names: tuple[str, ...]
    min_cookies_per_session: int
    max_session_failures: int
    session_validation_url: str
    session_validation_timeout_ms: int

    def __post_init__(self) -> None:
        if self.min_cookies_per_session <= 0:
            raise ValueError("min_cookies_per_session must be > 0")
        if self.max_session_failures <= 0:
            raise ValueError("max_session_failures must be > 0")
        if self.session_validation_timeout_ms <= 0:
            raise ValueError("session_validation_timeout_ms must be > 0")
        if not self.session_validation_url.startswith(("http://", "https://")):
            raise ValueError("session_validation_url must start with http:// or https://")


@dataclass(frozen=True, slots=True)
class LogSettings:
    log_file: Path
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.log_level.strip():
            raise ValueError("log_level cannot be empty")


@dataclass(frozen=True, slots=True)
class BotConfig:
    project_root: Path
    base_url: str
    browser: BrowserSettings
    delays: DelaySettings
    selectors: SelectorSettings
    credentials: Credentials
    cookies: CookieSettings
    logs: LogSettings
    request_timeout_seconds: float = 25.0

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be > 0")


def load_config(project_root: Path | None = None) -> BotConfig:
    root = (project_root or Path(__file__).resolve().parent).resolve()
    cookies_dir = Path(os.getenv("DWAR_COOKIE_DIR", str(root / "cookies"))).expanduser().resolve()
    log_file = Path(os.getenv("DWAR_LOG_FILE", str(root / "bot.log"))).expanduser().resolve()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cookies_dir.mkdir(parents=True, exist_ok=True)

    cookie_files_raw = _env_list("DWAR_COOKIE_FILES", [])
    cookie_files = tuple(Path(path).expanduser().resolve() for path in cookie_files_raw)

    allowed_domains = tuple(
        domain.lower().lstrip(".")
        for domain in _env_list("DWAR_ALLOWED_COOKIE_DOMAINS", ("warofdragons.ru", "dwar.mail.ru"))
    )
    required_cookie_names = tuple(
        cookie_name.strip()
        for cookie_name in _env_list("DWAR_REQUIRED_COOKIE_NAMES", ("PHPSESSID",))
    )

    browser = BrowserSettings(
        headless=_env_bool("DWAR_HEADLESS", False),
        slow_mo_ms=_env_int("DWAR_SLOW_MO_MS", 65, min_value=0),
        navigation_timeout_ms=_env_int("DWAR_NAVIGATION_TIMEOUT_MS", 45_000, min_value=1),
        action_timeout_ms=_env_int("DWAR_ACTION_TIMEOUT_MS", 20_000, min_value=1),
        context_locale=os.getenv("DWAR_CONTEXT_LOCALE", "ru-RU").strip(),
        timezone_id=os.getenv("DWAR_TIMEZONE", "Europe/Moscow").strip(),
        user_agent=os.getenv(
            "DWAR_USER_AGENT",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        ).strip(),
    )

    delays = DelaySettings(
        page_load=DelayRange(
            _env_float("DWAR_DELAY_PAGE_LOAD_MIN", 1.4, min_value=0),
            _env_float("DWAR_DELAY_PAGE_LOAD_MAX", 2.9, min_value=0),
        ),
        between_actions=DelayRange(
            _env_float("DWAR_DELAY_ACTION_MIN", 0.55, min_value=0),
            _env_float("DWAR_DELAY_ACTION_MAX", 1.35, min_value=0),
        ),
        after_click=DelayRange(
            _env_float("DWAR_DELAY_AFTER_CLICK_MIN", 0.35, min_value=0),
            _env_float("DWAR_DELAY_AFTER_CLICK_MAX", 1.05, min_value=0),
        ),
        cookie_apply=DelayRange(
            _env_float("DWAR_DELAY_COOKIE_APPLY_MIN", 0.4, min_value=0),
            _env_float("DWAR_DELAY_COOKIE_APPLY_MAX", 1.2, min_value=0),
        ),
        session_verify=DelayRange(
            _env_float("DWAR_DELAY_SESSION_VERIFY_MIN", 0.45, min_value=0),
            _env_float("DWAR_DELAY_SESSION_VERIFY_MAX", 1.4, min_value=0),
        ),
    )

    selectors = SelectorSettings(
        auth_success_selector=os.getenv("DWAR_AUTH_SUCCESS_SELECTOR", "a[href*='info.php']").strip(),
        auth_failure_selector=os.getenv("DWAR_AUTH_FAILURE_SELECTOR", "form[action*='login']").strip(),
        profile_money_selector=os.getenv("DWAR_PROFILE_MONEY_SELECTOR", "#money").strip(),
        notifications_selector=os.getenv("DWAR_NOTIFICATIONS_SELECTOR", ".notifications, .event-alert").strip(),
    )

    config = BotConfig(
        project_root=root,
        base_url=os.getenv("DWAR_BASE_URL", "https://www.warofdragons.ru/").strip(),
        browser=browser,
        delays=delays,
        selectors=selectors,
        credentials=Credentials(
            login=os.getenv("DWAR_LOGIN", "").strip(),
            password=os.getenv("DWAR_PASSWORD", "").strip(),
        ),
        cookies=CookieSettings(
            cookie_dir=cookies_dir,
            cookie_files=cookie_files,
            allowed_domains=allowed_domains,
            required_cookie_names=required_cookie_names,
            min_cookies_per_session=_env_int("DWAR_MIN_COOKIES_PER_SESSION", 1, min_value=1),
            max_session_failures=_env_int("DWAR_MAX_SESSION_FAILURES", 3, min_value=1),
            session_validation_url=os.getenv("DWAR_SESSION_VALIDATION_URL", "https://www.warofdragons.ru/").strip(),
            session_validation_timeout_ms=_env_int("DWAR_SESSION_VALIDATION_TIMEOUT_MS", 20_000, min_value=1),
        ),
        logs=LogSettings(
            log_file=log_file,
            log_level=os.getenv("DWAR_LOG_LEVEL", "INFO").strip().upper(),
        ),
        request_timeout_seconds=_env_float("DWAR_REQUEST_TIMEOUT_SECONDS", 25.0, min_value=0.1),
    )
    return config


CONFIG: BotConfig = load_config()

