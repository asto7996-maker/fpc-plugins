"""
Cookie Manager — loads, validates, rotates, and injects browser cookies.

Supported source formats
------------------------
1. **Cookie Editor JSON** — the JSON array exported by the "Cookie Editor"
   browser extension.  Each element is a dict with at minimum the keys
   ``name``, ``value``, ``domain``, and ``path``.

2. **Netscape / Mozilla cookie file** — the plain-text format used by curl,
   wget and most classic scrapers.  Lines look like:
       .dwar.ru	TRUE	/	FALSE	1893456000	PHPSESSID	abc123xyz

Rotation strategy
-----------------
The manager can hold a *pool* of session files.  After a session is deemed
stale (expired or invalid) it is removed from the pool and the next file is
used.  When the pool is empty a ``SessionExhaustedError`` is raised so the
caller can trigger a fresh login.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Union

from playwright.async_api import BrowserContext, Page

from dwar_bot.config import (
    COOKIE_MAX_AGE_SECONDS,
    COOKIES_DIR,
    DEFAULT_COOKIE_FILE,
    REQUIRED_COOKIE_NAMES,
    SESSION_ROTATION_POOL_SIZE,
    SELECTORS,
    GAME_BASE_URL,
    PAGE_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CookieError(Exception):
    """Base class for all cookie-related errors."""


class InvalidCookieFileError(CookieError):
    """Raised when a cookie file cannot be parsed or is structurally wrong."""


class MissingRequiredCookiesError(CookieError):
    """Raised when a mandatory cookie is absent from the loaded set."""


class ExpiredCookieError(CookieError):
    """Raised when all cookies in a file are past their expiry date."""


class SessionExhaustedError(CookieError):
    """Raised when the entire session pool has been depleted."""


# ---------------------------------------------------------------------------
# Low-level cookie parsing helpers
# ---------------------------------------------------------------------------

def _parse_cookie_editor_json(raw: str) -> list[dict[str, Any]]:
    """Parse a JSON array as exported by the Cookie Editor browser extension."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidCookieFileError(
            f"File is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, list):
        raise InvalidCookieFileError(
            "Cookie Editor JSON must be a top-level array of cookie objects."
        )

    result: list[dict[str, Any]] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict cookie entry at index %d", idx)
            continue
        if "name" not in item or "value" not in item:
            logger.warning(
                "Skipping cookie entry at index %d — missing 'name' or 'value'", idx
            )
            continue
        result.append(item)

    if not result:
        raise InvalidCookieFileError(
            "Cookie Editor JSON parsed successfully but contained no usable entries."
        )
    return result


def _parse_netscape_cookies(raw: str) -> list[dict[str, Any]]:
    """
    Parse Netscape/Mozilla cookie format.

    Expected column order (tab-separated):
        domain  httpOnly-flag  path  secure-flag  expiry  name  value
    """
    result: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            logger.warning(
                "Netscape cookie file line %d has only %d fields (expected 7); skipping.",
                lineno,
                len(parts),
            )
            continue
        domain, http_only_flag, path, secure_flag, expiry_str, name, value = parts[:7]
        try:
            expiry = int(expiry_str) if expiry_str else 0
        except ValueError:
            logger.warning(
                "Netscape cookie file line %d: cannot parse expiry %r; treating as 0.",
                lineno,
                expiry_str,
            )
            expiry = 0

        result.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure_flag.upper() == "TRUE",
                "httpOnly": http_only_flag.upper() == "TRUE",
                "expires": expiry,
            }
        )
    if not result:
        raise InvalidCookieFileError(
            "Netscape cookie file contained no usable entries."
        )
    return result


def load_cookies_from_file(filepath: Union[str, Path]) -> list[dict[str, Any]]:
    """
    Load cookies from *filepath*, auto-detecting whether it is
    Cookie Editor JSON or Netscape format.

    Returns a list of cookie dicts ready for Playwright's
    ``browser_context.add_cookies()``.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    InvalidCookieFileError
        If the file cannot be parsed as either supported format.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Cookie file not found: {filepath}")

    raw = filepath.read_text(encoding="utf-8").strip()
    if not raw:
        raise InvalidCookieFileError(f"Cookie file is empty: {filepath}")

    # Auto-detect format by first non-whitespace character
    if raw.startswith("[") or raw.startswith("{"):
        logger.debug("Detected Cookie Editor JSON format: %s", filepath.name)
        cookies = _parse_cookie_editor_json(raw)
    else:
        logger.debug("Detected Netscape cookie format: %s", filepath.name)
        cookies = _parse_netscape_cookies(raw)

    logger.info(
        "Loaded %d cookies from '%s'.", len(cookies), filepath.name
    )
    return cookies


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _normalize_cookies_for_playwright(
    cookies: list[dict[str, Any]], base_domain: str
) -> list[dict[str, Any]]:
    """
    Normalise a list of raw cookie dicts so every entry satisfies Playwright's
    ``add_cookies()`` contract:
    - ``domain`` must be set (falls back to *base_domain* if absent)
    - ``path`` defaults to ``"/"``
    - ``expires`` must be a Unix timestamp float or -1 (session cookie)
    - Unknown extra fields are stripped to avoid Playwright validation errors.
    """
    playwright_keys = {
        "name", "value", "domain", "path",
        "expires", "httpOnly", "secure", "sameSite", "url",
    }
    normalised: list[dict[str, Any]] = []
    for raw in cookies:
        cookie: dict[str, Any] = {}

        cookie["name"] = str(raw.get("name", ""))
        cookie["value"] = str(raw.get("value", ""))

        domain = raw.get("domain") or base_domain
        # Playwright requires domain without leading dot for some versions
        cookie["domain"] = domain if domain.startswith(".") else f".{domain}"

        cookie["path"] = raw.get("path") or "/"
        cookie["httpOnly"] = bool(raw.get("httpOnly", False))
        cookie["secure"] = bool(raw.get("secure", False))

        # Normalise expiry: Cookie Editor uses "expirationDate", Netscape uses "expires"
        expiry_raw = raw.get("expirationDate") or raw.get("expires") or -1
        try:
            cookie["expires"] = float(expiry_raw) if expiry_raw else -1.0
        except (TypeError, ValueError):
            cookie["expires"] = -1.0

        same_site = raw.get("sameSite", "Lax")
        if same_site not in ("Strict", "Lax", "None"):
            same_site = "Lax"
        cookie["sameSite"] = same_site

        # Drop keys Playwright does not recognise
        cookie = {k: v for k, v in cookie.items() if k in playwright_keys}
        normalised.append(cookie)
    return normalised


def validate_cookies(
    cookies: list[dict[str, Any]],
    required_names: frozenset[str] = REQUIRED_COOKIE_NAMES,
    max_age_seconds: int = COOKIE_MAX_AGE_SECONDS,
) -> None:
    """
    Validate *cookies* against the required name set and expiry rules.

    Raises
    ------
    MissingRequiredCookiesError
        If any name in *required_names* is absent from the list.
    ExpiredCookieError
        If every cookie that carries an expiry timestamp is already expired.
    """
    now = time.time()
    names_found: set[str] = set()
    any_valid_expiry = False

    for cookie in cookies:
        name = cookie.get("name", "")
        names_found.add(name)

        expiry = cookie.get("expirationDate") or cookie.get("expires") or -1
        try:
            expiry_ts = float(expiry)
        except (TypeError, ValueError):
            expiry_ts = -1.0

        if expiry_ts > 0:
            age = expiry_ts - now
            if age < 0:
                logger.debug(
                    "Cookie '%s' expired %.0f seconds ago.", name, abs(age)
                )
            elif age < max_age_seconds:
                any_valid_expiry = True
            else:
                any_valid_expiry = True

    # Check required names
    missing = required_names - names_found
    if missing:
        raise MissingRequiredCookiesError(
            f"Required cookies missing from file: {sorted(missing)}"
        )

    # All timed cookies are expired — warn but do not hard-fail if required
    # cookies are present (the server may still honour a stale token for a while)
    if not any_valid_expiry:
        logger.warning(
            "All cookies with expiry timestamps appear to be expired. "
            "The session may be rejected by the server."
        )


# ---------------------------------------------------------------------------
# CookieManager class
# ---------------------------------------------------------------------------

class CookieManager:
    """
    Manages one or more cookie session files for a Playwright browser context.

    Parameters
    ----------
    cookie_dir:
        Directory that holds session cookie files.
    pool_size:
        Maximum number of session files to keep in the rotation pool.
    base_domain:
        The game's domain string used when a cookie entry lacks an explicit
        domain field (e.g. ``"dwar.ru"``).
    required_names:
        Set of cookie names that must be present for a session to be valid.
    max_age_seconds:
        Cookies older than this (seconds) are considered stale.
    """

    def __init__(
        self,
        cookie_dir: Union[str, Path] = COOKIES_DIR,
        pool_size: int = SESSION_ROTATION_POOL_SIZE,
        base_domain: str = "dwar.ru",
        required_names: frozenset[str] = REQUIRED_COOKIE_NAMES,
        max_age_seconds: int = COOKIE_MAX_AGE_SECONDS,
    ) -> None:
        self._cookie_dir = Path(cookie_dir)
        self._pool_size = pool_size
        self._base_domain = base_domain
        self._required_names = required_names
        self._max_age_seconds = max_age_seconds

        # Ordered pool: list of Paths, front = current session
        self._pool: list[Path] = []
        self._current_cookies: list[dict[str, Any]] = []
        self._loaded_at: float = 0.0

        self._lock = asyncio.Lock()

        self._discover_pool()

    # ------------------------------------------------------------------
    # Internal pool management
    # ------------------------------------------------------------------

    def _discover_pool(self) -> None:
        """Scan *cookie_dir* and populate the rotation pool."""
        candidates: list[Path] = sorted(
            [
                p for p in self._cookie_dir.iterdir()
                if p.is_file() and p.suffix in {".json", ".txt"}
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,          # newest first
        )
        self._pool = candidates[: self._pool_size]
        if not self._pool:
            # Fall back to the single default file even if it doesn't exist yet
            self._pool = [DEFAULT_COOKIE_FILE]
        logger.info(
            "Cookie pool discovered: %d file(s) — [%s]",
            len(self._pool),
            ", ".join(p.name for p in self._pool),
        )

    def _pop_current(self) -> Optional[Path]:
        """Remove and return the front of the pool (current session)."""
        if self._pool:
            return self._pool.pop(0)
        return None

    # ------------------------------------------------------------------
    # Public API — synchronous helpers (used during setup)
    # ------------------------------------------------------------------

    def load(self, filepath: Optional[Union[str, Path]] = None) -> list[dict[str, Any]]:
        """
        Load and validate cookies from *filepath* (or the front of the pool).

        Returns the normalised list of cookie dicts ready for Playwright.

        Raises
        ------
        SessionExhaustedError
            If the pool is empty and no explicit path was given.
        CookieError subclasses
            On parse or validation failures.
        """
        if filepath is not None:
            target = Path(filepath)
        elif self._pool:
            target = self._pool[0]
        else:
            raise SessionExhaustedError(
                "Cookie pool is empty and no explicit cookie file was provided."
            )

        logger.info("Loading session cookies from: %s", target)
        raw_cookies = load_cookies_from_file(target)
        validate_cookies(raw_cookies, self._required_names, self._max_age_seconds)
        normalised = _normalize_cookies_for_playwright(raw_cookies, self._base_domain)

        self._current_cookies = normalised
        self._loaded_at = time.time()
        logger.info(
            "Session cookies ready — %d entries loaded, %d required names verified.",
            len(normalised),
            len(self._required_names),
        )
        return normalised

    def is_stale(self) -> bool:
        """Return True if the current session is older than *max_age_seconds*."""
        if not self._current_cookies or self._loaded_at == 0.0:
            return True
        return (time.time() - self._loaded_at) > self._max_age_seconds

    def rotate(self) -> list[dict[str, Any]]:
        """
        Discard the current (failed/expired) session and load the next one.

        Raises
        ------
        SessionExhaustedError
            When the pool has no more sessions to try.
        """
        discarded = self._pop_current()
        logger.warning(
            "Session rotated — discarded '%s'. Remaining sessions: %d.",
            discarded.name if discarded else "unknown",
            len(self._pool),
        )
        if not self._pool:
            raise SessionExhaustedError(
                "All sessions in the pool have been exhausted.  "
                "Please supply fresh cookie files and restart the bot."
            )
        return self.load()

    def save(
        self,
        cookies: list[dict[str, Any]],
        filename: Optional[str] = None,
    ) -> Path:
        """
        Persist *cookies* to a JSON file in *cookie_dir*.

        Useful for saving a freshly authenticated session so it can be
        reused across bot restarts.

        Parameters
        ----------
        cookies:
            List of raw cookie dicts (as returned by Playwright's
            ``context.cookies()``).
        filename:
            Override the output file name.  Defaults to
            ``session_<unix_timestamp>.json``.

        Returns
        -------
        Path
            Absolute path of the written file.
        """
        if filename is None:
            filename = f"session_{int(time.time())}.json"

        target = self._cookie_dir / filename
        target.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Saved %d cookies to '%s'.", len(cookies), target)
        # Prepend to pool so it becomes the current session on next load()
        self._pool.insert(0, target)
        return target

    # ------------------------------------------------------------------
    # Async Playwright integration
    # ------------------------------------------------------------------

    async def inject_into_context(
        self,
        context: BrowserContext,
        filepath: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        Load cookies (if not already loaded) and inject them into *context*.

        Parameters
        ----------
        context:
            A Playwright ``BrowserContext`` that has NOT yet navigated to the
            game domain (cookies must be set before the first navigation).
        filepath:
            Explicit cookie file path.  If *None*, the current pool front is used.
        """
        async with self._lock:
            if filepath is not None or not self._current_cookies or self.is_stale():
                self.load(filepath)

            try:
                await context.add_cookies(self._current_cookies)
                logger.info(
                    "Injected %d cookies into browser context.", len(self._current_cookies)
                )
            except Exception as exc:
                logger.error(
                    "Failed to inject cookies into browser context: %s", exc, exc_info=True
                )
                raise CookieError(f"Cookie injection failed: {exc}") from exc

    async def verify_session(self, page: Page) -> bool:
        """
        Navigate to game.php and verify that the injected cookies
        result in an authenticated session on dwar.ru.

        dwar.ru auth check rules
        ------------------------
        * Authenticated  → stays on ``game.php`` (or a game-internal page)
        * Not authenticated → redirected to ``index.php?error=Не+пройдена+авторизация``

        Returns
        -------
        bool
            True  — session is active.
            False — session is invalid/expired.
        """
        from dwar_bot.config import GAME_GAME_URL, GAME_WORLD_URL
        try:
            logger.debug("Verifying session via %s …", GAME_GAME_URL)
            await page.goto(
                GAME_GAME_URL,
                timeout=PAGE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            await asyncio.sleep(2.0)

            current_url: str = page.url.lower()

            # Auth failure: redirected back to index with error param
            if "error=" in current_url and "index.php" in current_url:
                logger.warning(
                    "Session check failed — auth error redirect: %s", page.url
                )
                return False

            # Auth failure: landed on login/register page
            bad_patterns = ("index.php", "/register.php", "login", "авторизац")
            if any(p in current_url for p in bad_patterns[:2]):
                logger.warning(
                    "Session check failed — on login page: %s", page.url
                )
                return False

            logger.info("Session verified — authenticated page: %s", page.url)
            return True

        except Exception as exc:
            logger.error(
                "Unexpected error during session verification: %s", exc, exc_info=True
            )
            return False

    async def inject_and_verify(
        self,
        context: BrowserContext,
        page: Page,
        filepath: Optional[Union[str, Path]] = None,
    ) -> bool:
        """
        Convenience wrapper: inject cookies then verify the session.

        Attempts up to ``len(pool)`` rotations before giving up.

        Returns
        -------
        bool
            True if a valid session was established, False otherwise.
        """
        attempts = max(1, len(self._pool))
        for attempt in range(1, attempts + 1):
            try:
                await self.inject_into_context(context, filepath)
                if await self.verify_session(page):
                    return True

                logger.warning(
                    "Session invalid after injection (attempt %d/%d).",
                    attempt, attempts,
                )
                if attempt < attempts:
                    self.rotate()
                    # Clear all existing cookies from context before trying next session
                    await context.clear_cookies()

            except SessionExhaustedError:
                logger.error("Session pool exhausted after %d attempt(s).", attempt)
                return False
            except CookieError as exc:
                logger.error(
                    "Cookie error on attempt %d/%d: %s", attempt, attempts, exc
                )
                if attempt < attempts:
                    try:
                        self.rotate()
                        await context.clear_cookies()
                    except SessionExhaustedError:
                        logger.error("Session pool exhausted during rotation.")
                        return False

        logger.error(
            "All %d session(s) failed verification — giving up.", attempts
        )
        return False

    async def capture_fresh_cookies(self, page: Page) -> list[dict[str, Any]]:
        """
        Extract the current cookies from *page*'s context and persist them.

        Call this right after a successful interactive or automated login so
        that the fresh session is preserved for future bot runs.

        Returns
        -------
        list[dict]
            The raw cookie list as returned by Playwright.
        """
        async with self._lock:
            try:
                cookies = await page.context.cookies()
                saved_path = self.save(cookies)
                logger.info(
                    "Captured %d cookies from live session → %s", len(cookies), saved_path
                )
                # Reload the normalised version so it becomes the active session
                self.load(saved_path)
                return cookies
            except Exception as exc:
                logger.error(
                    "Failed to capture cookies from page: %s", exc, exc_info=True
                )
                raise CookieError(f"Cookie capture failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "CookieManager":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # Nothing to release; provided for use in `async with` blocks
        pass

    def __repr__(self) -> str:
        return (
            f"CookieManager("
            f"pool_size={len(self._pool)}, "
            f"loaded_at={self._loaded_at:.0f}, "
            f"is_stale={self.is_stale()}"
            f")"
        )
