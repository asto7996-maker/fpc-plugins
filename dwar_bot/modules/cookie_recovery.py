"""
Cookie / session recovery for dwar.ru.

Policy
------
1. Prefer keeping a still-valid ``sess_sid`` over blind OAuth renew.
2. Soft-recheck before invalidate / auth_blocked.
3. When cookie file updates, try old sess first; only OAuth if soft fails
   and we have a usable access_token.
4. Attempt Astrum ``refresh_token`` when access_token is dead (best-effort).
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CookieRecovery:
    """
    Orchestrates soft-keep → refresh → OAuth → wait-for-paste.

    Expects a duck-typed ``client`` with:
      soft_recheck_session, invalidate_session, ensure_session,
      apply_cookies, unblock_auth, maybe_reload_cookie_file,
      _session, _access_token, _mycom_value, auth_blocked, _cookie_file
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self.last_soft_ok_at: float = 0.0
        self.last_soft_fail_at: float = 0.0
        self.recoveries: int = 0
        self.soft_keeps: int = 0
        self.oauth_renews: int = 0
        self.refresh_attempts: int = 0

    async def try_keep_old_session(self, reason: str = "") -> bool:
        """Return True if existing sess_sid still answers the game API."""
        ok = await self.client.soft_recheck_session()
        if ok:
            self.last_soft_ok_at = time.time()
            self.soft_keeps += 1
            logger.info("CookieRecovery: kept old sess_sid (%s).", reason or "ok")
            self.client.unblock_auth()
            return True
        self.last_soft_fail_at = time.time()
        logger.info("CookieRecovery: soft recheck failed (%s).", reason or "fail")
        return False

    async def try_refresh_access_token(self) -> bool:
        """
        Best-effort Astrum refresh_token → new access_token in mycom.

        Many Astrum deployments ignore refresh; failures are non-fatal.
        """
        mycom = str(getattr(self.client, "_mycom_value", "") or "")
        if not mycom:
            return False
        try:
            from dwar_bot.auth.oauth_login import extract_refresh_token, extract_access_token
        except Exception:
            return False

        refresh = extract_refresh_token(mycom)
        if not refresh:
            return False

        self.refresh_attempts += 1
        new_access = await _astrum_refresh(refresh)
        if not new_access:
            return False

        # Rewrite mycom with new access_token, keep refresh_token
        try:
            decoded = urllib.parse.unquote(mycom)
            params = dict(urllib.parse.parse_qsl(decoded))
            params["access_token"] = new_access
            if refresh and "refresh_token" not in params:
                params["refresh_token"] = refresh
            new_mycom = urllib.parse.urlencode(params)
            self.client._mycom_value = new_mycom
            self.client._access_token = new_access
            self.client._session["mycom"] = new_mycom
            self.client.unblock_auth()
            logger.info("CookieRecovery: access_token refreshed via refresh_token.")
            return True
        except Exception as exc:
            logger.debug("CookieRecovery rewrite mycom: %s", exc)
            return False

    async def recover_after_cookie_file_update(self) -> tuple[bool, str]:
        """
        Called when cookie JSON on disk changed.

        Returns (ok, human_message).
        """
        self.recoveries += 1
        await self.client.maybe_reload_cookie_file()
        self.client.unblock_auth()

        # 1) If pasted JSON already has sess_sid — soft check it
        if self.client._session.get("sess_sid"):
            if await self.try_keep_old_session("file update with sess"):
                return True, "старый sess_sid ещё жив — OAuth не нужен"

        # 2) Try refresh_token then OAuth renew
        await self.try_refresh_access_token()
        try:
            # Only wipe sess when soft already failed / missing
            if not self.client._session.get("sess_sid"):
                await self.client.invalidate_session("no sess after reload", force=True)
            else:
                # Soft failed above — force renew
                await self.client.invalidate_session("soft failed after file update", force=True)
            await self.client.ensure_session()
            self.oauth_renews += 1
            return True, "сессия обновлена через OAuth"
        except Exception as exc:
            # 3) Last chance: maybe somehow sess still works
            if await self.try_keep_old_session("oauth failed fallback"):
                return True, "OAuth упал, но старые куки ещё работают"
            return False, f"не удалось восстановить сессию: {exc}"

    async def recover_before_auth_block(self, reason: str = "") -> bool:
        """
        Before setting auth_blocked / wiping sess — try soft keep + refresh.
        """
        if await self.try_keep_old_session(reason or "pre-block"):
            return True
        if await self.try_refresh_access_token():
            try:
                await self.client.invalidate_session("refresh then renew", force=True)
                await self.client.ensure_session()
                self.oauth_renews += 1
                return True
            except Exception as exc:
                logger.warning("CookieRecovery renew after refresh failed: %s", exc)
                if await self.try_keep_old_session("post-refresh fallback"):
                    return True
        return False


async def _astrum_refresh(refresh_token: str) -> Optional[str]:
    """
    Attempt to refresh Astrum / VK Play access_token.

    Endpoint varies; we try a few known hosts. Returns new access_token or None.
    """
    import httpx

    candidates = [
        ("https://api.vkplay.ru/oauth/token", {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }),
        ("https://auth-ac.vkplay.ru/oauth2/token", {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }),
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    for url, data in candidates:
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                resp = await client.post(url, data=data, headers=headers)
            if resp.status_code >= 400:
                logger.debug("Astrum refresh %s → %s", url, resp.status_code)
                continue
            try:
                payload = resp.json()
            except Exception:
                continue
            token = (
                payload.get("access_token")
                or (payload.get("data") or {}).get("access_token")
            )
            if token:
                return str(token)
        except Exception as exc:
            logger.debug("Astrum refresh %s error: %s", url, exc)
    return None
