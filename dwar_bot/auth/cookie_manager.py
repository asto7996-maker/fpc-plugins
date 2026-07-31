"""Управление куками и ротация сессий dwar.ru.

Модуль решает четыре задачи:

1. **Парсинг** файлов cookie в двух форматах:
    * ``Cookie-Editor`` (расширение Chrome) — JSON-массив объектов
      с полями ``name/value/domain/path/expirationDate/...``;
    * ``Netscape HTTP Cookie File`` — текстовый формат, генерируемый
      curl/wget/EditThisCookie (7 полей, разделитель — таб).
2. **Валидация** содержимого: домены (`dwar.ru`), обязательные куки
   (например, ``PHPSESSID``), сроки жизни, дубликаты.
3. **Ротация** нескольких сессий (мультиаккаунт): round-robin,
   random, weighted; учёт кулдаунов, счётчики ошибок, blacklist.
4. **Экспорт** в форматы, ожидаемые Playwright (``context.add_cookies``)
   и ``requests.Session.cookies``.

Модуль спроектирован так, чтобы работать без внешних зависимостей
(только стандартная библиотека). Дополнительно поддерживает интеграцию
с ``requests`` — если библиотека установлена, доступен экспорт в
``RequestsCookieJar``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..config import (
    REQUIRED_COOKIE_NAMES,
    RECOMMENDED_COOKIE_NAMES,
    ROTATION,
    SESSIONS_DIR,
    TARGET_DOMAINS,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------

class CookieFormatError(ValueError):
    """Файл куков не соответствует ни одному из поддерживаемых форматов."""


class CookieValidationError(ValueError):
    """Куки успешно разобраны, но не проходят логическую валидацию."""


class NoAvailableSessionError(RuntimeError):
    """В пуле не осталось живых сессий (все на кулдауне/заблокированы)."""


# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------

_SAMESITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
    "": "Lax",
    None: "Lax",
}


@dataclass
class Cookie:
    """Универсальная модель кука, независимая от источника."""

    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None  # UNIX timestamp; None → session cookie
    http_only: bool = False
    secure: bool = False
    same_site: str = "Lax"  # "Strict" | "Lax" | "None"

    # ---- Конвертация к внешним форматам ----------------------------------
    def to_playwright(self) -> dict[str, Any]:
        """Формат, ожидаемый Playwright ``BrowserContext.add_cookies``."""
        payload: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path or "/",
            "httpOnly": bool(self.http_only),
            "secure": bool(self.secure),
            "sameSite": self.same_site if self.same_site in ("Strict", "Lax", "None") else "Lax",
        }
        if self.expires is not None:
            payload["expires"] = float(self.expires)
        return payload

    def to_requests_kwargs(self) -> dict[str, Any]:
        """Формат для ``requests.cookies.create_cookie``."""
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain.lstrip("."),
            "path": self.path or "/",
            "expires": int(self.expires) if self.expires else None,
            "secure": bool(self.secure),
            "rest": {"HttpOnly": None} if self.http_only else {},
        }

    def is_expired(self, now: float | None = None) -> bool:
        """Истёк ли срок жизни кука."""
        if self.expires is None:
            return False
        return (now if now is not None else time.time()) >= self.expires

    def matches_domain(self, allowed: Iterable[str]) -> bool:
        """Проверка, что домен кука относится к одному из ``allowed``."""
        cookie_domain = self.domain.lstrip(".").lower()
        for candidate in allowed:
            c = candidate.lstrip(".").lower()
            if cookie_domain == c or cookie_domain.endswith("." + c):
                return True
        return False


@dataclass
class SessionProfile:
    """Метаданные одной сессии (одного аккаунта).

    ``cookies`` — распарсенный список куков; ``source_path`` — исходный файл;
    остальные поля управляют ротацией.
    """

    name: str
    source_path: Path
    format: str  # "cookie_editor" | "netscape"
    cookies: list[Cookie] = field(default_factory=list)
    loaded_at: float = field(default_factory=time.time)

    # Runtime-метрики ротации (мутируются под lock'ом)
    failures: int = 0
    last_used_at: float = 0.0
    cooldown_until: float = 0.0
    blacklisted: bool = False
    blacklist_reason: str | None = None

    def cookie_names(self) -> set[str]:
        return {c.name for c in self.cookies}

    def get(self, name: str) -> Cookie | None:
        for c in self.cookies:
            if c.name == name:
                return c
        return None

    def is_available(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if self.blacklisted:
            return False
        if self.cooldown_until and now < self.cooldown_until:
            return False
        return True

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": str(self.source_path),
            "format": self.format,
            "cookies_total": len(self.cookies),
            "cookie_names": sorted(self.cookie_names()),
            "failures": self.failures,
            "blacklisted": self.blacklisted,
            "blacklist_reason": self.blacklist_reason,
            "cooldown_until": self.cooldown_until,
            "last_used_at": self.last_used_at,
        }


# ---------------------------------------------------------------------------
# Парсеры
# ---------------------------------------------------------------------------

_NETSCAPE_LINE_RE = re.compile(r"^(?P<domain>[^\t]+)\t(?P<flag>TRUE|FALSE)\t(?P<path>[^\t]+)\t"
                               r"(?P<secure>TRUE|FALSE)\t(?P<expires>-?\d+)\t"
                               r"(?P<name>[^\t]+)\t(?P<value>.*)$")


def _normalize_samesite(raw: Any) -> str:
    if isinstance(raw, str):
        key = raw.strip().lower()
    else:
        key = raw
    return _SAMESITE_MAP.get(key, "Lax") if key in _SAMESITE_MAP else \
        (raw if raw in ("Strict", "Lax", "None") else "Lax")


def _parse_cookie_editor(raw: str, source: Path) -> list[Cookie]:
    """Парсит JSON-экспорт из расширения Cookie-Editor."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CookieFormatError(f"{source}: невалидный JSON: {exc}") from exc

    if not isinstance(data, list):
        raise CookieFormatError(f"{source}: ожидался JSON-массив, получен {type(data).__name__}")

    cookies: list[Cookie] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            logger.warning("%s[#%d]: элемент не является объектом, пропущен", source, idx)
            continue
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if not isinstance(name, str) or name == "":
            logger.warning("%s[#%d]: отсутствует name, пропущен", source, idx)
            continue
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        if not isinstance(domain, str) or domain == "":
            logger.warning("%s[#%d] (%s): пустой domain, пропущен", source, idx, name)
            continue

        expires: float | None = None
        session_flag = item.get("session", False)
        if not session_flag:
            raw_exp = item.get("expirationDate")
            if isinstance(raw_exp, (int, float)) and raw_exp > 0:
                expires = float(raw_exp)

        cookies.append(
            Cookie(
                name=name,
                value=value,
                domain=domain,
                path=(item.get("path") or "/") or "/",
                expires=expires,
                http_only=bool(item.get("httpOnly", False)),
                secure=bool(item.get("secure", False)),
                same_site=_normalize_samesite(item.get("sameSite")),
            )
        )
    return cookies


def _parse_netscape(raw: str, source: Path) -> list[Cookie]:
    """Парсит текстовый формат Netscape HTTP Cookie File."""
    cookies: list[Cookie] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # ``#HttpOnly_`` префикс — валидный расширенный синтаксис curl:
            if stripped.startswith("#HttpOnly_"):
                # уберём префикс и продолжим парсинг строки как обычной
                line = line.replace("#HttpOnly_", "", 1)
                http_only_forced = True
            else:
                continue
        else:
            http_only_forced = False

        m = _NETSCAPE_LINE_RE.match(line.rstrip("\n"))
        if not m:
            logger.warning("%s:%d: строка не соответствует Netscape-формату", source, lineno)
            continue

        try:
            expires_int = int(m.group("expires"))
        except ValueError:
            logger.warning("%s:%d: некорректное поле expires", source, lineno)
            continue

        expires: float | None = float(expires_int) if expires_int > 0 else None

        cookies.append(
            Cookie(
                name=m.group("name"),
                value=m.group("value"),
                domain=m.group("domain"),
                path=m.group("path") or "/",
                expires=expires,
                http_only=http_only_forced,
                secure=(m.group("secure") == "TRUE"),
                same_site="Lax",
            )
        )
    return cookies


def _detect_format(raw: str) -> str:
    stripped = raw.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        return "cookie_editor"
    # Netscape-файлы часто начинаются с "# Netscape HTTP Cookie File"
    return "netscape"


def parse_cookies_file(path: Path) -> tuple[list[Cookie], str]:
    """Загружает и парсит файл куков.

    Автоматически определяет формат по содержимому.

    Returns
    -------
    tuple[list[Cookie], str]
        Список куков и строковый идентификатор формата.

    Raises
    ------
    FileNotFoundError
        Если файл отсутствует.
    CookieFormatError
        Если формат не удалось распознать/распарсить.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Файл куков не найден: {path}")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CookieFormatError(f"Не удалось прочитать {path}: {exc}") from exc

    if not raw.strip():
        raise CookieFormatError(f"{path}: файл пуст")

    fmt = _detect_format(raw)
    cookies = _parse_cookie_editor(raw, path) if fmt == "cookie_editor" else _parse_netscape(raw, path)

    if not cookies:
        raise CookieFormatError(f"{path}: не удалось извлечь ни одного кука (формат={fmt})")

    return cookies, fmt


# ---------------------------------------------------------------------------
# Валидация
# ---------------------------------------------------------------------------

def validate_cookies(
    cookies: Sequence[Cookie],
    *,
    required: Sequence[str] = REQUIRED_COOKIE_NAMES,
    allowed_domains: Sequence[str] = TARGET_DOMAINS,
    now: float | None = None,
    strict_domain: bool = True,
) -> list[str]:
    """Валидирует список куков и возвращает список замечаний-предупреждений.

    * Обязательные куки должны присутствовать — иначе
      :class:`CookieValidationError`.
    * Все куки должны быть валидны по домену — если ``strict_domain=True``,
      посторонние куки вызовут исключение, иначе будут возвращены как warning.
    * Просроченные куки — только warning (могут быть session-only).
    """
    now = now if now is not None else time.time()
    warnings: list[str] = []
    names_present = {c.name for c in cookies}

    missing_required = [n for n in required if n not in names_present]
    if missing_required:
        raise CookieValidationError(
            f"Отсутствуют обязательные куки: {', '.join(missing_required)}"
        )

    missing_recommended = [n for n in RECOMMENDED_COOKIE_NAMES if n not in names_present]
    if missing_recommended:
        warnings.append(
            "Отсутствуют рекомендуемые куки (автологин может не сработать): "
            + ", ".join(missing_recommended)
        )

    seen_keys: set[tuple[str, str, str]] = set()
    for c in cookies:
        key = (c.name, c.domain.lstrip(".").lower(), c.path)
        if key in seen_keys:
            warnings.append(f"Дубликат кука {c.name} на {c.domain}{c.path}")
        seen_keys.add(key)

        if not c.matches_domain(allowed_domains):
            msg = f"Кук {c.name!r} принадлежит чужому домену: {c.domain}"
            if strict_domain:
                raise CookieValidationError(msg)
            warnings.append(msg)

        if c.is_expired(now):
            warnings.append(
                f"Кук {c.name!r} истёк "
                f"({datetime.fromtimestamp(c.expires or 0, tz=timezone.utc).isoformat()})"
            )

    return warnings


# ---------------------------------------------------------------------------
# CookieManager — оркестратор пула сессий
# ---------------------------------------------------------------------------

class CookieManager:
    """Пул сессий с поддержкой ротации и потокобезопасных операций.

    Пример использования::

        cm = CookieManager()
        cm.load_from_directory()
        session = cm.acquire()
        try:
            await browser_context.add_cookies(session.to_playwright_cookies())
            ...
        except SomeAuthError:
            cm.mark_failure(session, reason="captcha")
        else:
            cm.mark_success(session)
    """

    def __init__(
        self,
        *,
        sessions_dir: Path = SESSIONS_DIR,
        required_cookies: Sequence[str] = REQUIRED_COOKIE_NAMES,
        allowed_domains: Sequence[str] = TARGET_DOMAINS,
        strategy: str | None = None,
        cooldown_sec: float | None = None,
        max_failures: int | None = None,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.required_cookies = tuple(required_cookies)
        self.allowed_domains = tuple(allowed_domains)
        self.strategy = (strategy or ROTATION.strategy).lower()
        self.cooldown_sec = float(cooldown_sec if cooldown_sec is not None else ROTATION.cooldown_sec)
        self.max_failures = int(max_failures if max_failures is not None else ROTATION.max_failures)

        self._profiles: list[SessionProfile] = []
        self._rr_index: int = 0
        self._lock = threading.RLock()
        self._async_lock = asyncio.Lock()

    # ---- Загрузка --------------------------------------------------------

    def load_file(self, path: Path, *, name: str | None = None) -> SessionProfile:
        """Загружает один файл сессии, валидирует и добавляет в пул."""
        cookies, fmt = parse_cookies_file(path)
        warnings = validate_cookies(
            cookies,
            required=self.required_cookies,
            allowed_domains=self.allowed_domains,
            strict_domain=False,
        )
        for w in warnings:
            logger.warning("Сессия %s: %s", path.name, w)

        profile = SessionProfile(
            name=name or path.stem,
            source_path=path,
            format=fmt,
            cookies=cookies,
        )

        with self._lock:
            existing = next((p for p in self._profiles if p.name == profile.name), None)
            if existing is not None:
                logger.info("Обновляю сессию %r из %s", profile.name, path)
                existing.cookies = profile.cookies
                existing.format = profile.format
                existing.source_path = profile.source_path
                existing.loaded_at = time.time()
                existing.failures = 0
                existing.blacklisted = False
                existing.blacklist_reason = None
                existing.cooldown_until = 0.0
                return existing
            self._profiles.append(profile)

        logger.info(
            "Загружена сессия %r (%d куков, формат=%s)",
            profile.name, len(profile.cookies), profile.format,
        )
        return profile

    def load_from_directory(self, directory: Path | None = None) -> list[SessionProfile]:
        """Массовая загрузка всех ``*.json``/``*.txt``/``*.cookies`` из папки."""
        directory = directory or self.sessions_dir
        if not directory.is_dir():
            raise FileNotFoundError(f"Директория сессий не найдена: {directory}")

        patterns = ("*.json", "*.txt", "*.cookies", "*.netscape")
        files: list[Path] = []
        for pat in patterns:
            files.extend(sorted(directory.glob(pat)))

        if not files:
            logger.warning("В %s не найдено файлов куков", directory)
            return []

        loaded: list[SessionProfile] = []
        for path in files:
            try:
                loaded.append(self.load_file(path))
            except FileNotFoundError as exc:
                logger.error("Файл исчез до загрузки: %s (%s)", path, exc)
            except CookieFormatError as exc:
                logger.error("Файл %s: некорректный формат: %s", path, exc)
            except CookieValidationError as exc:
                logger.error("Файл %s: не прошёл валидацию: %s", path, exc)
            except Exception:  # pragma: no cover — на всякий случай
                logger.exception("Неизвестная ошибка при загрузке %s", path)
        return loaded

    # ---- Наблюдение за пулом ---------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._profiles)

    def __iter__(self) -> Iterator[SessionProfile]:
        with self._lock:
            return iter(list(self._profiles))

    def all_profiles(self) -> list[SessionProfile]:
        with self._lock:
            return list(self._profiles)

    def available_profiles(self) -> list[SessionProfile]:
        now = time.time()
        with self._lock:
            return [p for p in self._profiles if p.is_available(now)]

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [p.summary() for p in self._profiles]

    # ---- Ротация ---------------------------------------------------------

    def acquire(self, *, exclude: Iterable[str] = ()) -> SessionProfile:
        """Возвращает следующую доступную сессию согласно стратегии.

        Стратегии: ``round_robin`` (по кругу), ``random``, ``least_used``.
        """
        excluded = set(exclude)
        with self._lock:
            candidates = [p for p in self._profiles if p.is_available() and p.name not in excluded]
            if not candidates:
                raise NoAvailableSessionError(
                    "Нет доступных сессий (все на кулдауне/в blacklist)"
                )

            if self.strategy == "random":
                pick = random.choice(candidates)
            elif self.strategy == "least_used":
                pick = min(candidates, key=lambda p: p.last_used_at)
            else:  # round_robin (default)
                if self._rr_index >= len(self._profiles):
                    self._rr_index = 0
                # найдём следующий доступный, начиная с _rr_index
                total = len(self._profiles)
                pick = None
                for step in range(total):
                    profile = self._profiles[(self._rr_index + step) % total]
                    if profile.is_available() and profile.name not in excluded:
                        pick = profile
                        self._rr_index = (self._profiles.index(profile) + 1) % total
                        break
                if pick is None:
                    pick = candidates[0]

            pick.last_used_at = time.time()
            return pick

    async def acquire_async(self, *, exclude: Iterable[str] = ()) -> SessionProfile:
        """Async-обёртка над :meth:`acquire` с общим асинхронным локом."""
        async with self._async_lock:
            return self.acquire(exclude=exclude)

    def mark_success(self, profile: SessionProfile) -> None:
        with self._lock:
            profile.failures = 0
            profile.cooldown_until = 0.0
            profile.blacklisted = False
            profile.blacklist_reason = None

    def mark_failure(self, profile: SessionProfile, *, reason: str | None = None) -> None:
        with self._lock:
            profile.failures += 1
            profile.cooldown_until = time.time() + self.cooldown_sec
            logger.warning(
                "Сессия %r: ошибка #%d (%s); кулдаун %.0fs",
                profile.name, profile.failures, reason or "?", self.cooldown_sec,
            )
            if profile.failures >= self.max_failures:
                profile.blacklisted = True
                profile.blacklist_reason = reason or "max_failures"
                logger.error(
                    "Сессия %r занесена в blacklist (причина: %s)",
                    profile.name, profile.blacklist_reason,
                )

    def blacklist(self, profile: SessionProfile, *, reason: str) -> None:
        with self._lock:
            profile.blacklisted = True
            profile.blacklist_reason = reason
            profile.cooldown_until = time.time() + self.cooldown_sec

    def reset(self, profile: SessionProfile) -> None:
        """Полный сброс метрик сессии (например, после ручной починки)."""
        with self._lock:
            profile.failures = 0
            profile.cooldown_until = 0.0
            profile.blacklisted = False
            profile.blacklist_reason = None

    # ---- Экспортеры -------------------------------------------------------

    @staticmethod
    def to_playwright_cookies(profile: SessionProfile) -> list[dict[str, Any]]:
        """Готовит куки к передаче в ``context.add_cookies``."""
        now = time.time()
        return [c.to_playwright() for c in profile.cookies if not c.is_expired(now)]

    @staticmethod
    def to_requests_jar(profile: SessionProfile) -> Any:
        """Формирует ``requests.cookies.RequestsCookieJar`` (если установлен requests).

        Если ``requests`` не установлен — возвращает обычный ``dict``.
        """
        try:
            from requests.cookies import RequestsCookieJar, create_cookie  # type: ignore
        except ImportError:
            logger.debug("requests не установлен — возвращаю dict вместо RequestsCookieJar")
            now = time.time()
            return {c.name: c.value for c in profile.cookies if not c.is_expired(now)}

        jar = RequestsCookieJar()
        now = time.time()
        for c in profile.cookies:
            if c.is_expired(now):
                continue
            jar.set_cookie(create_cookie(**c.to_requests_kwargs()))
        return jar

    # ---- Персистентность runtime-метрик ----------------------------------

    def dump_state(self, path: Path) -> None:
        """Сохраняет runtime-метрики (кулдауны/blacklist) в JSON."""
        payload = {p.name: {
            "failures": p.failures,
            "cooldown_until": p.cooldown_until,
            "blacklisted": p.blacklisted,
            "blacklist_reason": p.blacklist_reason,
            "last_used_at": p.last_used_at,
        } for p in self.all_profiles()}
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.error("Не удалось сохранить state сессий в %s: %s", path, exc)

    def load_state(self, path: Path) -> None:
        """Восстанавливает runtime-метрики из JSON, созданного :meth:`dump_state`."""
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось загрузить state из %s: %s", path, exc)
            return
        if not isinstance(data, Mapping):
            logger.warning("State %s имеет неожиданный тип: %s", path, type(data).__name__)
            return
        with self._lock:
            for profile in self._profiles:
                entry = data.get(profile.name)
                if not isinstance(entry, Mapping):
                    continue
                profile.failures = int(entry.get("failures", 0))
                profile.cooldown_until = float(entry.get("cooldown_until", 0.0))
                profile.blacklisted = bool(entry.get("blacklisted", False))
                profile.blacklist_reason = entry.get("blacklist_reason")
                profile.last_used_at = float(entry.get("last_used_at", 0.0))


__all__ = [
    "Cookie",
    "SessionProfile",
    "CookieManager",
    "CookieFormatError",
    "CookieValidationError",
    "NoAvailableSessionError",
    "parse_cookies_file",
    "validate_cookies",
]
