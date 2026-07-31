"""Менеджер куки-сессий для авторизации в игре Dwar.

Возможности:
    * загрузка куков из файлов, экспортированных расширением **Cookie-Editor**
      в двух форматах:
        - JSON (массив объектов ``{"name", "value", "domain", ...}``);
        - Netscape ``cookies.txt`` (табулированный формат).
    * нормализация куков к формату, который принимает Playwright
      (``add_cookies``);
    * валидация: наличие обязательных полей, срок годности (``expirationDate``),
      наличие «сессионных» куков авторизации;
    * ротация сессий: перебор нескольких файлов куки при протухании одной
      сессии (мультиаккаунт / резервные сессии).

Модуль полностью самодостаточен и не требует запущенного браузера для
загрузки и валидации — это позволяет тестировать логику отдельно.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ..config import CONFIG
from ..logger import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
#  Исключения                                                                  #
# --------------------------------------------------------------------------- #
class CookieError(Exception):
    """Базовое исключение менеджера куки."""


class CookieValidationError(CookieError):
    """Куки не прошли валидацию (протухли/неполные/нет авторизационных)."""


class NoSessionsAvailableError(CookieError):
    """Не осталось валидных сессий для ротации."""


# --------------------------------------------------------------------------- #
#  Внутренние структуры                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    """Одна загруженная сессия (набор куков из одного файла)."""

    path: Path
    cookies: List[Dict] = field(default_factory=list)
    # Пометка о том, что сессия признана нерабочей в этом запуске.
    invalidated: bool = False

    @property
    def name(self) -> str:
        return self.path.stem


# Обязательные поля, которые должны присутствовать в каждом куки для Playwright.
_REQUIRED_FIELDS = ("name", "value")

# Допустимые значения sameSite для Playwright.
_VALID_SAMESITE = {"Strict", "Lax", "None"}


class CookieManager:
    """Загрузка, валидация и ротация куки-сессий."""

    def __init__(
        self,
        cookies_dir: Optional[Path] = None,
        cookies_file: Optional[str] = None,
        base_url: Optional[str] = None,
        auth_cookie_names: Optional[Sequence[str]] = None,
    ) -> None:
        self._cookies_dir = Path(cookies_dir or CONFIG.runtime.cookies_dir)
        self._explicit_file = cookies_file if cookies_file is not None else CONFIG.runtime.cookies_file
        self._base_url = (base_url or CONFIG.game.base_url).rstrip("/")
        self._auth_names = tuple(
            auth_cookie_names or CONFIG.game.auth_cookie_names
        )

        parsed = urlparse(self._base_url)
        # Домен по умолчанию, если в куках он не указан.
        self._default_domain = parsed.hostname or "www.dwar.ru"

        self._sessions: List[Session] = []
        self._current_index: int = -1

    # ------------------------------------------------------------------ #
    #  Публичный API                                                      #
    # ------------------------------------------------------------------ #
    def discover_sessions(self) -> List[Session]:
        """Находит и загружает все доступные файлы куки.

        Приоритет:
            1. Явно указанный ``cookies_file`` (если задан).
            2. Все ``*.json`` и ``*.txt`` в каталоге ``cookies_dir``.

        Возвращает список успешно загруженных (но ещё не обязательно
        провалидированных) сессий. Битые файлы пропускаются с логированием.
        """
        self._sessions = []
        self._current_index = -1

        candidate_paths: List[Path] = []

        if self._explicit_file:
            explicit = Path(self._explicit_file)
            if not explicit.is_absolute():
                explicit = (self._cookies_dir / explicit).resolve()
            candidate_paths.append(explicit)
        else:
            if self._cookies_dir.exists():
                for pattern in ("*.json", "*.txt"):
                    candidate_paths.extend(sorted(self._cookies_dir.glob(pattern)))

        if not candidate_paths:
            logger.warning(
                "Не найдено ни одного файла куки (dir=%s, file=%s)",
                self._cookies_dir,
                self._explicit_file or "-",
            )
            return []

        for path in candidate_paths:
            try:
                cookies = self.load_file(path)
            except CookieError as exc:
                logger.error("Пропускаю файл куки %s: %s", path.name, exc)
                continue
            if not cookies:
                logger.warning("Файл куки %s пуст после нормализации", path.name)
                continue
            self._sessions.append(Session(path=path, cookies=cookies))
            logger.info(
                "Загружена сессия '%s' (%d куки)", path.stem, len(cookies)
            )

        return list(self._sessions)

    def load_file(self, path: Path) -> List[Dict]:
        """Загружает и нормализует один файл куки (JSON или Netscape)."""
        path = Path(path)
        if not path.exists():
            raise CookieError(f"Файл не найден: {path}")

        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CookieError(f"Не удалось прочитать {path}: {exc}") from exc

        stripped = raw.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            cookies = self._parse_json(raw, path)
        else:
            cookies = self._parse_netscape(raw, path)

        normalized = [self._normalize_cookie(c) for c in cookies]
        # Отбрасываем None (не прошедшие нормализацию).
        return [c for c in normalized if c is not None]

    def get_current_session(self) -> Optional[Session]:
        """Возвращает текущую активную сессию (или None, если не выбрана)."""
        if 0 <= self._current_index < len(self._sessions):
            return self._sessions[self._current_index]
        return None

    def next_session(self) -> Session:
        """Выбирает следующую валидную сессию для ротации.

        Пропускает уже помеченные как нерабочие и протухшие. Бросает
        :class:`NoSessionsAvailableError`, если валидных сессий не осталось.
        """
        if not self._sessions:
            self.discover_sessions()

        total = len(self._sessions)
        if total == 0:
            raise NoSessionsAvailableError("Нет загруженных файлов куки")

        start = self._current_index + 1
        for offset in range(total):
            idx = (start + offset) % total
            session = self._sessions[idx]
            if session.invalidated:
                continue
            try:
                self.validate(session.cookies)
            except CookieValidationError as exc:
                logger.warning(
                    "Сессия '%s' не прошла валидацию: %s", session.name, exc
                )
                session.invalidated = True
                continue
            self._current_index = idx
            logger.info("Активирована сессия '%s'", session.name)
            return session

        raise NoSessionsAvailableError(
            "Не осталось валидных сессий для ротации (все протухли/помечены)"
        )

    def invalidate_current(self) -> None:
        """Помечает текущую сессию как нерабочую (например, после разлогина)."""
        session = self.get_current_session()
        if session is not None:
            session.invalidated = True
            logger.warning("Сессия '%s' помечена как нерабочая", session.name)

    def validate(self, cookies: Sequence[Dict]) -> bool:
        """Проверяет набор куки. Бросает :class:`CookieValidationError`.

        Критерии:
            * список не пуст;
            * у каждого куки есть ``name`` и ``value``;
            * есть хотя бы один авторизационный куки из ``auth_cookie_names``;
            * авторизационные куки не протухли по ``expires``.
        """
        if not cookies:
            raise CookieValidationError("Пустой набор куки")

        now = time.time()
        auth_found = False
        auth_valid = False

        for cookie in cookies:
            name = cookie.get("name")
            if not name or "value" not in cookie:
                raise CookieValidationError(
                    f"Куки без обязательных полей: {cookie!r}"
                )

            if name in self._auth_names:
                auth_found = True
                # Поддерживаем как нормализованный ключ ``expires`` (Playwright),
                # так и «сырой» ``expirationDate`` (Cookie-Editor).
                expires = cookie.get("expires")
                if expires is None:
                    expires = cookie.get("expirationDate", -1)
                try:
                    expires = float(expires) if expires is not None else -1
                except (TypeError, ValueError):
                    expires = -1
                # <=0 (или отсутствие) означает сессионный куки — считаем валидным.
                if expires <= 0 or expires > now:
                    if cookie.get("value"):
                        auth_valid = True
                else:
                    logger.debug(
                        "Авторизационный куки '%s' протух (expires=%s)",
                        name,
                        expires,
                    )

        if not auth_found:
            raise CookieValidationError(
                "Не найден ни один авторизационный куки "
                f"из {self._auth_names}"
            )
        if not auth_valid:
            raise CookieValidationError(
                "Авторизационные куки протухли или пусты"
            )
        return True

    async def apply_to_context(self, context, session: Optional[Session] = None) -> Session:
        """Применяет куки выбранной сессии к контексту Playwright.

        Если ``session`` не передан — берётся текущая, а если и её нет —
        выбирается через :meth:`next_session`. Возвращает применённую сессию.
        """
        if session is None:
            session = self.get_current_session() or self.next_session()

        # Финальная валидация перед применением.
        self.validate(session.cookies)

        try:
            await context.clear_cookies()
        except Exception:  # noqa: BLE001 - не критично, некоторые движки могут падать
            logger.debug("clear_cookies не поддержан/не выполнен, продолжаю")

        await context.add_cookies(session.cookies)
        logger.info(
            "Применены куки сессии '%s' (%d шт.) к контексту браузера",
            session.name,
            len(session.cookies),
        )
        return session

    # ------------------------------------------------------------------ #
    #  Парсеры форматов                                                   #
    # ------------------------------------------------------------------ #
    def _parse_json(self, raw: str, path: Path) -> List[Dict]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CookieError(f"Некорректный JSON в {path.name}: {exc}") from exc

        # Cookie-Editor экспортирует массив; некоторые расширения — объект-обёртку.
        if isinstance(data, dict):
            for key in ("cookies", "Cookies", "data"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                # Единичный куки-объект.
                data = [data]

        if not isinstance(data, list):
            raise CookieError(
                f"Ожидался список куки в {path.name}, получено {type(data).__name__}"
            )
        return data

    def _parse_netscape(self, raw: str, path: Path) -> List[Dict]:
        """Разбирает формат Netscape cookies.txt.

        Строка: ``domain  flag  path  secure  expiration  name  value``
        (разделитель — табуляция). Строки с ``#`` игнорируются, но учитывается
        префикс ``#HttpOnly_`` для HttpOnly-куков.
        """
        cookies: List[Dict] = []
        for line_no, raw_line in enumerate(raw.splitlines(), start=1):
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue

            http_only = False
            if line.startswith("#HttpOnly_"):
                http_only = True
                line = line[len("#HttpOnly_"):]
            elif line.lstrip().startswith("#"):
                # Обычный комментарий.
                continue

            parts = line.split("\t")
            if len(parts) < 7:
                # Иногда используют множественные пробелы вместо табов.
                parts = line.split()
            if len(parts) < 7:
                logger.debug(
                    "Строка %d в %s не похожа на Netscape-куки, пропускаю",
                    line_no,
                    path.name,
                )
                continue

            domain, _flag, cookie_path, secure, expiration, name, value = parts[:7]
            try:
                expires = float(expiration)
            except ValueError:
                expires = -1.0

            cookies.append(
                {
                    "domain": domain,
                    "path": cookie_path or "/",
                    "secure": secure.strip().upper() == "TRUE",
                    "expirationDate": expires if expires > 0 else None,
                    "name": name,
                    "value": value,
                    "httpOnly": http_only,
                }
            )
        return cookies

    # ------------------------------------------------------------------ #
    #  Нормализация под Playwright                                        #
    # ------------------------------------------------------------------ #
    def _normalize_cookie(self, cookie: Dict) -> Optional[Dict]:
        """Приводит один куки к формату Playwright ``add_cookies``.

        Возвращает ``None``, если куки невалиден (нет имени/значения).
        Playwright требует либо (``url``), либо (``domain`` + ``path``).
        """
        if not isinstance(cookie, dict):
            return None

        name = cookie.get("name") or cookie.get("Name")
        value = cookie.get("value")
        if value is None:
            value = cookie.get("Value")
        if not name or value is None:
            logger.debug("Куки без name/value отброшен: %r", cookie)
            return None

        domain = (
            cookie.get("domain")
            or cookie.get("Domain")
            or self._default_domain
        )
        # Cookie-Editor иногда хранит домен с ведущей точкой — Playwright ok.
        path = cookie.get("path") or cookie.get("Path") or "/"

        normalized: Dict = {
            "name": str(name),
            "value": str(value),
            "domain": str(domain),
            "path": str(path),
        }

        # secure / httpOnly
        normalized["secure"] = bool(
            cookie.get("secure", cookie.get("Secure", False))
        )
        normalized["httpOnly"] = bool(
            cookie.get("httpOnly", cookie.get("HttpOnly", cookie.get("http_only", False)))
        )

        # Срок годности: Cookie-Editor -> expirationDate (float, unix seconds).
        expires = (
            cookie.get("expires")
            if cookie.get("expires") is not None
            else cookie.get("expirationDate")
        )
        if expires is not None:
            try:
                expires_val = float(expires)
                # Отрицательные/нулевые считаем сессионными.
                normalized["expires"] = expires_val if expires_val > 0 else -1
            except (TypeError, ValueError):
                normalized["expires"] = -1
        # sameSite
        same_site = cookie.get("sameSite") or cookie.get("SameSite")
        normalized_same_site = self._normalize_samesite(same_site)
        if normalized_same_site:
            normalized["sameSite"] = normalized_same_site
            # Playwright требует secure=True при SameSite=None.
            if normalized_same_site == "None":
                normalized["secure"] = True

        return normalized

    @staticmethod
    def _normalize_samesite(value) -> Optional[str]:
        if not value:
            return None
        mapping = {
            "strict": "Strict",
            "lax": "Lax",
            "none": "None",
            "no_restriction": "None",
            "unspecified": "Lax",
        }
        result = mapping.get(str(value).strip().lower())
        if result in _VALID_SAMESITE:
            return result
        return None


def build_default_manager() -> CookieManager:
    """Фабрика менеджера куки с параметрами из глобальной конфигурации."""
    return CookieManager()


__all__ = [
    "CookieManager",
    "Session",
    "CookieError",
    "CookieValidationError",
    "NoSessionsAvailableError",
    "build_default_manager",
]
