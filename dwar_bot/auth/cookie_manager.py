"""
Загрузка, валидация и сохранение cookie-сессий для dwar.ru.

Поддерживаемые форматы экспорта:
  - Cookie Editor / EditThisCookie (JSON-массив)
  - Netscape / cookies.txt

Сессии валидируются по сроку действия cookie и HTTP-запросом к главной
странице с проверкой маркеров авторизации и игровых фреймов.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urljoin

import aiohttp

from dwar_bot.config import (
    AUTH_FAILURE_MARKERS,
    AUTH_SUCCESS_MARKERS,
    BotConfig,
    config,
    get_delay_range,
)

logger = logging.getLogger(__name__)

NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
COOKIE_EDITOR_REQUIRED_KEYS = frozenset({"name", "value"})

# HTML-признаки авторизованных фреймов игры
AUTH_FRAME_MARKERS: tuple[str, ...] = (
    'name="main"',
    "name='main'",
    'name="fight"',
    "name='fight'",
    'name="backpack"',
    "name='backpack'",
    'name="chat"',
    "name='chat'",
    'name="menu"',
    "frameset",
    "game.php",
)


class CookieFormat(str, Enum):
    JSON = "json"
    NETSCAPE = "netscape"
    AUTO = "auto"


class CookieValidationError(Exception):
    """Cookie не прошли структурную или сетевую валидацию."""


class SessionRotationError(Exception):
    """Не удалось переключиться на рабочую сессию."""


@dataclass(slots=True)
class RawCookie:
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: Optional[float] = None
    http_only: bool = False
    secure: bool = False
    same_site: str = "Lax"

    def is_expired(self, skew_sec: int = 0) -> bool:
        if self.expires is None or self.expires <= 0:
            return False
        return time.time() >= (self.expires - skew_sec)

    def domain_matches(self, host: str) -> bool:
        if not self.domain:
            return True
        cookie_domain = self.domain.lstrip(".").lower()
        host_lower = host.lower()
        return host_lower == cookie_domain or host_lower.endswith("." + cookie_domain)

    def to_editor_dict(self) -> Dict[str, Any]:
        """Сериализация в формат Cookie Editor / EditThisCookie."""
        item: Dict[str, Any] = {
            "domain": self.domain,
            "hostOnly": not self.domain.startswith(".") if self.domain else True,
            "httpOnly": self.http_only,
            "name": self.name,
            "path": self.path or "/",
            "sameSite": self.same_site or "Lax",
            "secure": self.secure,
            "session": self.expires is None or self.expires <= 0,
            "storeId": "0",
            "value": self.value,
        }
        if self.expires and self.expires > 0:
            item["expirationDate"] = self.expires
        return item


@dataclass(slots=True)
class CookieSession:
    """Одна именованная cookie-сессия."""

    name: str
    source_path: Path
    cookies: List[RawCookie]
    format: CookieFormat
    loaded_at: float = field(default_factory=time.time)
    last_validated_at: Optional[float] = None
    last_validation_ok: Optional[bool] = None
    validation_message: str = ""

    @property
    def cookie_map(self) -> Dict[str, str]:
        return {c.name: c.value for c in self.cookies}

    def get_playwright_cookies(self, default_domain: str) -> List[Dict[str, Any]]:
        """Конвертация в формат Playwright context.add_cookies()."""
        result: List[Dict[str, Any]] = []
        for cookie in self.cookies:
            domain = cookie.domain or default_domain
            if domain and not domain.startswith(".") and domain.count(".") >= 1:
                domain = "." + domain.lstrip(".")

            entry: Dict[str, Any] = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": cookie.path or "/",
            }
            if cookie.expires and cookie.expires > 0:
                entry["expires"] = int(cookie.expires)
            if cookie.http_only:
                entry["httpOnly"] = True
            if cookie.secure:
                entry["secure"] = True
            if cookie.same_site:
                same_site = cookie.same_site.capitalize()
                if same_site in {"Lax", "Strict", "None"}:
                    entry["sameSite"] = same_site
            result.append(entry)
        return result

    def summary(self) -> str:
        names = ", ".join(sorted(self.cookie_map))
        return f"{self.name} ({self.source_path.name}): [{names}]"


class CookieManager:
    """
    Загрузка cookie из JSON, валидация сессии и сохранение обновлений.

    Основной путь: data/cookies/cookies.json (Cookie Editor / EditThisCookie).
    """

    def __init__(self, bot_config: Optional[BotConfig] = None) -> None:
        self._config = bot_config or config
        self._cookie_cfg = self._config.cookies
        self._sessions: List[CookieSession] = []
        self._active_index: int = 0
        self._lock = asyncio.Lock()
        self._http_session: Optional[aiohttp.ClientSession] = None

    @property
    def active_session(self) -> Optional[CookieSession]:
        if not self._sessions:
            return None
        if 0 <= self._active_index < len(self._sessions):
            return self._sessions[self._active_index]
        return None

    @property
    def sessions(self) -> Sequence[CookieSession]:
        return tuple(self._sessions)

    @property
    def cookies_file(self) -> Path:
        return self._cookie_cfg.cookies_file

    async def __aenter__(self) -> "CookieManager":
        await self._ensure_http_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

    async def _ensure_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self._cookie_cfg.validation_timeout_sec,
                connect=min(10.0, self._cookie_cfg.validation_timeout_sec),
            )
            self._http_session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": self._config.browser.user_agent,
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,*/*;q=0.8"
                    ),
                },
            )
        return self._http_session

    async def human_delay(self, kind: str = "action") -> None:
        min_delay, max_delay = get_delay_range(kind)
        delay = random.uniform(min_delay, max_delay)
        logger.debug("Human delay (%s): %.2f сек", kind, delay)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Чтение и валидация JSON
    # ------------------------------------------------------------------

    def discover_session_files(self) -> List[Path]:
        """Находит файлы сессий: сначала cookies.json, затем резервные."""
        cookies_dir = self._cookie_cfg.cookies_dir
        discovered: List[Path] = []

        primary = self._cookie_cfg.cookies_file
        if primary.is_file():
            discovered.append(primary)

        for filename in self._cookie_cfg.session_files:
            path = cookies_dir / filename
            if path.is_file() and path.resolve() not in {
                p.resolve() for p in discovered
            }:
                discovered.append(path)

        if not discovered and cookies_dir.is_dir():
            for pattern in ("*.json", "*.txt", "*.cookies"):
                for path in sorted(cookies_dir.glob(pattern)):
                    if path.resolve() not in {p.resolve() for p in discovered}:
                        discovered.append(path)

        return discovered

    def load_cookies(
        self, path: Optional[Path] = None, *, validate_structure: bool = True
    ) -> CookieSession:
        """
        Читает и валидирует JSON-файл куков (Cookie Editor / EditThisCookie).

        Raises:
            FileNotFoundError: файл отсутствует
            CookieValidationError: невалидный JSON, пустой файл, нет PHPSESSID,
                просроченные cookie
        """
        target = (path or self._cookie_cfg.cookies_file).expanduser().resolve()
        session = self.load_session_from_file(target, name=target.stem)
        if validate_structure:
            self._validate_structure(session)
        if not self._sessions:
            self._sessions = [session]
            self._active_index = 0
        else:
            replaced = False
            for idx, existing in enumerate(self._sessions):
                if existing.source_path.resolve() == session.source_path.resolve():
                    self._sessions[idx] = session
                    self._active_index = idx
                    replaced = True
                    break
            if not replaced:
                self._sessions.insert(0, session)
                self._active_index = 0
        logger.info("Загружены cookie из %s (%s шт.)", target, len(session.cookies))
        return session

    def load_all_sessions(self) -> List[CookieSession]:
        """Синхронная загрузка всех доступных cookie-файлов."""
        files = self.discover_session_files()
        sessions: List[CookieSession] = []

        for index, path in enumerate(files):
            try:
                session = self.load_session_from_file(
                    path, name=path.stem or f"session_{index}"
                )
                sessions.append(session)
                logger.info("Загружена cookie-сессия: %s", session.summary())
            except FileNotFoundError as exc:
                logger.error("Файл cookie не найден: %s", exc)
            except CookieValidationError as exc:
                logger.error("Невалидные cookie в %s: %s", path, exc, exc_info=True)
            except Exception as exc:
                logger.error(
                    "Не удалось загрузить cookie из %s: %s",
                    path,
                    exc,
                    exc_info=True,
                )

        self._sessions = sessions
        self._active_index = 0 if sessions else 0
        if not sessions:
            logger.warning(
                "Cookie-файлы не найдены. Положите экспорт Cookie Editor в %s "
                "(ожидается cookies.json).",
                self._cookie_cfg.cookies_file,
            )
        return sessions

    def load_session_from_file(
        self,
        path: Path,
        name: Optional[str] = None,
        fmt: CookieFormat = CookieFormat.AUTO,
    ) -> CookieSession:
        path = path.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Cookie-файл не найден: {path}")

        try:
            content = path.read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise CookieValidationError(f"Не удалось прочитать {path}: {exc}") from exc

        if not content:
            raise CookieValidationError(f"Пустой cookie-файл: {path}")

        detected = self._detect_format(content) if fmt == CookieFormat.AUTO else fmt
        if detected == CookieFormat.JSON:
            cookies = self._parse_json_cookies(content, source=path)
        elif detected == CookieFormat.NETSCAPE:
            cookies = self._parse_netscape_cookies(content, source=path)
        else:
            raise CookieValidationError(f"Не удалось определить формат cookie: {path}")

        session = CookieSession(
            name=name or path.stem,
            source_path=path,
            cookies=cookies,
            format=detected,
        )
        self._validate_structure(session)
        return session

    def _detect_format(self, content: str) -> CookieFormat:
        stripped = content.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return CookieFormat.JSON
        if NETSCAPE_HEADER.lower() in content.lower() or self._looks_like_netscape(
            content
        ):
            return CookieFormat.NETSCAPE
        raise CookieValidationError(
            "Неизвестный формат cookie: ожидается JSON (Cookie Editor / EditThisCookie) "
            "или Netscape .txt"
        )

    @staticmethod
    def _looks_like_netscape(content: str) -> bool:
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            return len(line.split("\t")) >= 6
        return False

    def _parse_json_cookies(self, content: str, source: Path) -> List[RawCookie]:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise CookieValidationError(
                f"Невалидный JSON в {source}: {exc.msg} (строка {exc.lineno})"
            ) from exc

        items: Iterable[Any]
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # EditThisCookie / некоторые экспорты оборачивают в объект
            if "cookies" in data and isinstance(data["cookies"], list):
                items = data["cookies"]
            elif all(isinstance(v, dict) and "value" in v for v in data.values()):
                # Редкий формат: { "PHPSESSID": { "value": "...", ... } }
                items = [{"name": k, **v} for k, v in data.items()]
            else:
                raise CookieValidationError(
                    f"JSON в {source} должен быть массивом cookie "
                    f"(Cookie Editor / EditThisCookie) или объектом с ключом 'cookies'"
                )
        else:
            raise CookieValidationError(f"Неподдерживаемая JSON-структура в {source}")

        cookies: List[RawCookie] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                logger.warning("Пропуск cookie #%s в %s: не объект", index, source)
                continue
            try:
                cookies.append(self._raw_from_mapping(item))
            except CookieValidationError as exc:
                logger.warning("Пропуск cookie #%s в %s: %s", index, source, exc)

        if not cookies:
            raise CookieValidationError(f"В {source} не найдено валидных cookie")
        return cookies

    @staticmethod
    def _raw_from_mapping(item: Mapping[str, Any]) -> RawCookie:
        missing = COOKIE_EDITOR_REQUIRED_KEYS - set(item.keys())
        if missing:
            raise CookieValidationError(
                f"Отсутствуют поля: {', '.join(sorted(missing))}"
            )

        name = str(item["name"]).strip()
        value = str(item["value"])
        if not name:
            raise CookieValidationError("Пустое имя cookie")

        expires_raw = item.get("expirationDate", item.get("expires"))
        expires: Optional[float] = None
        if expires_raw is not None and expires_raw != "":
            try:
                expires = float(expires_raw)
            except (TypeError, ValueError) as exc:
                raise CookieValidationError(
                    f"Некорректный expires: {expires_raw}"
                ) from exc

        same_site_raw = item.get("sameSite")
        same_site = "Lax"
        if isinstance(same_site_raw, str) and same_site_raw.strip():
            normalized = same_site_raw.strip().lower()
            if normalized in {"no_restriction", "none"}:
                same_site = "None"
            elif normalized in {"lax", "strict"}:
                same_site = normalized.capitalize()
            else:
                same_site = same_site_raw.strip()

        return RawCookie(
            name=name,
            value=value,
            domain=str(item.get("domain", "")).strip(),
            path=str(item.get("path", "/") or "/"),
            expires=expires,
            http_only=bool(item.get("httpOnly", item.get("httponly", False))),
            secure=bool(item.get("secure", False)),
            same_site=same_site,
        )

    def _parse_netscape_cookies(self, content: str, source: Path) -> List[RawCookie]:
        cookies: List[RawCookie] = []
        for line_no, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) < 7:
                logger.warning(
                    "Строка %s в %s: ожидается 7 полей Netscape, получено %s",
                    line_no,
                    source,
                    len(parts),
                )
                continue

            domain, _flag, path, secure, expires, name, value = parts[:7]
            if not name:
                continue

            expires_val: Optional[float]
            try:
                expires_int = int(expires)
                expires_val = float(expires_int) if expires_int > 0 else None
            except ValueError:
                expires_val = None

            cookies.append(
                RawCookie(
                    name=name,
                    value=value,
                    domain=domain.strip(),
                    path=path.strip() or "/",
                    expires=expires_val,
                    secure=secure.upper() == "TRUE",
                    http_only=False,
                    same_site="Lax",
                )
            )

        if not cookies:
            raise CookieValidationError(f"В Netscape-файле {source} нет cookie")
        return cookies

    def _validate_structure(self, session: CookieSession) -> None:
        names = {c.name for c in session.cookies}
        missing_required = [
            name
            for name in self._cookie_cfg.required_cookie_names
            if name not in names
        ]
        if missing_required:
            raise CookieValidationError(
                f"Сессия {session.name}: отсутствуют обязательные cookie: "
                f"{', '.join(missing_required)}"
            )

        expired = [
            c.name
            for c in session.cookies
            if c.is_expired(self._cookie_cfg.expiry_skew_sec)
        ]
        if expired:
            raise CookieValidationError(
                f"Сессия {session.name}: просроченные cookie: {', '.join(expired)}"
            )

        host = self._extract_host(self._config.server.base_url)
        domain_mismatched = [
            c.name
            for c in session.cookies
            if c.domain
            and not self._domain_allowed(c.domain)
            and not c.domain_matches(host)
        ]
        if domain_mismatched:
            logger.warning(
                "Сессия %s: cookie с нестандартным доменом: %s",
                session.name,
                ", ".join(domain_mismatched),
            )

    def _domain_allowed(self, domain: str) -> bool:
        domain_clean = domain.lstrip(".").lower()
        return any(
            domain_clean == allowed.lstrip(".").lower()
            or domain_clean.endswith("." + allowed.lstrip(".").lower())
            for allowed in self._cookie_cfg.allowed_domains
        )

    @staticmethod
    def _extract_host(url: str) -> str:
        match = re.match(r"^https?://([^/]+)", url.strip(), re.IGNORECASE)
        if not match:
            raise ValueError(f"Некорректный URL: {url}")
        return match.group(1).lower()

    # ------------------------------------------------------------------
    # Проверка валидности сессии
    # ------------------------------------------------------------------

    async def validate_session(
        self,
        session: Optional[CookieSession] = None,
        *,
        url: Optional[str] = None,
    ) -> bool:
        """
        Проверяет сессию запросом к главной странице.

        Критерии успеха:
          - cookie не просрочены;
          - HTTP 200/302;
          - в HTML/URL есть маркеры авторизации или игровых фреймов
            (main / fight / backpack / frameset).
        """
        target_session = session or self.active_session
        if target_session is None:
            raise CookieValidationError("Нет сессии для валидации")

        target_url = url or urljoin(
            self._config.server.base_url,
            self._cookie_cfg.validation_url_path,
        )
        host = self._extract_host(target_url)

        for cookie in target_session.cookies:
            if cookie.is_expired(self._cookie_cfg.expiry_skew_sec):
                target_session.validation_message = f"Просрочен cookie: {cookie.name}"
                target_session.last_validation_ok = False
                target_session.last_validated_at = time.time()
                logger.warning(
                    "Сессия %s: %s",
                    target_session.name,
                    target_session.validation_message,
                )
                return False

        http = await self._ensure_http_session()

        try:
            async with http.get(
                target_url,
                cookies=self._aiohttp_cookies(target_session, host),
                allow_redirects=True,
                max_redirects=self._cookie_cfg.validation_max_redirects,
            ) as response:
                body = await response.text(errors="replace")
                final_url = str(response.url)
                ok = self._interpret_auth_response(
                    status=response.status,
                    body=body,
                    final_url=final_url,
                )
                target_session.last_validated_at = time.time()
                target_session.last_validation_ok = ok
                if ok:
                    target_session.validation_message = f"OK (HTTP {response.status})"
                    logger.info(
                        "Сессия %s валидна: HTTP %s, URL %s",
                        target_session.name,
                        response.status,
                        final_url,
                    )
                else:
                    target_session.validation_message = (
                        f"Неавторизован (HTTP {response.status}, URL {final_url})"
                    )
                    logger.warning(
                        "Сессия %s невалидна: %s",
                        target_session.name,
                        target_session.validation_message,
                    )
                return ok
        except asyncio.TimeoutError:
            target_session.validation_message = "Таймаут валидации"
            target_session.last_validation_ok = False
            target_session.last_validated_at = time.time()
            logger.error("Таймаут валидации сессии %s", target_session.name)
            return False
        except aiohttp.ClientError as exc:
            target_session.validation_message = f"Сетевая ошибка: {exc}"
            target_session.last_validation_ok = False
            target_session.last_validated_at = time.time()
            logger.error(
                "Ошибка сети при валидации %s: %s",
                target_session.name,
                exc,
                exc_info=True,
            )
            return False

    def _aiohttp_cookies(
        self, session: CookieSession, host: str
    ) -> Mapping[str, str]:
        result: MutableMapping[str, str] = {}
        for cookie in session.cookies:
            if cookie.domain and not cookie.domain_matches(host):
                if not self._domain_allowed(cookie.domain):
                    continue
            result[cookie.name] = cookie.value
        return result

    def _interpret_auth_response(
        self, status: int, body: str, final_url: str
    ) -> bool:
        if status >= 500:
            return False

        text = body.lower()
        url_lower = final_url.lower()

        # Явные маркеры логина без игровых фреймов — сессия мертва
        has_login_form = any(m in text for m in AUTH_FAILURE_MARKERS)
        has_game_frames = any(m.lower() in text for m in AUTH_FRAME_MARKERS)
        has_success = any(
            marker in text or marker in url_lower for marker in AUTH_SUCCESS_MARKERS
        )

        if has_game_frames or has_success:
            return True

        if has_login_form and not any(
            marker in url_lower for marker in ("game.php", "user.php")
        ):
            return False

        if status == 200 and "password" in text and (
            "login" in text or "войти" in text
        ):
            return False

        return status in {200, 302} and "dwar.ru" in url_lower and not has_login_form

    # ------------------------------------------------------------------
    # Сохранение обновлённых куков
    # ------------------------------------------------------------------

    def save_cookies(
        self,
        cookies: Optional[Sequence[RawCookie | Mapping[str, Any]]] = None,
        destination: Optional[Path] = None,
        *,
        session: Optional[CookieSession] = None,
    ) -> Path:
        """
        Сохраняет cookie обратно в JSON (формат Cookie Editor / EditThisCookie).

        Если cookies не переданы — сохраняется активная сессия.
        По умолчанию путь: data/cookies/cookies.json.
        """
        dest = (destination or self._cookie_cfg.cookies_file).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)

        raw_list: List[RawCookie]
        if cookies is not None:
            raw_list = []
            for item in cookies:
                if isinstance(item, RawCookie):
                    raw_list.append(item)
                elif isinstance(item, Mapping):
                    raw_list.append(self._raw_from_mapping(item))
                else:
                    raise CookieValidationError(
                        f"Неподдерживаемый тип cookie: {type(item)!r}"
                    )
        else:
            target = session or self.active_session
            if target is None:
                raise SessionRotationError("Нет активной сессии для сохранения")
            raw_list = list(target.cookies)

        if not raw_list:
            raise CookieValidationError("Нечего сохранять: список cookie пуст")

        payload = [cookie.to_editor_dict() for cookie in raw_list]
        tmp_path = dest.with_suffix(dest.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(dest)
        except OSError as exc:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    logger.debug("Не удалось удалить временный файл %s", tmp_path)
            raise CookieValidationError(
                f"Не удалось сохранить cookie в {dest}: {exc}"
            ) from exc

        # Обновляем in-memory сессию
        active = session or self.active_session
        if active is not None:
            active.cookies = raw_list
            active.source_path = dest
            active.format = CookieFormat.JSON
        else:
            self._sessions = [
                CookieSession(
                    name=dest.stem,
                    source_path=dest,
                    cookies=raw_list,
                    format=CookieFormat.JSON,
                )
            ]
            self._active_index = 0

        logger.info("Cookie сохранены в %s (%s шт.)", dest, len(raw_list))
        return dest

    async def sync_from_playwright(
        self, context: Any, destination: Optional[Path] = None
    ) -> Path:
        """
        Читает cookie из Playwright BrowserContext и сохраняет в файл.

        context — playwright.async_api.BrowserContext
        """
        pw_cookies = await context.cookies()
        converted: List[RawCookie] = []
        for item in pw_cookies:
            expires_raw = item.get("expires")
            expires: Optional[float] = None
            if expires_raw is not None:
                try:
                    expires_f = float(expires_raw)
                    # Playwright использует -1 для session cookie
                    expires = expires_f if expires_f > 0 else None
                except (TypeError, ValueError):
                    expires = None

            converted.append(
                RawCookie(
                    name=str(item.get("name", "")),
                    value=str(item.get("value", "")),
                    domain=str(item.get("domain", "")),
                    path=str(item.get("path", "/") or "/"),
                    expires=expires,
                    http_only=bool(item.get("httpOnly", False)),
                    secure=bool(item.get("secure", False)),
                    same_site=str(item.get("sameSite", "Lax") or "Lax"),
                )
            )

        converted = [c for c in converted if c.name]
        if not converted:
            raise CookieValidationError("Playwright context не вернул cookie")

        return self.save_cookies(converted, destination=destination)

    # ------------------------------------------------------------------
    # Жизненный цикл сессий
    # ------------------------------------------------------------------

    async def initialize(self, validate: bool = True) -> CookieSession:
        """Загружает сессии с диска и выбирает первую рабочую."""
        async with self._lock:
            # Предпочитаем явный cookies.json
            if self._cookie_cfg.cookies_file.is_file():
                try:
                    self.load_cookies(self._cookie_cfg.cookies_file)
                except (FileNotFoundError, CookieValidationError) as exc:
                    logger.error("Не удалось загрузить cookies.json: %s", exc)
                    self.load_all_sessions()
            else:
                self.load_all_sessions()

            if not self._sessions:
                raise SessionRotationError(
                    f"Нет cookie-сессий. Создайте файл {self._cookie_cfg.cookies_file} "
                    f"экспортом Cookie Editor / EditThisCookie."
                )

            if not validate:
                return self._sessions[self._active_index]

            for index, session in enumerate(self._sessions):
                await self.human_delay("action")
                if await self.validate_session(session):
                    self._active_index = index
                    return session

            raise SessionRotationError("Ни одна cookie-сессия не прошла валидацию")

    async def rotate_session(self, *, validate: bool = True) -> CookieSession:
        """Переключается на следующую сессию по кругу."""
        async with self._lock:
            if len(self._sessions) < 2:
                raise SessionRotationError(
                    "Ротация невозможна: доступна только одна сессия"
                )

            start_index = self._active_index
            attempts = 0
            total = len(self._sessions)

            while attempts < total:
                self._active_index = (self._active_index + 1) % total
                attempts += 1
                candidate = self._sessions[self._active_index]

                if self._active_index == start_index and attempts > 1:
                    break

                logger.info("Ротация сессии -> %s", candidate.name)
                await self.human_delay("session_rotate")

                if not validate or await self.validate_session(candidate):
                    logger.info("Активна сессия: %s", candidate.summary())
                    return candidate

            raise SessionRotationError("Ротация не нашла валидную сессию")

    async def ensure_valid_session(self) -> CookieSession:
        """Возвращает активную сессию или пытается ротировать при ошибке."""
        session = self.active_session
        if session is None:
            return await self.initialize(validate=True)

        if session.last_validation_ok is False:
            if self._cookie_cfg.rotate_on_validation_failure and len(self._sessions) > 1:
                return await self.rotate_session(validate=True)
            raise CookieValidationError(
                session.validation_message or "Сессия невалидна"
            )

        if await self.validate_session(session):
            return session

        if self._cookie_cfg.rotate_on_validation_failure and len(self._sessions) > 1:
            return await self.rotate_session(validate=True)

        raise CookieValidationError(session.validation_message or "Сессия невалидна")

    async def apply_to_playwright(self, context: Any) -> CookieSession:
        """Добавляет cookie активной сессии в Playwright BrowserContext."""
        session = await self.ensure_valid_session()
        host = self._extract_host(self._config.server.base_url)
        default_domain = f".{host.split(':')[0]}"
        # Для w1/w2 используем общий домен .dwar.ru
        if host.endswith("dwar.ru"):
            default_domain = ".dwar.ru"

        cookies = session.get_playwright_cookies(default_domain=default_domain)
        await context.clear_cookies()
        await context.add_cookies(cookies)
        logger.info(
            "В Playwright применено %s cookie из сессии %s",
            len(cookies),
            session.name,
        )
        return session

    def export_active_as_json(self, destination: Optional[Path] = None) -> Path:
        """Алиас save_cookies для активной сессии."""
        return self.save_cookies(destination=destination)

    def get_cookie_header(self, session: Optional[CookieSession] = None) -> str:
        """Строка Cookie для лёгких HTTP-запросов."""
        target = session or self.active_session
        if target is None:
            raise SessionRotationError("Нет активной сессии")
        return "; ".join(f"{k}={v}" for k, v in target.cookie_map.items())

    def format_expiry_report(self, session: Optional[CookieSession] = None) -> str:
        """Текстовый отчёт о сроках действия cookie."""
        target = session or self.active_session
        if target is None:
            return "Нет активной сессии"

        lines = [f"Сессия: {target.name} ({target.source_path})"]
        now = time.time()
        for cookie in sorted(target.cookies, key=lambda c: c.name):
            if cookie.expires is None or cookie.expires <= 0:
                exp_str = "session"
            else:
                dt = datetime.fromtimestamp(cookie.expires, tz=timezone.utc)
                remaining = cookie.expires - now
                exp_str = f"{dt.isoformat()} (через {int(remaining)} сек)"
            lines.append(f"  - {cookie.name}: {exp_str}")
        return "\n".join(lines)
