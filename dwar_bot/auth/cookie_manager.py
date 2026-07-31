from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from dwar_bot.config import CONFIG, BotConfig

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page
else:
    BrowserContext = Any  # type: ignore[misc,assignment]
    Page = Any  # type: ignore[misc,assignment]

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover - fallback for environments without Playwright
    class PlaywrightTimeoutError(Exception):
        """Fallback TimeoutError when Playwright is unavailable."""


class CookieManagerError(RuntimeError):
    """Base exception for cookie manager errors."""


class CookieParseError(CookieManagerError):
    """Raised when cookie file cannot be parsed."""


class CookieValidationError(CookieManagerError):
    """Raised when cookie payload is invalid for runtime usage."""


class NoValidCookieSessionsError(CookieManagerError):
    """Raised when there are no valid cookie sessions available."""


class CookieFileFormat(str, Enum):
    JSON = "json"
    NETSCAPE = "netscape"


@dataclass(slots=True)
class CookieSession:
    source: Path
    cookies: list[dict[str, Any]]
    loaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    failures: int = 0


class CookieManager:
    def __init__(self, config: BotConfig = CONFIG, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("dwar_bot.cookie_manager")
        self._sessions: list[CookieSession] = []
        self._rotation_index = 0

    def refresh_sessions(self) -> list[CookieSession]:
        sources = self._discover_cookie_sources()
        if not sources:
            raise NoValidCookieSessionsError(
                f"No cookie sources found. Checked directory: {self.config.cookies.cookie_dir}"
            )

        sessions: list[CookieSession] = []
        for source in sources:
            try:
                cookies = self._load_cookie_file(source)
                validated = self._validate_session(cookies, source)
                sessions.append(CookieSession(source=source, cookies=validated))
                self.logger.info("Loaded valid cookie session from %s (%d cookies).", source, len(validated))
            except CookieManagerError as exc:
                self.logger.warning("Skipping cookie source %s: %s", source, exc)
            except Exception as exc:
                self.logger.exception("Unexpected error while loading cookies from %s: %s", source, exc)

        if not sessions:
            raise NoValidCookieSessionsError("No valid cookie sessions available after validation.")

        random.shuffle(sessions)
        self._sessions = sessions
        self._rotation_index = 0
        return sessions

    def get_next_session(self) -> CookieSession:
        if not self._sessions:
            self.refresh_sessions()

        attempts = len(self._sessions)
        for _ in range(attempts):
            session = self._sessions[self._rotation_index % len(self._sessions)]
            self._rotation_index += 1
            if session.failures < self.config.cookies.max_session_failures:
                return session

        raise NoValidCookieSessionsError(
            "All cookie sessions reached failure limit. Refresh sessions or provide new cookie files."
        )

    def mark_session_failed(self, session: CookieSession, reason: str) -> None:
        session.failures += 1
        self.logger.warning(
            "Cookie session %s marked as failed (%d/%d). Reason: %s",
            session.source,
            session.failures,
            self.config.cookies.max_session_failures,
            reason,
        )

    def mark_session_success(self, session: CookieSession) -> None:
        if session.failures > 0:
            self.logger.info("Cookie session %s recovered after %d failures.", session.source, session.failures)
        session.failures = 0

    async def apply_session_to_context(
        self,
        context: BrowserContext,
        session: CookieSession | None = None,
        clear_existing: bool = True,
    ) -> CookieSession:
        selected_session = session or self.get_next_session()
        try:
            await asyncio.sleep(random.uniform(
                self.config.delays.cookie_apply.min_seconds,
                self.config.delays.cookie_apply.max_seconds,
            ))
            if clear_existing:
                await context.clear_cookies()
            await context.add_cookies(selected_session.cookies)
            self.logger.info(
                "Applied %d cookies from %s to browser context.",
                len(selected_session.cookies),
                selected_session.source,
            )
            return selected_session
        except Exception as exc:
            self.mark_session_failed(selected_session, f"apply_session_to_context failed: {exc}")
            self.logger.exception("Failed to apply cookies to browser context.")
            raise CookieManagerError(f"Unable to apply cookies from {selected_session.source}") from exc

    async def verify_session(
        self,
        page: Page,
        session: CookieSession,
        success_selector: str | None = None,
        check_url: str | None = None,
    ) -> bool:
        url = check_url or self.config.cookies.session_validation_url
        selector = success_selector or self.config.selectors.auth_success_selector

        try:
            await asyncio.sleep(random.uniform(
                self.config.delays.session_verify.min_seconds,
                self.config.delays.session_verify.max_seconds,
            ))
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.config.browser.navigation_timeout_ms,
            )
            await page.wait_for_selector(selector, timeout=self.config.cookies.session_validation_timeout_ms)
            self.mark_session_success(session)
            self.logger.info("Session %s verified successfully via selector %s.", session.source, selector)
            return True
        except PlaywrightTimeoutError:
            self.mark_session_failed(session, f"verification timeout for selector {selector!r}")
            self.logger.warning("Session verification timed out for %s.", session.source)
            return False
        except Exception as exc:
            self.mark_session_failed(session, f"verification exception: {exc}")
            self.logger.exception("Unexpected error while verifying cookie session %s.", session.source)
            return False

    def _discover_cookie_sources(self) -> list[Path]:
        discovered: list[Path] = []
        configured_files = self.config.cookies.cookie_files
        if configured_files:
            for path in configured_files:
                if path.is_file():
                    discovered.append(path.resolve())
                else:
                    self.logger.warning("Configured cookie file does not exist: %s", path)
        else:
            for pattern in ("*.json", "*.txt", "*.cookies", "*.cookie"):
                discovered.extend(path.resolve() for path in self.config.cookies.cookie_dir.glob(pattern) if path.is_file())

        unique_paths = sorted(set(discovered))
        return unique_paths

    def _load_cookie_file(self, source: Path) -> list[dict[str, Any]]:
        try:
            raw_text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw_text = source.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CookieParseError(f"Unable to read file {source}: {exc}") from exc

        payload = raw_text.strip()
        if not payload:
            raise CookieParseError(f"Cookie file {source} is empty.")

        file_format = self._detect_format(source, payload)
        if file_format is CookieFileFormat.JSON:
            raw_cookies = self._parse_json_payload(payload, source)
        else:
            raw_cookies = self._parse_netscape_payload(payload, source)

        normalized = [self._normalize_cookie(cookie, source) for cookie in raw_cookies]
        return normalized

    def _detect_format(self, source: Path, payload: str) -> CookieFileFormat:
        suffix = source.suffix.lower()
        if suffix == ".json":
            return CookieFileFormat.JSON
        if payload.startswith("{") or payload.startswith("["):
            return CookieFileFormat.JSON
        return CookieFileFormat.NETSCAPE

    def _parse_json_payload(self, payload: str, source: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CookieParseError(f"Invalid JSON format in {source}: {exc}") from exc

        if isinstance(data, dict):
            if isinstance(data.get("cookies"), list):
                cookies = data["cookies"]
            elif "name" in data and "value" in data:
                cookies = [data]
            else:
                raise CookieParseError(
                    f"JSON cookie format is unsupported in {source}. Expected list or object with 'cookies'."
                )
        elif isinstance(data, list):
            cookies = data
        else:
            raise CookieParseError(f"Unsupported JSON cookie root type in {source}: {type(data)!r}")

        parsed: list[dict[str, Any]] = []
        for index, item in enumerate(cookies):
            if not isinstance(item, dict):
                raise CookieParseError(f"Cookie at index {index} in {source} must be object, got {type(item)!r}")
            parsed.append(item)
        return parsed

    def _parse_netscape_payload(self, payload: str, source: Path) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        for line_no, line in enumerate(payload.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
                continue

            parts = line.split("\t")
            if len(parts) != 7:
                parts = stripped.split()
            if len(parts) != 7:
                raise CookieParseError(
                    f"Invalid Netscape cookie row in {source}:{line_no}. Expected 7 columns, got {len(parts)}."
                )

            domain, _include_subdomains, path, secure, expires, name, value = parts
            http_only = False
            if domain.startswith("#HttpOnly_"):
                http_only = True
                domain = domain.replace("#HttpOnly_", "", 1)

            try:
                expires_int = int(expires)
            except ValueError as exc:
                raise CookieParseError(
                    f"Invalid expires value in {source}:{line_no}. Expected unix timestamp, got {expires!r}."
                ) from exc

            parsed.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path or "/",
                    "secure": secure.upper() == "TRUE",
                    "httpOnly": http_only,
                    "expires": expires_int,
                }
            )
        return parsed

    def _normalize_cookie(self, cookie: dict[str, Any], source: Path) -> dict[str, Any]:
        name = str(cookie.get("name", "")).strip()
        if not name:
            raise CookieValidationError(f"Cookie in {source} is missing required field 'name'.")

        if "value" not in cookie:
            raise CookieValidationError(f"Cookie {name!r} in {source} is missing required field 'value'.")
        value = str(cookie.get("value", ""))

        domain = str(cookie.get("domain", "")).strip()
        if not domain:
            raw_url = str(cookie.get("url", "")).strip()
            if raw_url:
                domain = urlparse(raw_url).hostname or ""
        if not domain:
            raise CookieValidationError(f"Cookie {name!r} in {source} has no domain.")

        path = str(cookie.get("path", "/")).strip() or "/"
        if not path.startswith("/"):
            path = f"/{path}"

        expires_raw = cookie.get("expires", cookie.get("expirationDate", cookie.get("expiry", -1)))
        expires = self._normalize_expires(expires_raw)

        same_site_raw = str(cookie.get("sameSite", cookie.get("same_site", "Lax"))).strip().lower()
        same_site_map = {"strict": "Strict", "lax": "Lax", "none": "None"}
        same_site = same_site_map.get(same_site_raw, "Lax")

        normalized = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "httpOnly": bool(cookie.get("httpOnly", cookie.get("httponly", False))),
            "secure": bool(cookie.get("secure", False)),
            "sameSite": same_site,
            "expires": expires,
        }

        self._validate_cookie(normalized, source)
        return normalized

    @staticmethod
    def _normalize_expires(raw_value: Any) -> int:
        if raw_value in (None, "", 0, "0", -1, "-1"):
            return -1
        try:
            numeric = int(float(raw_value))
        except (TypeError, ValueError):
            return -1
        if numeric <= 0:
            return -1
        return numeric

    def _validate_cookie(self, cookie: dict[str, Any], source: Path) -> None:
        domain_clean = cookie["domain"].lstrip(".").lower()
        allowed_domains = self.config.cookies.allowed_domains
        if allowed_domains:
            matches_allowed = any(
                domain_clean == allowed or domain_clean.endswith(f".{allowed}")
                for allowed in allowed_domains
            )
            if not matches_allowed:
                raise CookieValidationError(
                    f"Cookie {cookie['name']!r} from {source} has forbidden domain {cookie['domain']!r}."
                )

        expires = int(cookie.get("expires", -1))
        if expires > 0:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            if expires <= now_ts:
                raise CookieValidationError(f"Cookie {cookie['name']!r} from {source} is already expired.")

    def _validate_session(self, cookies: list[dict[str, Any]], source: Path) -> list[dict[str, Any]]:
        if not cookies:
            raise CookieValidationError(f"No cookies loaded from {source}.")

        deduped_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        for cookie in cookies:
            key = (
                cookie["name"].lower(),
                cookie["domain"].lower(),
                cookie["path"],
            )
            deduped_map[key] = cookie

        validated = list(deduped_map.values())
        if len(validated) < self.config.cookies.min_cookies_per_session:
            raise CookieValidationError(
                f"Cookie session from {source} has {len(validated)} cookies, "
                f"expected at least {self.config.cookies.min_cookies_per_session}."
            )

        cookie_name_set = {cookie["name"].lower() for cookie in validated}
        missing = [
            required_name
            for required_name in self.config.cookies.required_cookie_names
            if required_name.lower() not in cookie_name_set
        ]
        if missing:
            raise CookieValidationError(
                f"Cookie session from {source} is missing required cookies: {', '.join(missing)}."
            )
        return validated

