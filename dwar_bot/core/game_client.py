"""
Pure HTTP game client for dwar.ru — no Flash, no Playwright.

All game interactions go through:
  * POST /entry_point.php?object=X&action=Y&json_mode_on=1  → JSON
  * GET  /user.php / area.php / hunt_conf.php / npc.php
  * POST /register.php  → OAuth session create (only when needed)

Session rules
-------------
* Prefer cookies already stored on disk (sess_sid / sess_uid / sess_crc / mycom).
* Renew via OAuth **only** when the session is missing or the server rejects auth.
* Never rotate on a timer — that was wiping good sessions every 20 minutes.
* All renewals are serialized through an asyncio.Lock (no parallel renew storms).
* After a successful renew, sess_* cookies are persisted back to the cookie file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from dwar_bot.config import COOKIES_DIR, DEFAULT_COOKIE_FILE, DELAY_RETRY, MAX_RETRIES

logger = logging.getLogger(__name__)


class TokenExpiredError(Exception):
    """Raised when the OAuth access_token is expired and needs manual renewal."""


class AuthRequiredError(Exception):
    """Raised when the game session is invalid but a renew may still succeed."""


_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

STATUS_OK = 100
STATUS_ERROR = 1

# Cookie names that form a playable game session
_SESSION_COOKIE_NAMES = (
    "sess_sid", "sess_uid", "sess_crc", "sess_nn",
    "mycom", "cid", "cidc", "sstype", "mr1lad",
    "forceHtml5", "domain_sid",
)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class GameState:
    area_id: str = "0"
    level: int = 0
    kind: int = 0
    money: float = 0.0
    money_gold: float = 0.0
    money_silver: float = 0.0
    server_time: int = 0
    party: int = 0
    clan: int = 0
    flags: int = 0
    flags2: int = 0
    flags3: int = 0
    fight_id: int = 0


@dataclass
class CharStats:
    nick: str = ""
    level: int = 0
    hp: int = 0
    hp_max: int = 0
    mp: int = 0
    mp_max: int = 0
    kind: int = 0
    lc_id: str = ""
    online: int = 1
    avail: int = 0

    @property
    def hp_percent(self) -> float:
        return (self.hp / self.hp_max * 100) if self.hp_max else 0.0

    @property
    def mp_percent(self) -> float:
        return (self.mp / self.mp_max * 100) if self.mp_max else 0.0


@dataclass
class AreaItem:
    item_id: str = ""
    name: str = ""
    item_type: str = ""
    code: str = ""
    href: str = ""
    area_id: str = ""
    npc_id: str = ""
    link_id: str = ""
    f_id: str = ""
    action_id: str = ""
    object_id: str = ""
    object_class: str = ""
    link_href: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class AreaInfo:
    area_id: str = "0"
    title: str = ""
    items: list[AreaItem] = field(default_factory=list)
    npc_id: int = 0
    action_id: int = 0
    fight_count: int = 0


@dataclass
class ApiResponse:
    status: int = 0
    error: str = ""
    data: dict = field(default_factory=dict)
    redirect_url: Optional[str] = None
    redirect_error: Any = None
    bonus_text: list = field(default_factory=list)
    macros: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def loot_lines(self) -> list[str]:
        """Human-readable bonus / artifact lines from the response."""
        lines: list[str] = []
        for b in self.bonus_text or []:
            if isinstance(b, str) and b.strip():
                lines.append(b.strip())
            elif isinstance(b, dict):
                t = b.get("text") or b.get("title") or b.get("bonus_text") or ""
                if t:
                    lines.append(str(t).strip())
        for m in self.macros or []:
            if not isinstance(m, dict):
                continue
            kind = str(m.get("type") or m.get("name") or m.get("macro") or "").upper()
            title = m.get("title") or m.get("artikul_title") or m.get("text") or ""
            artikul = m.get("artikul") or m.get("art_id") or m.get("id") or ""
            if "ARTIFACT" in kind or title:
                label = str(title or artikul or kind).strip()
                if label:
                    lines.append(f"ARTIFACT: {label}")
        return lines


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def _cookies_from_editor_list(data: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in data:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        out[name] = str(item.get("value", ""))
    return out


def load_cookie_dict(path: Optional[Path] = None) -> dict[str, str]:
    """Load name→value map from Cookie Editor JSON (or newest file in cookies/)."""
    target = Path(path) if path else None
    if target is None:
        files = sorted(
            list(COOKIES_DIR.glob("*.json")) + list(COOKIES_DIR.glob("*.txt")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return {}
        target = files[0]

    raw = target.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if isinstance(data, list):
        return _cookies_from_editor_list(data)
    if isinstance(data, dict) and "cookies" in data:
        return _cookies_from_editor_list(data["cookies"])
    if isinstance(data, dict):
        # Already a flat name→value map
        return {str(k): str(v) for k, v in data.items()}
    return {}


def persist_session_cookies(
    session: dict[str, str],
    path: Optional[Path] = None,
    base_cookies: Optional[list[dict]] = None,
) -> Path:
    """
    Merge live session cookies into the Cookie Editor JSON file so restarts
    keep a working sess_sid without an immediate OAuth renew.
    """
    target = Path(path) if path else DEFAULT_COOKIE_FILE
    existing: list[dict] = []
    if base_cookies is not None:
        existing = list(base_cookies)
    elif target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except Exception:
            existing = []

    by_name = {str(c.get("name")): c for c in existing if isinstance(c, dict)}
    now = time.time()
    for name, value in session.items():
        if name not in _SESSION_COOKIE_NAMES and not name.startswith("sess_"):
            # Keep analytics cookies from the export; only upsert session-ish ones
            if name not in by_name:
                continue
        domain = ".w1.dwar.ru" if name.startswith(("sess_", "mycom", "cid", "sstype", "mr1")) else ".dwar.ru"
        if name in ("domain_sid", "forceHtml5", "flash_version", "sess_area_id", "sess_location"):
            domain = "w1.dwar.ru"
        entry = by_name.get(name, {
            "name": name,
            "domain": domain,
            "path": "/",
            "httpOnly": name.startswith("sess_") or name in ("mycom", "cid", "cidc"),
            "secure": False,
            "session": False,
            "storeId": "0",
        })
        entry["name"] = name
        entry["value"] = value
        entry["expirationDate"] = now + 86400 * 30
        by_name[name] = entry

    merged = list(by_name.values())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Persisted %d cookies → %s", len(merged), target)
    return target


# ---------------------------------------------------------------------------
# DwarGameClient
# ---------------------------------------------------------------------------

class DwarGameClient:
    """
    Stateful HTTP client for dwar.ru game interactions.
    """

    def __init__(
        self,
        world_url: str,
        access_token: str = "",
        mycom_cookie_value: str = "",
        cookie_file: Optional[Path] = None,
        initial_cookies: Optional[dict[str, str]] = None,
    ) -> None:
        self._world_url = world_url.rstrip("/")
        self._access_token = access_token
        self._mycom_value = mycom_cookie_value
        self._cookie_file = Path(cookie_file) if cookie_file else DEFAULT_COOKIE_FILE
        self._session: dict[str, str] = dict(initial_cookies or {})
        self._session_renewed_at: float = time.time() if self._session.get("sess_sid") else 0.0
        self._headers = {**_BASE_HEADERS, "Referer": f"{world_url}/game.php"}
        self._lock = asyncio.Lock()
        self._auth_blocked = False
        self._cookie_mtime: float = 0.0
        self._http: Optional[httpx.AsyncClient] = None

        if self._session.get("mycom") and not self._mycom_value:
            self._mycom_value = self._session["mycom"]
        if self._mycom_value and "mycom" not in self._session:
            self._session["mycom"] = self._mycom_value

        try:
            if self._cookie_file.exists():
                self._cookie_mtime = self._cookie_file.stat().st_mtime
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Shared HTTP client
    # ------------------------------------------------------------------

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(follow_redirects=False, timeout=20.0)
        return self._http

    async def aclose(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @property
    def auth_blocked(self) -> bool:
        return self._auth_blocked

    def unblock_auth(self) -> None:
        self._auth_blocked = False

    def apply_cookies(self, cookies: dict[str, str], *, mark_fresh: bool = True) -> None:
        """Replace / merge live session cookies (e.g. after Telegram paste)."""
        self._session.update({k: v for k, v in cookies.items() if v})
        if cookies.get("mycom"):
            self._mycom_value = cookies["mycom"]
            from dwar_bot.auth.oauth_login import extract_access_token
            tok = extract_access_token(cookies["mycom"])
            if tok:
                self._access_token = tok
        if mark_fresh and self._session.get("sess_sid"):
            self._session_renewed_at = time.time()
            self._auth_blocked = False
        logger.info(
            "Applied cookies — sess_uid=%s sess_sid=%s… mycom=%s",
            self._session.get("sess_uid", "?"),
            (self._session.get("sess_sid") or "")[:8],
            "yes" if self._session.get("mycom") else "no",
        )

    def load_cookies_from_disk(self) -> bool:
        """Reload cookies from the cookie file if present. Returns True if sess_sid loaded."""
        try:
            cookies = load_cookie_dict(self._cookie_file if self._cookie_file.exists() else None)
            if not cookies:
                return False
            self.apply_cookies(cookies, mark_fresh=bool(cookies.get("sess_sid")))
            if self._cookie_file.exists():
                self._cookie_mtime = self._cookie_file.stat().st_mtime
            return bool(self._session.get("sess_sid"))
        except Exception as exc:
            logger.warning("load_cookies_from_disk failed: %s", exc)
            return False

    async def maybe_reload_cookie_file(self) -> bool:
        """If the cookie file mtime changed, reload it. Returns True when reloaded."""
        try:
            files = sorted(
                list(COOKIES_DIR.glob("*.json")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not files:
                return False
            newest = files[0]
            mtime = newest.stat().st_mtime
            if mtime <= self._cookie_mtime:
                return False
            self._cookie_file = newest
            self._cookie_mtime = mtime
            cookies = load_cookie_dict(newest)
            if not cookies:
                return False
            old_token = self._access_token
            self.apply_cookies(cookies, mark_fresh=True)
            # Force OAuth renew if we got a new mycom but no sess_sid
            if cookies.get("mycom") and not cookies.get("sess_sid"):
                self._session.pop("sess_sid", None)
                self._session.pop("sess_uid", None)
                self._session.pop("sess_crc", None)
                self._session_renewed_at = 0.0
            elif cookies.get("mycom") and self._access_token != old_token:
                # New token — keep sess_* if present, but clear auth block
                self._auth_blocked = False
            logger.info("Cookie file changed (%s) — session reloaded.", newest.name)
            return True
        except Exception as exc:
            logger.debug("maybe_reload_cookie_file: %s", exc)
            return False

    async def ensure_session(self) -> None:
        """
        Ensure we have a usable sess_sid.

        Does **not** renew on age. Only renews when sess_sid is missing
        and we are not in an auth-blocked state.
        """
        if self._auth_blocked:
            raise TokenExpiredError("OAuth access_token expired — waiting for fresh cookies.")

        await self.maybe_reload_cookie_file()

        if self._session.get("sess_sid"):
            return

        await self._renew_session()

    def _is_token_redirect(self, location: str) -> bool:
        loc = (location or "").lower()
        return (
            "soc_auth.php" in loc
            or "vkplay.ru" in loc
            or "account.vkplay" in loc
            or "oauth.vk" in loc
            or "login" in loc and "register" in loc
        )

    def _is_auth_failure_response(self, resp: httpx.Response) -> bool:
        """Detect redirects / bodies that mean the session is dead."""
        location = resp.headers.get("location", "")
        if self._is_token_redirect(location):
            return True
        low_loc = location.lower()
        if "index.php" in low_loc and "error=" in low_loc:
            return True
        if resp.status_code in (301, 302, 303):
            # Any bounce to the portal / register while we expected game JSON/HTML
            if any(x in low_loc for x in ("register.php", "index.php", "soc_auth", "vkplay")):
                return True
            return False

        # 200 responses: only flag obvious auth-error HTML, never JSON API payloads
        ctype = (resp.headers.get("content-type") or "").lower()
        text_head = (resp.text or "")[:500]
        if "json" in ctype or text_head.lstrip().startswith("{") or text_head.lstrip().startswith("["):
            # JSON may still signal auth death via missing state + error text
            low = text_head.lower()
            if "не пройдена авторизация" in low:
                return True
            return False

        low = text_head.lower()
        if "не пройдена авторизация" in low:
            return True
        if "error=" in low and "авториз" in low:
            return True
        return False

    async def _renew_session(self) -> None:
        """POST register.php with access_token — serialized, single-flight."""
        async with self._lock:
            # Another waiter may have finished renewing while we queued
            if self._session.get("sess_sid") and not self._auth_blocked:
                return
            if self._auth_blocked:
                raise TokenExpiredError("OAuth access_token expired — waiting for fresh cookies.")

            if not self._access_token and self._mycom_value:
                from dwar_bot.auth.oauth_login import extract_access_token
                self._access_token = extract_access_token(self._mycom_value) or ""

            if not self._access_token:
                self._auth_blocked = True
                raise TokenExpiredError("No access_token available for OAuth renew.")

            url = f"{self._world_url}/register.php"
            logger.info("Renewing OAuth session via %s …", url)
            client = await self._client()
            r = await client.post(
                url,
                data={"soc_system_id": "18", "access_token": self._access_token},
                headers={
                    "User-Agent": _BASE_HEADERS["User-Agent"],
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{self._world_url}/",
                },
            )

            location = r.headers.get("location", "")
            new_cookies = dict(r.cookies)

            if self._is_token_redirect(location) or not new_cookies.get("sess_sid"):
                self._auth_blocked = True
                self._session.pop("sess_sid", None)
                raise TokenExpiredError(
                    f"OAuth access_token has expired. The bot needs fresh cookies.\n"
                    f"Redirect destination: {location}"
                )

            # Keep long-lived identity cookies alongside fresh sess_*
            merged = dict(self._session)
            merged.update(new_cookies)
            if self._mycom_value:
                merged["mycom"] = self._mycom_value
            merged.setdefault("forceHtml5", "1")
            self._session = merged
            self._session_renewed_at = time.time()
            self._auth_blocked = False

            try:
                persist_session_cookies(self._session, self._cookie_file)
                if self._cookie_file.exists():
                    # Avoid treating our own write as an external cookie update
                    self._cookie_mtime = self._cookie_file.stat().st_mtime
            except Exception as exc:
                logger.warning("Could not persist renewed session cookies: %s", exc)

            logger.info(
                "Session renewed — sess_uid=%s sess_sid=%s…",
                new_cookies.get("sess_uid", "?"),
                new_cookies.get("sess_sid", "")[:8],
            )

    async def invalidate_session(self, reason: str = "") -> None:
        """Drop sess_* so the next call renews (does not set auth_blocked)."""
        async with self._lock:
            for k in ("sess_sid", "sess_uid", "sess_crc", "sess_nn"):
                self._session.pop(k, None)
            self._session_renewed_at = 0.0
            logger.warning("Session invalidated%s", f": {reason}" if reason else "")

    # ------------------------------------------------------------------
    # Core HTTP helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: float = 15.0,
        allow_renew: bool = True,
    ) -> httpx.Response:
        await self.ensure_session()
        url = path if path.startswith("http") else f"{self._world_url}{path}"
        client = await self._client()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if method == "POST":
                    resp = await client.post(
                        url, data=data or {}, headers=self._headers,
                        cookies=self._session, timeout=timeout,
                    )
                else:
                    resp = await client.get(
                        url, params=params,
                        headers={**self._headers, "Content-Type": "text/html"},
                        cookies=self._session, timeout=timeout,
                    )

                # Absorb any refreshed cookies the server set
                if resp.cookies:
                    self._session.update(dict(resp.cookies))

                if allow_renew and self._is_auth_failure_response(resp):
                    logger.warning(
                        "Auth failure on %s %s (status=%s loc=%s) — renewing.",
                        method, path, resp.status_code, resp.headers.get("location", ""),
                    )
                    await self.invalidate_session("auth failure response")
                    await self._renew_session()
                    continue

                return resp

            except TokenExpiredError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == MAX_RETRIES:
                    raise
                wait = DELAY_RETRY.min * (2 ** (attempt - 1))
                logger.warning(
                    "%s %s failed (%s), retry %d in %.1fs",
                    method, path, exc, attempt, wait,
                )
                await asyncio.sleep(wait)

        raise RuntimeError(f"{method} {path} failed after all retries")

    async def _post(self, path: str, data: dict, timeout: float = 15.0) -> httpx.Response:
        return await self._request("POST", path, data=data, timeout=timeout)

    async def _get(
        self, path: str, params: Optional[dict] = None, timeout: float = 15.0
    ) -> httpx.Response:
        return await self._request("GET", path, params=params, timeout=timeout)

    # ------------------------------------------------------------------
    # entry_point.php API
    # ------------------------------------------------------------------

    async def entry_point(
        self, obj: str, action: str, extra: Optional[dict] = None
    ) -> ApiResponse:
        params = {"json_mode_on": "1", "object": obj, "action": action}
        if extra:
            params.update({k: str(v) for k, v in extra.items() if v is not None})

        path = f"/entry_point.php?object={obj}&action={action}&json_mode_on=1"
        try:
            resp = await self._post(path, params)
            if not resp.text or resp.text.strip() in ("", "null"):
                return ApiResponse(status=0, error="empty response")
            data = resp.json()
        except TokenExpiredError:
            raise
        except Exception as exc:
            logger.error("entry_point(%s|%s) error: %s", obj, action, exc)
            return ApiResponse(status=0, error=str(exc))

        key = f"{obj}|{action}"
        inner = data.get(key, {}) if isinstance(data, dict) else {}
        if not isinstance(inner, dict):
            inner = {}

        # Session death signal: missing state + auth-ish error
        state = data.get("state", {}) if isinstance(data, dict) else {}
        err = str(inner.get("error", "") or "")
        if not state and ("авториз" in err.lower() or "сесс" in err.lower()):
            logger.warning("entry_point auth error (%s) — invalidating session.", err)
            await self.invalidate_session(err)
            try:
                await self._renew_session()
            except TokenExpiredError:
                raise

        bonus = inner.get("bonus_text", []) or []
        if not bonus and isinstance(data, dict):
            bonus = data.get("bonus_text", []) or []
        macros = inner.get("macros", []) or inner.get("macro_list", []) or []
        if not macros and isinstance(data, dict):
            macros = data.get("macros", []) or data.get("macro_list", []) or []
        # Some actions nest macros under redirect / awards
        if not macros and isinstance(inner.get("awards"), list):
            macros = inner.get("awards") or []

        return ApiResponse(
            status=int(inner.get("status", 0) or 0),
            error=err,
            data=inner,
            redirect_url=inner.get("redirect_url"),
            redirect_error=inner.get("redirect_error"),
            bonus_text=bonus if isinstance(bonus, list) else [bonus],
            macros=macros if isinstance(macros, list) else [],
            raw=data if isinstance(data, dict) else {},
        )

    async def common_action(self, code: str, extra: Optional[dict] = None) -> ApiResponse:
        params = {"code": code}
        if extra:
            params.update(extra)
        return await self.entry_point("common", "action", params)

    async def get_state(self) -> GameState:
        resp = await self._post(
            "/entry_point.php?object=common&action=dummy&json_mode_on=1",
            {"json_mode_on": "1", "object": "common", "action": "dummy"},
        )
        try:
            if self._is_auth_failure_response(resp):
                await self.invalidate_session("get_state auth failure")
                await self._renew_session()
                resp = await self._post(
                    "/entry_point.php?object=common&action=dummy&json_mode_on=1",
                    {"json_mode_on": "1", "object": "common", "action": "dummy"},
                )
            data = resp.json()
            st = data.get("state", {}) or {}
            sq = data.get("sq", {})
            fight_id = 0
            if isinstance(sq, dict):
                fight_id = int(sq.get("fight_id", 0) or 0)
            return GameState(
                area_id=str(st.get("area_id", "0")),
                level=int(st.get("level", 0) or 0),
                kind=int(st.get("kind", 0) or 0),
                money=float(st.get("money", 0) or 0),
                money_gold=float(st.get("money_gold", 0) or 0),
                money_silver=float(st.get("money_silver", 0) or 0),
                server_time=int(st.get("server_time", 0) or 0),
                party=int(st.get("party", 0) or 0),
                clan=int(st.get("clan", 0) or 0),
                flags=int(st.get("flags", 0) or 0),
                flags2=int(st.get("flags2", 0) or 0),
                flags3=int(st.get("flags3", 0) or 0),
                fight_id=fight_id,
            )
        except TokenExpiredError:
            raise
        except Exception as exc:
            logger.error("get_state parse error: %s", exc)
            return GameState()

    # ------------------------------------------------------------------
    # Character stats (user.php)
    # ------------------------------------------------------------------

    async def get_char_stats(self, html: Optional[str] = None) -> CharStats:
        try:
            if html is None:
                resp = await self._get("/user.php")
                html = resp.text
            return self.parse_char_stats(html)
        except TokenExpiredError:
            raise
        except Exception as exc:
            logger.error("get_char_stats error: %s", exc)
            return CharStats()

    @staticmethod
    def parse_char_stats(html: str) -> CharStats:
        """Parse CharStats from an already-fetched user.php body."""
        par_m = re.search(r"var par='([^']+)'", html or "")
        if not par_m:
            return CharStats()
        par = dict(urllib.parse.parse_qsl(par_m.group(1), keep_blank_values=True))

        chat_m = re.search(r"sessionUpdate\((\{[^}]+\})\)", html)
        avail = 0
        if chat_m:
            try:
                chat = json.loads(chat_m.group(1))
                avail = int(chat.get("avail", 0))
            except Exception:
                pass

        return CharStats(
            nick=par.get("nick", ""),
            level=int(par.get("lvl", 0) or 0),
            hp=int(par.get("hp", 0) or 0),
            hp_max=int(par.get("hpMax", 0) or 0),
            mp=int(par.get("mp", 0) or 0),
            mp_max=int(par.get("mpMax", 0) or 0),
            kind=int(par.get("sk", par.get("kind", 0)) or 0),
            lc_id=par.get("LC_id", ""),
            online=int(par.get("online", 1) or 1),
            avail=avail,
        )

    # ------------------------------------------------------------------
    # Area info (area.php)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_par(html: str) -> dict[str, str]:
        """
        Extract ``var par='…'`` as a query-string map.

        IMPORTANT: do **not** urllib.unquote the whole string first — that turns
        ``%26`` inside JSON values into ``&`` and truncates ``area_conf_json``.
        ``parse_qsl`` already percent-decodes each value safely.
        """
        par_m = re.search(r"var par='([^']+)'", html)
        if not par_m:
            return {}
        return dict(urllib.parse.parse_qsl(par_m.group(1), keep_blank_values=True))

    @staticmethod
    def _parse_area_conf(par: dict[str, str]) -> dict:
        raw = par.get("area_conf_json", "")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Truncation from oversized fields — try to close the JSON
            try:
                fixed = raw.rstrip()
                if not fixed.endswith("}"):
                    fixed += "]}}"
                return json.loads(fixed)
            except Exception:
                return {}

    async def get_area_info(self) -> AreaInfo:
        try:
            resp = await self._get("/area.php")
            html = resp.text
            par = self._extract_par(html)
            if not par:
                return AreaInfo()

            area = AreaInfo(
                area_id=str(
                    par.get("object_id")
                    or par.get("area_id")
                    or par.get("cur_area_id")
                    or "0"
                ),
                npc_id=int(par.get("npc_id", 0) or 0),
                action_id=int(par.get("action_id", 0) or 0),
                fight_count=int(par.get("fight_count", 0) or 0),
            )

            conf = self._parse_area_conf(par)
            town = conf.get("town", {}) if conf else {}
            area.title = urllib.parse.unquote_plus(str(town.get("title", "") or ""))
            if not area.area_id or area.area_id == "0":
                area.area_id = str(town.get("area_id") or "0")
            for item in town.get("items", []) or []:
                href = item.get("href", {})
                code = ""
                href_str = ""
                area_id = ""
                action_id = ""
                object_id = ""
                object_class = ""
                link_id = ""
                if isinstance(href, dict):
                    code = str(href.get("code", "") or "")
                    area_id = str(href.get("area_id", "") or "")
                    action_id = str(href.get("action_id", "") or "")
                    object_id = str(href.get("object_id", "") or "")
                    object_class = str(href.get("object_class", "") or "")
                    link_id = str(href.get("link_id", "") or item.get("link_id", "") or "")
                elif isinstance(href, str):
                    href_str = href
                    link_id = str(item.get("link_id", "") or "")

                name = urllib.parse.unquote_plus(str(item.get("name", "") or ""))
                area.items.append(AreaItem(
                    item_id=str(item.get("id", "")),
                    name=name,
                    item_type=str(item.get("type", "") or ""),
                    code=code,
                    href=href_str,
                    area_id=area_id or str(item.get("loc_id", "") or ""),
                    npc_id=str(item.get("npc_id", "") or ""),
                    link_id=link_id or str(item.get("link_id", "") or ""),
                    f_id=str(item.get("f_id", "") or ""),
                    action_id=action_id,
                    object_id=object_id,
                    object_class=object_class,
                    link_href=str(item.get("link_href", "") or ""),
                    raw=item if isinstance(item, dict) else {},
                ))
            return area
        except TokenExpiredError:
            raise
        except Exception as exc:
            logger.error("get_area_info error: %s", exc)
            return AreaInfo()

    async def go_area(self, area_id: str, code: str = "COME_IN") -> ApiResponse:
        """Attempt to move into another area via common|action."""
        return await self.common_action(code, {"area_id": str(area_id)})

    async def run_area_action(
        self,
        object_id: str,
        action_id: str,
        link_id: str = "",
        object_class: str = "AREA",
    ) -> ApiResponse:
        """Trigger an area hotspot action (e.g. Расселина)."""
        extra = {
            "object_class": object_class,
            "object_id": str(object_id),
            "action_id": str(action_id),
        }
        if link_id:
            extra["link_id"] = str(link_id)
        return await self.entry_point("common", "action", extra)

    # ------------------------------------------------------------------
    # Hunt / arena (hunt_conf.php)
    # ------------------------------------------------------------------

    async def get_hunt_conf(self) -> dict:
        try:
            resp = await self._get("/hunt_conf.php")
            root = ET.fromstring(resp.text)
            npcs = []
            for npc in root.findall(".//npc"):
                npcs.append({
                    "npc_id": npc.get("npc_id", ""),
                    "title": npc.get("title", ""),
                    "url": npc.get("url", ""),
                    "time_left": int(npc.get("time_left", 0) or 0),
                })
            event = root.find("event")
            return {
                "npcs": npcs,
                "event": {
                    "id": event.get("id", "") if event is not None else "",
                    "title": event.get("title", "") if event is not None else "",
                },
            }
        except Exception as exc:
            logger.debug("get_hunt_conf error: %s", exc)
            return {"npcs": [], "event": {}}

    # ------------------------------------------------------------------
    # NPC dialogue API (entry_point object=npc)
    # ------------------------------------------------------------------

    async def npc_quests(
        self,
        npc_id: str | int,
        *,
        global_npc: int = 0,
        link_id: str | int = 0,
        f_id: str | int = 0,
        area_id: str | int = 0,
    ) -> dict:
        """Fetch NPC quest list + current point (npc|quests + npc|point)."""
        resp = await self.entry_point("npc", "quests", {
            "npc_id": npc_id,
            "global_npc": global_npc,
            "link_id": link_id,
            "f_id": f_id,
            "area_id": area_id,
        })
        return resp.raw

    async def npc_point(
        self,
        npc_id: str | int,
        quest_id: str | int,
        point_id: str | int,
        *,
        global_npc: int = 0,
        link_id: str | int = 0,
        f_id: str | int = 0,
        subpoint_id: str | int = "",
    ) -> dict:
        resp = await self.entry_point("npc", "point", {
            "npc_id": npc_id,
            "global_npc": global_npc,
            "link_id": link_id,
            "f_id": f_id,
            "quest_id": quest_id,
            "point_id": point_id,
            "subpoint_id": subpoint_id or point_id,
        })
        return resp.raw

    async def npc_answer(
        self,
        npc_id: str | int,
        quest_id: str | int,
        point_id: str | int,
        *,
        global_npc: int = 0,
        link_id: str | int = 0,
        f_id: str | int = 0,
        subpoint_id: str | int = "",
    ) -> dict:
        resp = await self.entry_point("npc", "answer", {
            "npc_id": npc_id,
            "global_npc": global_npc,
            "link_id": link_id,
            "f_id": f_id,
            "quest_id": quest_id,
            "point_id": point_id,
            "subpoint_id": subpoint_id or point_id,
        })
        return resp.raw

    # ------------------------------------------------------------------
    # Specific game actions
    # ------------------------------------------------------------------

    async def use_effect(self, show: bool = True) -> ApiResponse:
        return await self.common_action("EFFECT_SHOW" if show else "EFFECT_HIDE")

    async def join_arena(self, npc_id: int, npc_url_hash: str = "") -> httpx.Response:
        params = {"global_npc": "1", "npc_id": str(npc_id)}
        if npc_url_hash:
            # hunt_conf embeds the hash as a bare query flag
            path = f"/npc.php?global_npc=1&npc_id={npc_id}&{npc_url_hash}"
            return await self._get(path)
        return await self._get("/npc.php", params=params)

    async def get_front_locations(self) -> list:
        resp = await self.entry_point("front", "locations")
        return resp.data.get("fronts", []) or []

    async def join_front(self, area_id: str) -> ApiResponse:
        return await self.entry_point("front", "fight_join", {"area_id": area_id})

    async def start_front(self) -> ApiResponse:
        return await self.entry_point("front", "fight_start")
