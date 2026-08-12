"""
Automatic session renewal via the dwar.ru OAuth / Astrum Play login endpoint.

Flow
----
1. Parse the ``mycom`` cookie value to extract ``access_token`` (and optionally
   ``refresh_token``).
2. POST ``access_token`` + ``soc_system_id=18`` to ``/register.php`` on the
   game world server.  The server validates the token with Astrum Play and
   returns a fresh ``Set-Cookie`` header with new session cookies.
3. Inject all returned cookies into the Playwright ``BrowserContext``.
4. Verify that ``game.php`` returns HTTP 200 (no auth-error redirect).

This allows the bot to authenticate from ANY IP without requiring the user to
manually export fresh session cookies every time the old session expires.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Optional

import httpx
from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOC_SYSTEM_ID = "18"           # Astrum Play provider ID used by dwar.ru
_REGISTER_PATH = "/register.php"
_GAME_PATH = "/game.php"
_LOGIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

def extract_access_token(mycom_cookie_value: str) -> Optional[str]:
    """
    Parse ``access_token`` from the URL-encoded ``mycom`` cookie value.

    Cookie value format::
        access_token%3DXXXXX%26refresh_token%3DYYYYY
    URL-decoded::
        access_token=XXXXX&refresh_token=YYYYY
    """
    try:
        decoded = urllib.parse.unquote(mycom_cookie_value)
        params = dict(urllib.parse.parse_qsl(decoded))
        token = params.get("access_token", "").strip()
        if token:
            logger.debug("Extracted access_token (length=%d).", len(token))
            return token
        logger.warning("'access_token' not found in mycom cookie value.")
    except Exception as exc:
        logger.warning("Failed to parse mycom cookie: %s", exc)
    return None


def extract_refresh_token(mycom_cookie_value: str) -> Optional[str]:
    """Parse ``refresh_token`` from the mycom cookie value."""
    try:
        decoded = urllib.parse.unquote(mycom_cookie_value)
        params = dict(urllib.parse.parse_qsl(decoded))
        return params.get("refresh_token", "").strip() or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# OAuth login
# ---------------------------------------------------------------------------

async def oauth_login(
    world_url: str,
    access_token: str,
    timeout: float = 20.0,
) -> Optional[dict[str, str]]:
    """
    Perform the Astrum Play OAuth handshake against dwar.ru.

    Parameters
    ----------
    world_url:
        Game world base URL, e.g. ``"https://w1.dwar.ru"``.
    access_token:
        The ``access_token`` value extracted from the ``mycom`` cookie.
    timeout:
        HTTP timeout in seconds.

    Returns
    -------
    dict[str, str] or None
        Map of ``{cookie_name: cookie_value}`` set by the server on success,
        or ``None`` on failure.
    """
    register_url = world_url.rstrip("/") + _REGISTER_PATH
    headers = {**_LOGIN_HEADERS, "Referer": world_url + "/"}

    logger.info("OAuth login: POST %s (soc_system_id=%s)", register_url, _SOC_SYSTEM_ID)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            resp = await client.post(
                register_url,
                data={"soc_system_id": _SOC_SYSTEM_ID, "access_token": access_token},
                headers=headers,
            )

        if resp.status_code not in (200, 301, 302, 303):
            logger.warning(
                "OAuth login: unexpected status %d from %s.",
                resp.status_code, register_url,
            )
            return None

        new_cookies = dict(resp.cookies)
        if not new_cookies.get("sess_sid"):
            logger.warning(
                "OAuth login: no sess_sid in Set-Cookie response. "
                "Cookies received: %s",
                list(new_cookies.keys()),
            )
            return None

        logger.info(
            "OAuth login success — new sess_sid=%s, uid=%s.",
            new_cookies.get("sess_sid", "?")[:8] + "…",
            new_cookies.get("sess_uid", "?"),
        )
        return new_cookies

    except httpx.TimeoutException:
        logger.error("OAuth login timed out after %.0fs.", timeout)
    except Exception as exc:
        logger.error("OAuth login error: %s", exc, exc_info=True)
    return None


async def verify_game_session(
    world_url: str,
    session_cookies: dict[str, str],
    timeout: float = 15.0,
) -> bool:
    """
    Verify that *session_cookies* give an authenticated session on game.php.

    Returns True if game.php responds with HTTP 200 (no auth-error redirect).
    """
    game_url = world_url.rstrip("/") + _GAME_PATH
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            resp = await client.get(
                game_url,
                cookies=session_cookies,
                headers=_LOGIN_HEADERS,
            )
        if resp.status_code == 200:
            logger.info("Session verified: game.php returned 200.")
            return True
        location = resp.headers.get("location", "")
        logger.warning(
            "Session invalid: game.php → %d %s", resp.status_code, location
        )
        return False
    except Exception as exc:
        logger.warning("verify_game_session error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Playwright integration
# ---------------------------------------------------------------------------

def _build_playwright_cookies(
    session_cookies: dict[str, str],
    mycom_value: str,
    world_domain: str,
) -> list[dict]:
    """
    Convert an httpx cookie dict into a list of Playwright add_cookies() dicts.
    Always includes the persistent ``mycom`` OAuth cookie so the next renewal works.
    """
    cookies: list[dict] = []
    for name, value in session_cookies.items():
        cookies.append({
            "name": name,
            "value": value,
            "domain": f".{world_domain}" if not world_domain.startswith(".") else world_domain,
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
        })

    # Keep the mycom OAuth cookie for future re-auth
    if mycom_value and not any(c["name"] == "mycom" for c in cookies):
        cookies.append({
            "name": "mycom",
            "value": mycom_value,
            "domain": f".{world_domain}" if not world_domain.startswith(".") else world_domain,
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
        })
    return cookies


async def oauth_login_and_inject(
    context: BrowserContext,
    mycom_cookie_value: str,
    world_url: str,
    world_domain: str,
) -> bool:
    """
    Full pipeline: extract token → login → verify → inject into Playwright context.

    Parameters
    ----------
    context:
        Playwright BrowserContext to inject cookies into.
    mycom_cookie_value:
        Raw value of the ``mycom`` cookie (URL-encoded OAuth tokens).
    world_url:
        e.g. ``"https://w1.dwar.ru"``
    world_domain:
        e.g. ``"w1.dwar.ru"`` (without leading dot)

    Returns
    -------
    bool
        True on success.
    """
    access_token = extract_access_token(mycom_cookie_value)
    if not access_token:
        logger.error("Cannot perform OAuth login: no access_token in mycom cookie.")
        return False

    session_cookies = await oauth_login(world_url, access_token)
    if not session_cookies:
        logger.error("OAuth login failed — could not obtain new session cookies.")
        return False

    # Verify before injecting
    ok = await verify_game_session(world_url, session_cookies)
    if not ok:
        logger.error("New session cookies failed game.php verification.")
        return False

    playwright_cookies = _build_playwright_cookies(session_cookies, mycom_cookie_value, world_domain)
    try:
        await context.clear_cookies()
        await context.add_cookies(playwright_cookies)
        logger.info(
            "Injected %d fresh OAuth session cookies into browser context.",
            len(playwright_cookies),
        )
    except Exception as exc:
        logger.error("Failed to inject cookies into context: %s", exc)
        return False

    return True
