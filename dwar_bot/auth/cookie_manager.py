"""Cookie Manager для Dwar-бота.

Отвечает за:
    * загрузку кук из файлов формата **Cookie-Editor JSON** и **Netscape** (`cookies.txt`);
    * валидацию структуры/полей и проверку истечения срока;
    * конвертацию в форматы Playwright (``BrowserContext.add_cookies``) и Requests
      (``requests.cookies.RequestsCookieJar``);
    * атомарное сохранение обновлённых кук после сессии;
    * ротацию нескольких профилей (несколько аккаунтов) с выбором по стратегии
      "самый давно неиспользованный".

Модуль не зависит от Playwright — работает и в чисто-HTTP режиме.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..config import COOKIES_DIR, CREDS, URLS

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------
class CookieLoadError(RuntimeError):
    """Не удалось прочитать или распарсить файл с куками."""


class CookieValidationError(ValueError):
    """Куки прошли парсинг, но содержимое некорректное/устаревшее."""


# ---------------------------------------------------------------------------
# Каноничная модель куки
# ---------------------------------------------------------------------------
_SAMESITE_MAP: dict[str, str] = {
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}


@dataclass
class CanonicalCookie:
    """Внутреннее унифицированное представление cookie.

    Поле ``expires`` = UNIX-timestamp в секундах; ``None`` — session cookie.
    ``same_site`` — одно из "Lax" | "Strict" | "None" (или ``None``).
    """

    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None
    secure: bool = False
    http_only: bool = False
    same_site: str | None = "Lax"

    # -- Валидация ----------------------------------------------------------
    def validate(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise CookieValidationError("cookie.name отсутствует или не строка")
        if not isinstance(self.value, str):
            raise CookieValidationError(f"cookie[{self.name}].value должно быть строкой")
        if not self.domain or not isinstance(self.domain, str):
            raise CookieValidationError(f"cookie[{self.name}].domain отсутствует")
        if not self.path or not self.path.startswith("/"):
            raise CookieValidationError(
                f"cookie[{self.name}].path должен начинаться с '/'"
            )
        if self.expires is not None and not isinstance(self.expires, (int, float)):
            raise CookieValidationError(
                f"cookie[{self.name}].expires должно быть числом"
            )
        if self.same_site is not None and self.same_site not in ("Lax", "Strict", "None"):
            raise CookieValidationError(
                f"cookie[{self.name}].same_site недопустимо: {self.same_site!r}"
            )
        # SameSite=None требует Secure — приводим к валидному состоянию.
        if self.same_site == "None" and not self.secure:
            self.secure = True

    @property
    def is_expired(self) -> bool:
        if self.expires is None or self.expires <= 0:
            return False
        return self.expires < time.time()

    def matches_domain(self, allowed: Iterable[str]) -> bool:
        d = self.domain.lstrip(".").lower()
        for host in allowed:
            host = host.lstrip(".").lower()
            if d == host or d.endswith("." + host) or host.endswith("." + d):
                return True
        return False

    # -- Экспорт ------------------------------------------------------------
    def to_playwright(self) -> dict[str, Any]:
        """Возвращает dict, совместимый с ``context.add_cookies``."""
        entry: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "httpOnly": self.http_only,
        }
        if self.expires is not None:
            entry["expires"] = int(self.expires)
        if self.same_site is not None:
            entry["sameSite"] = self.same_site
        return entry

    def to_requests_cookie(self) -> Cookie:
        """Возвращает ``http.cookiejar.Cookie`` для requests/aiohttp."""
        domain = self.domain
        initial_dot = domain.startswith(".")
        return Cookie(
            version=0,
            name=self.name,
            value=self.value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=initial_dot,
            path=self.path,
            path_specified=True,
            secure=self.secure,
            expires=int(self.expires) if self.expires else None,
            discard=self.expires is None,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": ""} if self.http_only else {},
            rfc2109=False,
        )

    def to_editor_json(self) -> dict[str, Any]:
        """Экспорт в формат Cookie-Editor (совместимый JSON)."""
        entry: dict[str, Any] = {
            "domain": self.domain,
            "name": self.name,
            "value": self.value,
            "path": self.path,
            "secure": self.secure,
            "httpOnly": self.http_only,
            "hostOnly": not self.domain.startswith("."),
            "session": self.expires is None,
            "sameSite": (self.same_site or "unspecified").lower(),
            "storeId": None,
        }
        if self.expires is not None:
            entry["expirationDate"] = float(self.expires)
        return entry


# ---------------------------------------------------------------------------
# Профиль сессии (совокупность кук одного аккаунта)
# ---------------------------------------------------------------------------
@dataclass
class SessionProfile:
    """Куки одной учётной записи."""

    name: str
    source_path: Path
    cookies: list[CanonicalCookie] = field(default_factory=list)
    loaded_at: float = field(default_factory=time.time)
    last_used_at: float | None = None

    def __iter__(self) -> Iterator[CanonicalCookie]:
        return iter(self.cookies)

    def __len__(self) -> int:  # noqa: D401 - "return len"
        return len(self.cookies)

    # -- Быстрые выборки ----------------------------------------------------
    def get(self, name: str) -> CanonicalCookie | None:
        for c in self.cookies:
            if c.name == name:
                return c
        return None

    def filter_for_domain(self, domain: str) -> list[CanonicalCookie]:
        return [c for c in self.cookies if c.matches_domain([domain])]

    # -- Признаки валидности ------------------------------------------------
    @property
    def has_any_expired(self) -> bool:
        return any(c.is_expired for c in self.cookies)

    def has_session_marker(
        self,
        markers: Sequence[str] = ("dwar_sid", "PHPSESSID", "sid", "session"),
    ) -> bool:
        """Проверка, что среди кук есть что-то похожее на идентификатор сессии."""
        names = {c.name for c in self.cookies}
        return any(m in names for m in markers)

    # -- Экспорты --------------------------------------------------------
    def to_playwright(self) -> list[dict[str, Any]]:
        return [c.to_playwright() for c in self.cookies if not c.is_expired]

    def to_requests_jar(self) -> "Any":
        """Возвращает CookieJar; ``requests`` может не быть установлен."""
        try:
            from requests.cookies import RequestsCookieJar  # type: ignore
        except ImportError as exc:  # pragma: no cover — опциональная зависимость
            raise CookieLoadError(
                "Пакет 'requests' не установлен: pip install requests"
            ) from exc

        jar = RequestsCookieJar()
        for c in self.cookies:
            if c.is_expired:
                continue
            jar.set_cookie(c.to_requests_cookie())
        return jar

    def mark_used(self) -> None:
        self.last_used_at = time.time()


# ---------------------------------------------------------------------------
# Парсеры форматов
# ---------------------------------------------------------------------------
def _coerce_same_site(raw: Any) -> str | None:
    if raw is None:
        return "Lax"
    if isinstance(raw, bool):
        return "Lax"
    key = str(raw).strip().lower()
    if key in ("", "unspecified", "no_restriction"):
        return "Lax"
    return _SAMESITE_MAP.get(key, "Lax")


def _coerce_bool(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return default


def _coerce_expires(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw or raw.lower() in ("session", "0", "-1"):
            return None
        try:
            return float(raw)
        except ValueError:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                return None
    return None


def _parse_editor_entry(item: dict[str, Any]) -> CanonicalCookie:
    """Парсит один cookie из Cookie-Editor JSON."""
    name = item.get("name")
    value = item.get("value", "")
    domain = item.get("domain")
    if not name or not domain:
        raise CookieValidationError(f"cookie без имени/домена: {item!r}")
    path = item.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path

    expires_raw = item.get("expirationDate")
    if expires_raw is None:
        expires_raw = item.get("expires") or item.get("expiry")

    cookie = CanonicalCookie(
        name=str(name),
        value="" if value is None else str(value),
        domain=str(domain),
        path=str(path),
        expires=_coerce_expires(expires_raw),
        secure=_coerce_bool(item.get("secure"), False),
        http_only=_coerce_bool(item.get("httpOnly") or item.get("httponly"), False),
        same_site=_coerce_same_site(item.get("sameSite") or item.get("samesite")),
    )
    cookie.validate()
    return cookie


def _parse_editor_payload(data: Any, source: Path) -> list[CanonicalCookie]:
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and isinstance(data.get("cookies"), list):
        entries = data["cookies"]
    else:
        raise CookieLoadError(
            f"Неподдерживаемая структура JSON в {source}: ожидался список или объект с ключом 'cookies'"
        )

    cookies: list[CanonicalCookie] = []
    errors: list[str] = []
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            errors.append(f"[{i}] не dict: {type(raw).__name__}")
            continue
        try:
            cookies.append(_parse_editor_entry(raw))
        except CookieValidationError as exc:
            errors.append(f"[{i}] {exc}")

    if errors and not cookies:
        raise CookieValidationError(
            f"Ни одна кука из {source} не прошла валидацию: {'; '.join(errors[:5])}"
        )
    if errors:
        log.warning("Пропущено %d невалидных кук из %s: %s", len(errors), source.name, errors[:3])
    return cookies


_NETSCAPE_LINE = re.compile(
    r"^(?P<domain>\S+)\s+(?P<flag>TRUE|FALSE)\s+(?P<path>\S+)\s+"
    r"(?P<secure>TRUE|FALSE)\s+(?P<expires>-?\d+)\s+(?P<name>\S+)\s+(?P<value>.*)$",
    re.IGNORECASE,
)


def _parse_netscape_text(text: str, source: Path) -> list[CanonicalCookie]:
    cookies: list[CanonicalCookie] = []
    http_only_next = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            http_only_next = False
            continue
        if stripped.startswith("#"):
            if stripped.lower().startswith("#httponly_"):
                # Формат "#HttpOnly_<domain>	..." — префикс перед доменом.
                line = stripped[len("#HttpOnly_"):]
                http_only_next = True
            else:
                continue
        else:
            http_only_next = False

        # Netscape формат разделён табами, но многие редакторы шлют пробелами.
        fields = line.split("\t")
        if len(fields) < 7:
            match = _NETSCAPE_LINE.match(line)
            if not match:
                log.warning("Netscape: неразобранная строка в %s: %r", source.name, line[:80])
                continue
            domain = match.group("domain")
            path = match.group("path")
            secure = match.group("secure").upper() == "TRUE"
            expires = int(match.group("expires"))
            name = match.group("name")
            value = match.group("value")
        else:
            domain, _flag, path, secure_s, expires_s, name, value = fields[:7]
            secure = secure_s.upper() == "TRUE"
            try:
                expires = int(expires_s)
            except ValueError:
                expires = 0

        cookie = CanonicalCookie(
            name=name,
            value=value,
            domain=domain,
            path=path if path.startswith("/") else "/" + path,
            expires=float(expires) if expires > 0 else None,
            secure=secure,
            http_only=http_only_next,
            same_site="Lax",
        )
        try:
            cookie.validate()
        except CookieValidationError as exc:
            log.warning("Netscape: пропуск куки '%s' в %s: %s", name, source.name, exc)
            continue
        cookies.append(cookie)
    return cookies


def _detect_format(text: str) -> str:
    stripped = text.lstrip()
    if not stripped:
        return "empty"
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    if stripped.startswith("# Netscape HTTP Cookie File") or "\t" in stripped[:200]:
        return "netscape"
    return "netscape"


# ---------------------------------------------------------------------------
# CookieManager
# ---------------------------------------------------------------------------
class CookieManager:
    """Управляет одним или несколькими профилями кук.

    Пример::

        cm = CookieManager()
        cm.discover(Path("dwar_bot/sessions/cookies"))
        profile = cm.acquire()                      # взять свежий профиль
        await context.add_cookies(profile.to_playwright())
        ...
        cm.release(profile, refreshed_cookies=None) # вернуть в пул, опц. обновить
    """

    def __init__(
        self,
        allowed_domains: Sequence[str] | None = None,
        session_ttl_hours: int | None = None,
    ) -> None:
        self._profiles: list[SessionProfile] = []
        self._lock = threading.RLock()
        self._allowed_domains = tuple(allowed_domains or URLS.allowed_domains)
        self._session_ttl = (session_ttl_hours or CREDS.session_ttl_hours) * 3600

    # -- Свойства -----------------------------------------------------------
    @property
    def profiles(self) -> tuple[SessionProfile, ...]:
        with self._lock:
            return tuple(self._profiles)

    def __len__(self) -> int:
        return len(self._profiles)

    def __bool__(self) -> bool:
        return bool(self._profiles)

    # -- Загрузка -----------------------------------------------------------
    def load_file(self, path: str | os.PathLike[str]) -> SessionProfile:
        """Загружает один файл кук любого поддерживаемого формата."""
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise CookieLoadError(f"Файл с куками не найден: {p}")
        try:
            text = p.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CookieLoadError(f"Не удалось прочитать {p}: {exc}") from exc

        fmt = _detect_format(text)
        if fmt == "empty":
            raise CookieLoadError(f"Файл {p} пуст")
        if fmt == "json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CookieLoadError(f"Невалидный JSON в {p}: {exc}") from exc
            cookies = _parse_editor_payload(data, p)
        else:
            cookies = _parse_netscape_text(text, p)

        cookies = [c for c in cookies if c.matches_domain(self._allowed_domains)]
        if not cookies:
            raise CookieValidationError(
                f"В {p.name} нет ни одной куки для разрешённых доменов: {self._allowed_domains}"
            )

        profile = SessionProfile(name=p.stem, source_path=p, cookies=cookies)
        self._validate_profile(profile)

        with self._lock:
            self._profiles = [x for x in self._profiles if x.source_path != p]
            self._profiles.append(profile)
        log.info("Загружен профиль '%s' (%d кук) из %s", profile.name, len(cookies), p.name)
        return profile

    def discover(self, directory: str | os.PathLike[str] | None = None) -> int:
        """Ищет все файлы кук в каталоге и загружает их.

        Поддерживает расширения ``*.json``, ``*.txt``, ``*.cookies``.
        Возвращает число успешно загруженных профилей.
        """
        base = Path(directory) if directory else Path(CREDS.cookies_dir or COOKIES_DIR)
        base = base.expanduser().resolve()
        if not base.is_dir():
            log.warning("Каталог с куками не найден: %s", base)
            return 0

        patterns = ("*.json", "*.txt", "*.cookies")
        candidates: list[Path] = []
        for pat in patterns:
            candidates.extend(sorted(base.glob(pat)))

        loaded = 0
        for path in candidates:
            try:
                self.load_file(path)
                loaded += 1
            except (CookieLoadError, CookieValidationError) as exc:
                log.warning("Пропуск %s: %s", path.name, exc)
        log.info("CookieManager: загружено %d/%d файлов из %s", loaded, len(candidates), base)
        return loaded

    def load_from_env(self) -> SessionProfile | None:
        """Загружает файл, указанный в ``DWAR_COOKIES_FILE`` (если задан)."""
        cookies_file = CREDS.cookies_file
        if not cookies_file:
            return None
        return self.load_file(cookies_file)

    # -- Валидация ----------------------------------------------------------
    def _validate_profile(self, profile: SessionProfile) -> None:
        if not profile.cookies:
            raise CookieValidationError(f"Профиль '{profile.name}' пуст")
        alive = [c for c in profile.cookies if not c.is_expired]
        if not alive:
            raise CookieValidationError(
                f"Профиль '{profile.name}': все куки истекли"
            )
        # Мягкая проверка: если нет никаких session-подобных кук — предупреждение.
        if not profile.has_session_marker():
            log.warning(
                "Профиль '%s': не обнаружены session-куки (dwar_sid/PHPSESSID/sid). "
                "Авторизация может не сработать.",
                profile.name,
            )

    # -- Ротация ------------------------------------------------------------
    def acquire(self, strategy: str = "lru") -> SessionProfile:
        """Возвращает готовый к использованию профиль.

        strategy:
            * ``lru`` — наименее недавно использованный (по умолчанию);
            * ``random`` — случайный;
            * ``first`` — первый в списке.
        """
        with self._lock:
            candidates = [p for p in self._profiles if self._is_profile_usable(p)]
            if not candidates:
                raise CookieLoadError(
                    "Нет ни одного пригодного профиля кук. "
                    "Проверьте файлы в sessions/cookies или задайте DWAR_COOKIES_FILE."
                )

            if strategy == "random":
                choice = random.choice(candidates)
            elif strategy == "first":
                choice = candidates[0]
            else:
                candidates.sort(key=lambda p: (p.last_used_at or 0.0, p.loaded_at))
                choice = candidates[0]

            choice.mark_used()
            log.debug("acquire: выбран профиль '%s' (стратегия=%s)", choice.name, strategy)
            return choice

    def _is_profile_usable(self, profile: SessionProfile) -> bool:
        if not profile.cookies:
            return False
        if all(c.is_expired for c in profile.cookies):
            return False
        if self._session_ttl > 0 and profile.last_used_at is not None:
            # Сохраняем стратегию: разрешаем повторное использование,
            # даже если session_ttl истек — TTL используется как soft-hint.
            _ = profile.last_used_at  # placeholder to reference session_ttl semantics
        return True

    def release(
        self,
        profile: SessionProfile,
        refreshed_cookies: list[dict[str, Any]] | None = None,
        persist: bool = True,
    ) -> None:
        """Возвращает профиль в пул, при необходимости обновляя куки.

        ``refreshed_cookies`` — список dict в формате Playwright, полученный,
        например, из ``context.cookies()``.
        """
        with self._lock:
            if refreshed_cookies:
                updated = self._merge_playwright(profile.cookies, refreshed_cookies)
                profile.cookies = updated
                if persist:
                    try:
                        self.save_profile(profile)
                    except OSError as exc:
                        log.warning("Не удалось сохранить '%s': %s", profile.name, exc)
            profile.last_used_at = time.time()
            log.debug("release: профиль '%s' возвращён в пул", profile.name)

    def _merge_playwright(
        self,
        current: list[CanonicalCookie],
        incoming: list[dict[str, Any]],
    ) -> list[CanonicalCookie]:
        """Объединяет старые куки с новыми (по key = (domain, path, name))."""
        by_key: dict[tuple[str, str, str], CanonicalCookie] = {}
        for c in current:
            by_key[(c.domain.lstrip("."), c.path, c.name)] = c

        for raw in incoming:
            try:
                cookie = CanonicalCookie(
                    name=str(raw["name"]),
                    value=str(raw.get("value", "")),
                    domain=str(raw.get("domain", "")),
                    path=str(raw.get("path", "/")),
                    expires=_coerce_expires(raw.get("expires")),
                    secure=bool(raw.get("secure", False)),
                    http_only=bool(raw.get("httpOnly", False)),
                    same_site=_coerce_same_site(raw.get("sameSite")),
                )
                cookie.validate()
            except (KeyError, CookieValidationError) as exc:
                log.debug("merge: пропуск куки %r: %s", raw.get("name"), exc)
                continue
            if not cookie.matches_domain(self._allowed_domains):
                continue
            by_key[(cookie.domain.lstrip("."), cookie.path, cookie.name)] = cookie

        return list(by_key.values())

    # -- Экспорт / сохранение ----------------------------------------------
    def save_profile(
        self,
        profile: SessionProfile,
        target: Path | None = None,
        fmt: str = "editor_json",
    ) -> Path:
        """Сохраняет профиль в файл (атомарно).

        fmt: ``editor_json`` (по умолчанию) | ``netscape``.
        Если ``target`` не задан — переписывается исходный файл.
        """
        dest = Path(target) if target else profile.source_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "editor_json":
            payload = json.dumps(
                [c.to_editor_json() for c in profile.cookies],
                ensure_ascii=False,
                indent=2,
            )
            data_bytes = payload.encode("utf-8")
        elif fmt == "netscape":
            data_bytes = self._render_netscape(profile).encode("utf-8")
        else:
            raise ValueError(f"Неизвестный формат сохранения: {fmt!r}")

        # Атомарная запись через временный файл + os.replace
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent)
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, dest)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        log.info("Профиль '%s' сохранён в %s (%s)", profile.name, dest, fmt)
        return dest

    def _render_netscape(self, profile: SessionProfile) -> str:
        lines: list[str] = [
            "# Netscape HTTP Cookie File",
            "# Generated by dwar_bot.auth.cookie_manager",
            "",
        ]
        for c in profile.cookies:
            domain = c.domain if c.domain.startswith(".") else "." + c.domain
            include_sub = "TRUE"
            secure = "TRUE" if c.secure else "FALSE"
            expires = int(c.expires) if c.expires else 0
            prefix = "#HttpOnly_" if c.http_only else ""
            lines.append(
                f"{prefix}{domain}\t{include_sub}\t{c.path}\t{secure}\t{expires}\t{c.name}\t{c.value}"
            )
        lines.append("")
        return "\n".join(lines)

    # -- Утилиты ------------------------------------------------------------
    def snapshot_from_mozilla_jar(self, jar_path: str | os.PathLike[str]) -> SessionProfile:
        """Загружает куки из ``cookies.sqlite``-подобного Mozilla-jar файла.

        Используется для быстрой миграции существующих кук из curl/wget.
        """
        p = Path(jar_path).expanduser().resolve()
        jar = MozillaCookieJar(str(p))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except OSError as exc:
            raise CookieLoadError(f"MozillaCookieJar: {exc}") from exc

        cookies: list[CanonicalCookie] = []
        for c in jar:
            cookie = CanonicalCookie(
                name=c.name,
                value=c.value or "",
                domain=c.domain,
                path=c.path or "/",
                expires=float(c.expires) if c.expires else None,
                secure=bool(c.secure),
                http_only=bool(c.get_nonstandard_attr("HttpOnly", False)),
                same_site="Lax",
            )
            try:
                cookie.validate()
            except CookieValidationError as exc:
                log.debug("Mozilla jar: пропуск %s: %s", cookie.name, exc)
                continue
            if cookie.matches_domain(self._allowed_domains):
                cookies.append(cookie)

        if not cookies:
            raise CookieValidationError(
                f"MozillaCookieJar {p}: нет кук для {self._allowed_domains}"
            )
        profile = SessionProfile(name=p.stem, source_path=p, cookies=cookies)
        with self._lock:
            self._profiles.append(profile)
        return profile

    def clear(self) -> None:
        with self._lock:
            self._profiles.clear()


__all__ = [
    "CookieManager",
    "CookieValidationError",
    "CookieLoadError",
    "CanonicalCookie",
    "SessionProfile",
]
