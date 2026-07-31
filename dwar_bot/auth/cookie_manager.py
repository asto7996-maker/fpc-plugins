"""
auth/cookie_manager.py
======================

Загрузка, валидация, нормализация и ротация cookie-сессий для игры Dwar.

Поддерживаемые форматы экспорта cookie:

* **Cookie-Editor JSON** — массив объектов вида
  ``{"name": ..., "value": ..., "domain": ..., "expirationDate": ...}``.
  Именно этот формат отдаёт популярное расширение *Cookie-Editor*.
* **Netscape cookies.txt** — табулированный текстовый формат
  (``domain\\tflag\\tpath\\tsecure\\texpiry\\tname\\tvalue``), который
  экспортируют многие браузерные расширения и утилиты (curl, wget).

Каждый файл в директории cookie трактуется как отдельная **сессия/аккаунт**
(:class:`SessionProfile`). :class:`CookieManager` умеет:

* найти и распарсить все профили;
* провалидировать их (наличие обязательных cookie, срок годности, домен);
* выдавать текущий активный валидный профиль;
* конвертировать cookie в формат Playwright и в ``dict`` для ``requests``;
* ротировать сессии — переключаться на следующий валидный профиль, помечая
  протухшие/забаненные как недоступные;
* сохранять обновлённые cookie обратно на диск (после логина/продления).

Модуль не зависит от Playwright/requests и может использоваться автономно.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.cookiejar import Cookie as HttpCookie  # noqa: F401 (документируем совместимость)
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..config import CookieConfig, settings
from ..logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Исключения.
# ---------------------------------------------------------------------------
class CookieValidationError(Exception):
    """Профиль cookie не прошёл валидацию (нет обязательных cookie / протух)."""


class CookieParseError(Exception):
    """Не удалось разобрать файл cookie (неизвестный/битый формат)."""


# ---------------------------------------------------------------------------
# Нормализованная модель cookie.
# ---------------------------------------------------------------------------
# Соответствие значений sameSite между Cookie-Editor и Playwright.
_SAMESITE_MAP: dict[str, str] = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "unspecified": "Lax",
    "strict": "Strict",
}


@dataclass(slots=True)
class Cookie:
    """
    Единый внутренний формат cookie, независимый от источника.

    Поле ``expires`` хранит абсолютный Unix-timestamp в секундах; значение
    ``None`` означает session-cookie (живёт до закрытия браузера).
    """

    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None
    secure: bool = False
    http_only: bool = False
    same_site: str = "Lax"

    # ------------------------------------------------------------------ #
    #                          Признаки состояния                        #
    # ------------------------------------------------------------------ #
    def is_expired(self, *, leeway_seconds: int = 0, now: float | None = None) -> bool:
        """
        Истекла ли cookie.

        :param leeway_seconds: запас — cookie, до истечения которой осталось
            меньше ``leeway_seconds``, тоже считается просроченной.
        :param now: точка отсчёта (для тестируемости); по умолчанию ``time.time()``.
        """
        if self.expires is None:
            return False  # session-cookie не имеет срока годности
        current = time.time() if now is None else now
        return self.expires <= (current + leeway_seconds)

    def matches_domain(self, domain: str) -> bool:
        """
        Подходит ли cookie для указанного домена по правилам сопоставления.

        Учитывает ведущую точку (``.example.com`` матчит поддомены).
        """
        cookie_domain = self.domain.lower().lstrip(".")
        target = domain.lower().lstrip(".")
        if not cookie_domain:
            return True
        return target == cookie_domain or target.endswith("." + cookie_domain)

    # ------------------------------------------------------------------ #
    #                           Конвертеры                               #
    # ------------------------------------------------------------------ #
    def to_playwright(self) -> dict[str, Any]:
        """Представление cookie в формате Playwright ``BrowserContext.add_cookies``."""
        payload: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path or "/",
            "httpOnly": self.http_only,
            "secure": self.secure,
            "sameSite": self.same_site if self.same_site in {"Strict", "Lax", "None"} else "Lax",
        }
        # Playwright ждёт expires как float; -1 = session cookie.
        payload["expires"] = float(self.expires) if self.expires is not None else -1.0
        return payload

    def to_netscape_line(self) -> str:
        """Строка в формате Netscape cookies.txt."""
        include_subdomains = "TRUE" if self.domain.startswith(".") else "FALSE"
        secure = "TRUE" if self.secure else "FALSE"
        expiry = int(self.expires) if self.expires is not None else 0
        return "\t".join(
            [
                self.domain,
                include_subdomains,
                self.path or "/",
                secure,
                str(expiry),
                self.name,
                self.value,
            ]
        )

    # ------------------------------------------------------------------ #
    #                            Фабрики                                 #
    # ------------------------------------------------------------------ #
    @classmethod
    def from_cookie_editor(cls, raw: dict[str, Any]) -> "Cookie":
        """Построить cookie из одного объекта экспорта Cookie-Editor."""
        name = str(raw.get("name", "")).strip()
        if not name:
            raise CookieParseError("Cookie без имени в Cookie-Editor JSON.")

        expires_raw = raw.get("expirationDate", raw.get("expires"))
        expires: float | None
        if expires_raw in (None, "", 0):
            # session-cookie либо явно отсутствует срок
            expires = None
        else:
            try:
                expires = float(expires_raw)
            except (TypeError, ValueError):
                expires = None

        same_site_raw = str(raw.get("sameSite", "lax")).strip().lower()
        same_site = _SAMESITE_MAP.get(same_site_raw, "Lax")

        return cls(
            name=name,
            value=str(raw.get("value", "")),
            domain=str(raw.get("domain", "")).strip(),
            path=str(raw.get("path", "/")).strip() or "/",
            expires=expires,
            secure=bool(raw.get("secure", False)),
            http_only=bool(raw.get("httpOnly", raw.get("httponly", False))),
            same_site=same_site,
        )

    @classmethod
    def from_netscape_line(cls, line: str) -> "Cookie | None":
        """
        Разобрать одну строку cookies.txt.

        Возвращает ``None`` для комментариев/пустых строк.
        """
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # Спец-строка "#HttpOnly_" всё же несёт cookie.
            if not stripped.startswith("#HttpOnly_"):
                return None

        http_only = False
        if stripped.startswith("#HttpOnly_"):
            http_only = True
            stripped = stripped[len("#HttpOnly_"):]

        parts = stripped.split("\t")
        if len(parts) != 7:
            # Иногда используют пробелы — пробуем более мягкое разбиение.
            parts = stripped.split()
            if len(parts) < 7:
                raise CookieParseError(f"Некорректная строка Netscape cookie: {line!r}")
            # Значение может содержать пробелы — склеиваем хвост.
            parts = parts[:6] + [" ".join(parts[6:])]

        domain, _include_sub, path, secure, expiry, name, value = parts[:7]
        try:
            expires = float(expiry)
        except (TypeError, ValueError):
            expires = 0.0

        return cls(
            name=name.strip(),
            value=value,
            domain=domain.strip(),
            path=path.strip() or "/",
            expires=None if expires == 0 else expires,
            secure=secure.strip().upper() == "TRUE",
            http_only=http_only,
            same_site="Lax",
        )


# ---------------------------------------------------------------------------
# Профиль сессии = набор cookie из одного файла.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SessionProfile:
    """Одна сессия/аккаунт: путь к файлу и распарсенные cookie."""

    path: Path
    cookies: list[Cookie] = field(default_factory=list)
    # Помечается True после явной инвалидции (бан/разлогин), чтобы ротация
    # больше не выбирала этот профиль в текущем запуске.
    disabled: bool = False
    # Момент последней успешной активации (для стратегии ротации).
    last_used: float = 0.0

    @property
    def name(self) -> str:
        """Человекочитаемое имя профиля (имя файла без расширения)."""
        return self.path.stem

    def cookie_names(self) -> set[str]:
        return {c.name for c in self.cookies}

    def get(self, name: str) -> Cookie | None:
        for cookie in self.cookies:
            if cookie.name == name:
                return cookie
        return None

    # ------------------------------------------------------------------ #
    #                            Валидация                               #
    # ------------------------------------------------------------------ #
    def validate(self, config: CookieConfig, *, domain: str) -> None:
        """
        Проверить профиль. Бросает :class:`CookieValidationError` при провале.

        Критерии:
        * профиль не пуст;
        * присутствуют все обязательные cookie (``config.required_cookies``);
        * обязательные cookie не просрочены (с учётом ``expiry_leeway_seconds``);
        * хотя бы одна cookie принадлежит целевому домену игры.
        """
        if self.disabled:
            raise CookieValidationError(f"Профиль '{self.name}' помечен как отключённый.")

        if not self.cookies:
            raise CookieValidationError(f"Профиль '{self.name}' не содержит cookie.")

        present = self.cookie_names()
        missing = [name for name in config.required_cookies if name not in present]
        if missing:
            raise CookieValidationError(
                f"Профиль '{self.name}': отсутствуют обязательные cookie: "
                f"{', '.join(missing)}."
            )

        expired = [
            cookie.name
            for cookie in self.cookies
            if cookie.name in config.required_cookies
            and cookie.is_expired(leeway_seconds=config.expiry_leeway_seconds)
        ]
        if expired:
            raise CookieValidationError(
                f"Профиль '{self.name}': просрочены обязательные cookie: "
                f"{', '.join(expired)}."
            )

        if not any(cookie.matches_domain(domain) for cookie in self.cookies):
            raise CookieValidationError(
                f"Профиль '{self.name}': ни одна cookie не относится к домену '{domain}'."
            )

    def is_valid(self, config: CookieConfig, *, domain: str) -> bool:
        """Безопасная проверка без исключений."""
        try:
            self.validate(config, domain=domain)
            return True
        except CookieValidationError:
            return False

    # ------------------------------------------------------------------ #
    #                           Конвертеры                               #
    # ------------------------------------------------------------------ #
    def to_playwright(self, *, only_valid: bool = True, domain: str | None = None) -> list[dict[str, Any]]:
        """
        Список cookie в формате Playwright.

        :param only_valid: исключить просроченные cookie.
        :param domain: если задан — оставить только cookie этого домена.
        """
        leeway = 0
        result: list[dict[str, Any]] = []
        for cookie in self.cookies:
            if only_valid and cookie.is_expired(leeway_seconds=leeway):
                continue
            if domain is not None and not cookie.matches_domain(domain):
                continue
            result.append(cookie.to_playwright())
        return result

    def to_requests_dict(self, *, domain: str | None = None) -> dict[str, str]:
        """Простой ``{name: value}`` словарь для ``requests``/``httpx``."""
        result: dict[str, str] = {}
        for cookie in self.cookies:
            if domain is not None and not cookie.matches_domain(domain):
                continue
            result[cookie.name] = cookie.value
        return result

    def to_header(self, *, domain: str | None = None) -> str:
        """Готовое значение HTTP-заголовка ``Cookie: a=1; b=2``."""
        pairs = self.to_requests_dict(domain=domain)
        return "; ".join(f"{name}={value}" for name, value in pairs.items())


# ---------------------------------------------------------------------------
# Парсеры файлов cookie.
# ---------------------------------------------------------------------------
def _parse_cookie_editor_json(text: str) -> list[Cookie]:
    """Разобрать содержимое файла в формате Cookie-Editor JSON."""
    data = json.loads(text)

    # Возможны варианты: список cookie, либо объект-обёртка {"cookies": [...]}.
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        else:
            raise CookieParseError("JSON не содержит массива cookie.")

    if not isinstance(data, list):
        raise CookieParseError("Ожидался JSON-массив cookie.")

    cookies: list[Cookie] = []
    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            log.warning("Пропущен нечисловой элемент cookie #%d.", index)
            continue
        try:
            cookies.append(Cookie.from_cookie_editor(raw))
        except CookieParseError as exc:
            log.warning("Пропущена битая cookie #%d: %s", index, exc)
    if not cookies:
        raise CookieParseError("В JSON не найдено ни одной валидной cookie.")
    return cookies


def _parse_netscape(text: str) -> list[Cookie]:
    """Разобрать содержимое файла в формате Netscape cookies.txt."""
    cookies: list[Cookie] = []
    for line in text.splitlines():
        try:
            cookie = Cookie.from_netscape_line(line)
        except CookieParseError as exc:
            log.warning("Пропущена битая строка Netscape cookie: %s", exc)
            continue
        if cookie is not None and cookie.name:
            cookies.append(cookie)
    if not cookies:
        raise CookieParseError("В cookies.txt не найдено ни одной cookie.")
    return cookies


def load_cookies_from_file(path: Path) -> list[Cookie]:
    """
    Загрузить и распарсить один файл cookie, автоматически определив формат.

    Определение формата:
    1. расширение ``.json`` → пробуем JSON (Cookie-Editor);
    2. расширение ``.txt`` → пробуем Netscape;
    3. иначе — эвристика по содержимому (начинается с ``[``/``{`` → JSON).

    Если основной парсер падает — пробуем альтернативный, прежде чем сдаться.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")  # utf-8-sig съест BOM
    except OSError as exc:
        raise CookieParseError(f"Не удалось прочитать файл cookie '{path}': {exc}") from exc

    stripped = text.lstrip()
    suffix = path.suffix.lower()

    looks_json = stripped.startswith("[") or stripped.startswith("{")

    parsers: list[tuple[str, Any]]
    if suffix == ".json" or (suffix not in {".txt"} and looks_json):
        parsers = [("json", _parse_cookie_editor_json), ("netscape", _parse_netscape)]
    else:
        parsers = [("netscape", _parse_netscape), ("json", _parse_cookie_editor_json)]

    last_error: Exception | None = None
    for fmt_name, parser in parsers:
        try:
            cookies = parser(text)
            log.debug("Файл '%s' распознан как формат '%s' (%d cookie).",
                      path.name, fmt_name, len(cookies))
            return cookies
        except (CookieParseError, json.JSONDecodeError) as exc:
            last_error = exc
            continue

    raise CookieParseError(
        f"Не удалось разобрать файл cookie '{path}' ни как JSON, ни как Netscape: "
        f"{last_error}"
    )


# ---------------------------------------------------------------------------
# Менеджер сессий: обнаружение, валидация, ротация.
# ---------------------------------------------------------------------------
class CookieManager:
    """
    Управляет пулом cookie-профилей и ротацией между ними.

    Типовое использование::

        cm = CookieManager()
        cm.load()                       # найти и распарсить все файлы cookie
        profile = cm.active_profile     # текущий валидный профиль
        await context.add_cookies(cm.playwright_cookies())

        # при разлогине/бане:
        cm.invalidate_current()
        if cm.rotate():
            await context.add_cookies(cm.playwright_cookies())
        else:
            log.error("Валидных сессий не осталось!")
    """

    def __init__(
        self,
        config: CookieConfig | None = None,
        *,
        domain: str | None = None,
    ) -> None:
        self._config: CookieConfig = config or settings.cookies
        self._domain: str = domain or settings.game.cookie_domain
        self._profiles: list[SessionProfile] = []
        self._active_index: int = -1
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    #                       Обнаружение и загрузка                       #
    # ------------------------------------------------------------------ #
    def discover_files(self) -> list[Path]:
        """Найти все файлы cookie в директории по заданным маскам."""
        directory = self._config.directory
        if not directory.exists():
            log.warning("Директория cookie не существует: %s", directory)
            return []
        found: list[Path] = []
        for pattern in self._config.file_patterns:
            found.extend(sorted(directory.glob(pattern)))
        # Уникализируем, сохраняя порядок.
        unique: list[Path] = []
        seen: set[Path] = set()
        for path in found:
            resolved = path.resolve()
            if resolved not in seen and path.is_file():
                seen.add(resolved)
                unique.append(path)
        return unique

    def load(self) -> int:
        """
        Загрузить и распарсить все профили cookie из директории.

        :return: количество успешно загруженных профилей.
        """
        with self._lock:
            self._profiles.clear()
            self._active_index = -1

            for path in self.discover_files():
                try:
                    cookies = load_cookies_from_file(path)
                except CookieParseError as exc:
                    log.error("Профиль '%s' не загружен: %s", path.name, exc)
                    continue
                profile = SessionProfile(path=path, cookies=cookies)
                self._profiles.append(profile)
                valid = profile.is_valid(self._config, domain=self._domain)
                log.info(
                    "Загружен профиль '%s': %d cookie, валиден=%s.",
                    profile.name, len(cookies), valid,
                )

            if not self._profiles:
                log.error(
                    "Не найдено ни одного профиля cookie в '%s' (маски: %s).",
                    self._config.directory, ", ".join(self._config.file_patterns),
                )
                return 0

            # Сразу выбираем первый валидный профиль как активный.
            self._select_first_valid()
            return len(self._profiles)

    # ------------------------------------------------------------------ #
    #                       Доступ к профилям                            #
    # ------------------------------------------------------------------ #
    @property
    def profiles(self) -> tuple[SessionProfile, ...]:
        with self._lock:
            return tuple(self._profiles)

    @property
    def active_profile(self) -> SessionProfile | None:
        """Текущий активный профиль либо ``None``, если валидных нет."""
        with self._lock:
            if 0 <= self._active_index < len(self._profiles):
                return self._profiles[self._active_index]
            return None

    def valid_profiles(self) -> list[SessionProfile]:
        """Список профилей, проходящих валидацию прямо сейчас."""
        with self._lock:
            return [
                p for p in self._profiles
                if not p.disabled and p.is_valid(self._config, domain=self._domain)
            ]

    def require_active(self) -> SessionProfile:
        """
        Вернуть активный профиль или бросить исключение, если его нет.

        Удобно в местах, где отсутствие сессии — фатально.
        """
        profile = self.active_profile
        if profile is None:
            raise CookieValidationError(
                "Нет активного валидного профиля cookie. Проверьте директорию "
                f"'{self._config.directory}' и обязательные cookie "
                f"({', '.join(self._config.required_cookies)})."
            )
        return profile

    # ------------------------------------------------------------------ #
    #                            Ротация                                 #
    # ------------------------------------------------------------------ #
    def _select_first_valid(self) -> bool:
        """Выбрать первый валидный профиль. Возвращает успех операции."""
        for index, profile in enumerate(self._profiles):
            if not profile.disabled and profile.is_valid(self._config, domain=self._domain):
                self._active_index = index
                profile.last_used = time.time()
                log.info("Активная cookie-сессия: '%s'.", profile.name)
                return True
        self._active_index = -1
        log.error("Среди %d профилей нет ни одного валидного.", len(self._profiles))
        return False

    def rotate(self) -> bool:
        """
        Переключиться на следующий валидный профиль (по кругу).

        Учитывает флаг ``rotation_enabled``. Возвращает ``True``, если удалось
        активировать новый (отличный от текущего либо первый валидный) профиль.
        """
        with self._lock:
            if not self._config.rotation_enabled:
                log.warning("Ротация cookie отключена конфигурацией.")
                return False

            total = len(self._profiles)
            if total == 0:
                return False

            start = self._active_index
            # Идём по кругу, начиная со следующего индекса.
            for step in range(1, total + 1):
                candidate = (start + step) % total
                profile = self._profiles[candidate]
                if profile.disabled:
                    continue
                if profile.is_valid(self._config, domain=self._domain):
                    self._active_index = candidate
                    profile.last_used = time.time()
                    log.info("Ротация cookie → активирован профиль '%s'.", profile.name)
                    return True

            log.error("Ротация невозможна: валидных профилей не осталось.")
            self._active_index = -1
            return False

    def invalidate_current(self, *, reason: str = "") -> None:
        """
        Пометить текущий активный профиль как недоступный (бан/разлогин).

        После этого он исключается из ротации в рамках текущего запуска.
        """
        with self._lock:
            profile = self.active_profile
            if profile is None:
                return
            profile.disabled = True
            log.warning(
                "Профиль '%s' помечен недоступным%s.",
                profile.name,
                f" ({reason})" if reason else "",
            )

    def invalidate(self, profile_name: str, *, reason: str = "") -> None:
        """Пометить недоступным конкретный профиль по имени."""
        with self._lock:
            for profile in self._profiles:
                if profile.name == profile_name:
                    profile.disabled = True
                    log.warning(
                        "Профиль '%s' помечен недоступным%s.",
                        profile_name, f" ({reason})" if reason else "",
                    )
                    return

    def reset_disabled(self) -> None:
        """Снять флаг ``disabled`` со всех профилей (например, при рестарте)."""
        with self._lock:
            for profile in self._profiles:
                profile.disabled = False
            log.info("Сброшены флаги недоступности у всех профилей cookie.")

    # ------------------------------------------------------------------ #
    #                     Конвертеры активного профиля                   #
    # ------------------------------------------------------------------ #
    def playwright_cookies(self, *, only_valid: bool = True) -> list[dict[str, Any]]:
        """Cookie активного профиля в формате Playwright."""
        profile = self.require_active()
        return profile.to_playwright(only_valid=only_valid, domain=self._domain)

    def requests_cookies(self) -> dict[str, str]:
        """Cookie активного профиля как ``{name: value}`` для requests/httpx."""
        profile = self.require_active()
        return profile.to_requests_dict(domain=self._domain)

    def cookie_header(self) -> str:
        """Значение HTTP-заголовка ``Cookie`` для активного профиля."""
        profile = self.require_active()
        return profile.to_header(domain=self._domain)

    # ------------------------------------------------------------------ #
    #                     Сохранение обновлённых cookie                  #
    # ------------------------------------------------------------------ #
    def update_active_from_playwright(self, playwright_cookies: Iterable[dict[str, Any]]) -> None:
        """
        Обновить cookie активного профиля из cookie, полученных Playwright.

        Полезно после логина/продления сессии, чтобы новые значения ушли на
        диск при :meth:`save_active`.
        """
        with self._lock:
            profile = self.require_active()
            merged: dict[tuple[str, str, str], Cookie] = {}

            # Начинаем с уже имеющихся, затем перекрываем свежими.
            for cookie in profile.cookies:
                merged[(cookie.name, cookie.domain, cookie.path)] = cookie

            for raw in playwright_cookies:
                try:
                    expires_raw = raw.get("expires", -1)
                    expires = None if expires_raw in (None, -1, -1.0) else float(expires_raw)
                    same_site = str(raw.get("sameSite", "Lax"))
                    cookie = Cookie(
                        name=str(raw["name"]),
                        value=str(raw.get("value", "")),
                        domain=str(raw.get("domain", self._domain)),
                        path=str(raw.get("path", "/")) or "/",
                        expires=expires,
                        secure=bool(raw.get("secure", False)),
                        http_only=bool(raw.get("httpOnly", False)),
                        same_site=same_site if same_site in {"Strict", "Lax", "None"} else "Lax",
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("Пропущена некорректная Playwright-cookie: %s", exc)
                    continue
                merged[(cookie.name, cookie.domain, cookie.path)] = cookie

            profile.cookies = list(merged.values())
            log.debug("Активный профиль '%s' обновлён (%d cookie).",
                      profile.name, len(profile.cookies))

    def save_active(self, *, backup: bool = True) -> Path:
        """
        Сохранить активный профиль обратно на диск в формате Cookie-Editor JSON.

        :param backup: сделать резервную копию исходного файла (``*.bak``).
        :return: путь сохранённого файла.
        """
        with self._lock:
            profile = self.require_active()
            target = profile.path

            if backup and target.exists():
                backup_path = target.with_suffix(target.suffix + ".bak")
                try:
                    backup_path.write_bytes(target.read_bytes())
                except OSError as exc:
                    log.warning("Не удалось создать бэкап '%s': %s", backup_path, exc)

            payload = [self._cookie_to_editor_dict(c) for c in profile.cookies]
            try:
                target.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                raise CookieParseError(f"Не удалось сохранить cookie в '{target}': {exc}") from exc

            log.info("Профиль '%s' сохранён (%d cookie).", profile.name, len(payload))
            return target

    @staticmethod
    def _cookie_to_editor_dict(cookie: Cookie) -> dict[str, Any]:
        """Сериализовать cookie в объект формата Cookie-Editor."""
        same_site_reverse = {"None": "no_restriction", "Lax": "lax", "Strict": "strict"}
        data: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "httpOnly": cookie.http_only,
            "sameSite": same_site_reverse.get(cookie.same_site, "lax"),
            "session": cookie.expires is None,
        }
        if cookie.expires is not None:
            data["expirationDate"] = cookie.expires
        return data

    # ------------------------------------------------------------------ #
    #                          Служебное                                 #
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self) -> Iterator[SessionProfile]:
        return iter(self.profiles)

    def summary(self) -> str:
        """Короткая сводка состояния пула для логов/диагностики."""
        with self._lock:
            valid = len(self.valid_profiles())
            active = self.active_profile
            active_name = active.name if active else "—"
            return (
                f"CookieManager: всего={len(self._profiles)}, валидных={valid}, "
                f"активный='{active_name}', домен='{self._domain}'."
            )


__all__ = [
    "Cookie",
    "SessionProfile",
    "CookieManager",
    "CookieValidationError",
    "CookieParseError",
    "load_cookies_from_file",
]
