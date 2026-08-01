"""
Pure HTTP game client for dwar.ru — no Flash, no Playwright.

All game interactions go through:
  * POST /entry_point.php?object=OBJ&action=ACT&json_mode_on=1  → JSON
  * GET  /user.php           → HTML with embedded `par` variable (character stats)
  * GET  /area.php           → HTML with area navigation JSON
  * GET  /hunt_conf.php      → XML with NPC/event info
  * POST /register.php       → OAuth session renewal
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from dwar_bot.config import DELAY_RETRY, MAX_RETRIES

logger = logging.getLogger(__name__)


class TokenExpiredError(Exception):
    """Raised when the OAuth access_token is expired and needs manual renewal."""


_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

# API status codes
STATUS_OK = 100
STATUS_ERROR = 1


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class GameState:
    """Current server state as returned by common|dummy."""
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
    avail: int = 0  # available action flags

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


# ---------------------------------------------------------------------------
# DwarGameClient
# ---------------------------------------------------------------------------

class DwarGameClient:
    """
    Stateful HTTP client for dwar.ru game interactions.

    Manages:
    - Session cookies (renewed via OAuth when expired)
    - Retry logic with backoff
    - Parsing of all game data formats (JSON, HTML par, XML)
    """

    def __init__(
        self,
        world_url: str,
        access_token: str,
        mycom_cookie_value: str = "",
    ) -> None:
        self._world_url = world_url.rstrip("/")
        self._access_token = access_token
        self._mycom_value = mycom_cookie_value
        self._session: dict[str, str] = {}
        self._session_renewed_at: float = 0.0
        self._headers = {**_BASE_HEADERS, "Referer": f"{world_url}/game.php"}

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def ensure_session(self) -> None:
        """Renew session if empty or stale (> 20 min old)."""
        age = time.time() - self._session_renewed_at
        if not self._session or age > 1200:
            await self._renew_session()

    def _is_token_redirect(self, location: str) -> bool:
        """Return True if the redirect means the OAuth token has expired."""
        return "soc_auth.php" in location or "vkplay.ru" in location or "oauth" in location.lower()

    async def _renew_session(self) -> None:
        """POST to register.php with access_token to get a fresh session."""
        url = f"{self._world_url}/register.php"
        logger.info("Renewing OAuth session via %s …", url)
        async with httpx.AsyncClient(follow_redirects=False, timeout=20) as c:
            r = await c.post(
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

        # Check if the server redirected us to the OAuth login page
        # (which means the access_token has expired)
        if self._is_token_redirect(location) or not new_cookies.get("sess_sid"):
            raise TokenExpiredError(
                f"OAuth access_token has expired. The bot needs fresh cookies.\n"
                f"Redirect destination: {location}"
            )

        self._session = new_cookies
        self._session_renewed_at = time.time()
        logger.info(
            "Session renewed — sess_uid=%s sess_sid=%s…",
            new_cookies.get("sess_uid", "?"),
            new_cookies.get("sess_sid", "")[:8],
        )

    # ------------------------------------------------------------------
    # Core HTTP helpers
    # ------------------------------------------------------------------

    async def _post(self, path: str, data: dict, timeout: float = 15.0) -> httpx.Response:
        """POST to game server, retry on transient errors."""
        await self.ensure_session()
        url = f"{self._world_url}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=timeout,
                    cookies=self._session,
                ) as c:
                    return await c.post(url, data=data, headers=self._headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == MAX_RETRIES:
                    raise
                wait = DELAY_RETRY.min * (2 ** (attempt - 1))
                logger.warning("POST %s failed (%s), retry %d in %.1fs", path, exc, attempt, wait)
                await asyncio.sleep(wait)
        raise RuntimeError("POST failed after all retries")

    async def _get(self, path: str, params: Optional[dict] = None, timeout: float = 15.0) -> httpx.Response:
        await self.ensure_session()
        url = f"{self._world_url}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=timeout,
                    cookies=self._session,
                ) as c:
                    return await c.get(
                        url, params=params,
                        headers={**self._headers, "Content-Type": "text/html"},
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == MAX_RETRIES:
                    raise
                wait = DELAY_RETRY.min * (2 ** (attempt - 1))
                logger.warning("GET %s failed (%s), retry %d in %.1fs", path, exc, attempt, wait)
                await asyncio.sleep(wait)
        raise RuntimeError("GET failed after all retries")

    # ------------------------------------------------------------------
    # entry_point.php API
    # ------------------------------------------------------------------

    async def entry_point(
        self, obj: str, action: str, extra: Optional[dict] = None
    ) -> ApiResponse:
        """Call entry_point.php and parse JSON response."""
        params = {
            "json_mode_on": "1",
            "object": obj,
            "action": action,
        }
        if extra:
            params.update(extra)

        path = f"/entry_point.php?object={obj}&action={action}&json_mode_on=1"
        try:
            resp = await self._post(path, params)
            data = resp.json()
        except Exception as exc:
            logger.error("entry_point(%s|%s) error: %s", obj, action, exc)
            return ApiResponse(status=0, error=str(exc))

        key = f"{obj}|{action}"
        inner = data.get(key, {}) if isinstance(data, dict) else {}
        if not isinstance(inner, dict):
            inner = {}

        # Detect session expiry: if area_id is missing from state, re-auth
        state = data.get("state", {}) if isinstance(data, dict) else {}
        if not state and inner.get("status") not in (STATUS_OK, None):
            logger.warning("Empty state in entry_point response — may need re-auth.")
            await self._renew_session()

        return ApiResponse(
            status=inner.get("status", 0),
            error=str(inner.get("error", "") or ""),
            data=inner,
            redirect_url=inner.get("redirect_url"),
            redirect_error=inner.get("redirect_error"),
            bonus_text=inner.get("bonus_text", []) or [],
        )

    async def common_action(self, code: str, extra: Optional[dict] = None) -> ApiResponse:
        """Shortcut for common|action with a code."""
        params = {"code": code}
        if extra:
            params.update(extra)
        return await self.entry_point("common", "action", params)

    async def get_state(self) -> GameState:
        """Fetch current game state (area_id, level, money, etc.)."""
        resp = await self._post(
            "/entry_point.php?object=common&action=dummy&json_mode_on=1",
            {"json_mode_on": "1", "object": "common", "action": "dummy"},
        )
        try:
            data = resp.json()
            st = data.get("state", {})
            return GameState(
                area_id=str(st.get("area_id", "0")),
                level=int(st.get("level", 0)),
                kind=int(st.get("kind", 0)),
                money=float(st.get("money", 0)),
                money_gold=float(st.get("money_gold", 0)),
                money_silver=float(st.get("money_silver", 0)),
                server_time=int(st.get("server_time", 0)),
                party=int(st.get("party", 0)),
                clan=int(st.get("clan", 0)),
                flags=int(st.get("flags", 0)),
                flags2=int(st.get("flags2", 0)),
                flags3=int(st.get("flags3", 0)),
                fight_id=int(data.get("sq", {}).get("fight_id", 0) if isinstance(data.get("sq"), dict) else 0),
            )
        except Exception as exc:
            logger.error("get_state parse error: %s", exc)
            return GameState()

    # ------------------------------------------------------------------
    # Character stats (user.php)
    # ------------------------------------------------------------------

    async def get_char_stats(self) -> CharStats:
        """Parse character stats from user.php `par` variable."""
        try:
            resp = await self._get("/user.php")
            html = resp.text
            par_m = re.search(r"var par='([^']+)'", html)
            if not par_m:
                logger.debug("No par variable in user.php")
                return CharStats()
            par = dict(urllib.parse.parse_qsl(urllib.parse.unquote(par_m.group(1))))

            # Also extract from chat update JSON (more reliable for avail flags)
            chat_m = re.search(r"sessionUpdate\((\{[^}]+\})\)", html)
            avail = 0
            fight_id = 0
            if chat_m:
                try:
                    import json
                    chat = json.loads(chat_m.group(1))
                    avail = int(chat.get("avail", 0))
                    fight_id = int(chat.get("fight_id", 0))
                except Exception:
                    pass

            return CharStats(
                nick=par.get("nick", ""),
                level=int(par.get("lvl", 0)),
                hp=int(par.get("hp", 0)),
                hp_max=int(par.get("hpMax", 0)),
                mp=int(par.get("mp", 0)),
                mp_max=int(par.get("mpMax", 0)),
                kind=int(par.get("sk", par.get("kind", 0))),
                lc_id=par.get("LC_id", ""),
                online=int(par.get("online", 1)),
                avail=avail,
            )
        except Exception as exc:
            logger.error("get_char_stats error: %s", exc)
            return CharStats()

    # ------------------------------------------------------------------
    # Area info (area.php)
    # ------------------------------------------------------------------

    async def get_area_info(self) -> AreaInfo:
        """Parse area navigation and state from area.php."""
        import json as _json
        try:
            resp = await self._get("/area.php")
            html = resp.text
            par_m = re.search(r"var par='([^']+)'", html)
            if not par_m:
                return AreaInfo()
            par = dict(urllib.parse.parse_qsl(urllib.parse.unquote(par_m.group(1))))

            area = AreaInfo(
                area_id=par.get("object_id", "0"),
                npc_id=int(par.get("npc_id", 0)),
                action_id=int(par.get("action_id", 0)),
                fight_count=int(par.get("fight_count", 0)),
            )

            # Parse area_conf_json for navigation items
            if "area_conf_json" in par:
                try:
                    conf = _json.loads(par["area_conf_json"])
                    town = conf.get("town", {})
                    area.title = town.get("title", "")
                    for item in town.get("items", []):
                        href = item.get("href", {})
                        code = ""
                        href_str = ""
                        if isinstance(href, dict):
                            code = href.get("code", "")
                        elif isinstance(href, str):
                            href_str = href
                        area.items.append(AreaItem(
                            item_id=str(item.get("id", "")),
                            name=item.get("name", ""),
                            item_type=item.get("type", ""),
                            code=code,
                            href=href_str,
                        ))
                except Exception as e:
                    logger.debug("area_conf_json parse error: %s", e)

            return area
        except Exception as exc:
            logger.error("get_area_info error: %s", exc)
            return AreaInfo()

    # ------------------------------------------------------------------
    # Hunt / arena (hunt_conf.php)
    # ------------------------------------------------------------------

    async def get_hunt_conf(self) -> dict:
        """Parse hunt_conf.php XML for available NPCs and events."""
        try:
            resp = await self._get("/hunt_conf.php")
            root = ET.fromstring(resp.text)
            npcs = []
            for npc in root.findall(".//npc"):
                npcs.append({
                    "npc_id": npc.get("npc_id", ""),
                    "title": npc.get("title", ""),
                    "url": npc.get("url", ""),
                    "time_left": int(npc.get("time_left", 0)),
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
    # Specific game actions
    # ------------------------------------------------------------------

    async def use_effect(self, show: bool = True) -> ApiResponse:
        """Show or hide the newbie blessing effect."""
        return await self.common_action("EFFECT_SHOW" if show else "EFFECT_HIDE")

    async def join_arena(self, npc_id: int, npc_url_hash: str) -> httpx.Response:
        """Navigate to the arena NPC page."""
        return await self._get(
            f"/npc.php",
            params={"global_npc": "1", "npc_id": str(npc_id), "hash": npc_url_hash},
        )

    async def get_front_locations(self) -> list:
        """Return available front/PvP locations."""
        resp = await self.entry_point("front", "locations")
        return resp.data.get("fronts", [])

    async def join_front(self, area_id: str) -> ApiResponse:
        """Join a front battle at the given area."""
        return await self.entry_point("front", "fight_join", {"area_id": area_id})

    async def start_front(self) -> ApiResponse:
        return await self.entry_point("front", "fight_start")
