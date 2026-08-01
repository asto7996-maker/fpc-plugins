"""
Main entry point — pure HTTP orchestrator (no Flash/browser).

dwar.ru runs its game logic in Flash, but all backend calls are accessible
via /entry_point.php (JSON), /user.php (HTML par), /area.php and /hunt_conf.php.
The bot drives the game entirely through these HTTP endpoints.

Start:
    python -m dwar_bot.main
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dwar_bot.config import (
    DELAY_IDLE,
    DELAY_MAIN_LOOP,
    DELAY_RETRY,
    IDLE_PAUSE_PROBABILITY,
    MAX_RETRIES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from dwar_bot.logger import setup_logging, log_exception
from dwar_bot.auth.oauth_login import extract_access_token
from dwar_bot.core.game_client import DwarGameClient, GameState, CharStats, TokenExpiredError

logger = logging.getLogger("dwar_bot.main")

_shutdown_event = asyncio.Event()


def _handle_signal(signum, frame) -> None:
    logger.warning("Signal %d received — graceful shutdown …", signum)
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Helper: random sleep
# ---------------------------------------------------------------------------

import random

async def _sleep(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _maybe_idle() -> None:
    if random.random() < IDLE_PAUSE_PROBABILITY:
        s = random.uniform(DELAY_IDLE.min, DELAY_IDLE.max)
        logger.info("Idle pause: %.0fs.", s)
        await asyncio.sleep(s)


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class DwarBot:
    def __init__(self, client: DwarGameClient) -> None:
        self._client = client
        self._iteration = 0
        self._errors_in_row = 0
        self._char = CharStats()
        self._state = GameState()

    async def run(self) -> None:
        logger.info("DwarBot HTTP loop started (world=%s).", self._client._world_url)
        while not _shutdown_event.is_set():
            self._iteration += 1
            try:
                await self._tick()
                self._errors_in_row = 0
            except asyncio.CancelledError:
                break
            except TokenExpiredError as exc:
                await self._handle_token_expired(str(exc))
            except Exception as exc:
                self._errors_in_row += 1
                log_exception(logger, f"Error in tick #{self._iteration}", exc)
                if self._errors_in_row >= MAX_RETRIES:
                    logger.critical("%d consecutive errors — pausing 5min.", MAX_RETRIES)
                    await asyncio.sleep(300)
                    self._errors_in_row = 0
                    self._client._session = {}
                else:
                    await _sleep(DELAY_RETRY.min, DELAY_RETRY.max)

            if not _shutdown_event.is_set():
                await _sleep(DELAY_MAIN_LOOP.min, DELAY_MAIN_LOOP.max)
                await _maybe_idle()

    async def _handle_token_expired(self, detail: str) -> None:
        """
        When the OAuth token expires: notify via Telegram, then poll the cookie
        file every 60 s until the user uploads fresh cookies with a new token.
        """
        msg = (
            "⚠️ DwarBot: OAuth токен истёк. Бот остановлен.\n\n"
            "Чтобы возобновить работу:\n"
            "1. Войди на https://w1.dwar.ru в браузере\n"
            "2. Установи расширение Cookie Editor\n"
            "3. Экспортируй все куки сайта w1.dwar.ru как JSON\n"
            "4. Загрузи файл на сервер командой:\n"
            "   scp cookies.json root@31.76.30.135:"
            "/root/dwar_bot/cookies/session_cookies.json\n\n"
            "Бот продолжит работу автоматически."
        )
        logger.critical("TOKEN EXPIRED: %s", detail)
        await self._send_telegram(msg)

        # Poll until a fresh token appears in the cookie file
        from dwar_bot.config import COOKIES_DIR
        last_mtime = 0.0
        while not _shutdown_event.is_set():
            try:
                files = list(COOKIES_DIR.glob("*.json"))
                if files:
                    mtime = files[0].stat().st_mtime
                    if mtime != last_mtime:
                        new_token = _load_access_token()
                        if new_token and new_token != self._client._access_token:
                            self._client._access_token = new_token
                            self._client._session = {}
                            logger.info("Fresh OAuth token detected — resuming.")
                            await self._send_telegram("✅ DwarBot: новый токен обнаружен, возобновляю работу.")
                            return
                        last_mtime = mtime
            except Exception:
                pass
            await asyncio.sleep(60)

    async def _send_telegram(self, text: str) -> None:
        """Send a Telegram message (best-effort, no crash on failure)."""
        token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
        chat_id = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
        if not token or not chat_id:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
        except Exception as exc:
            logger.debug("Telegram send failed: %s", exc)

    async def _tick(self) -> None:
        """One bot iteration: read state → decide action → execute."""

        # 1. Refresh character stats
        self._state = await self._client.get_state()
        self._char = await self._client.get_char_stats()

        if not self._char.nick:
            logger.warning("Could not read character stats — forcing session renewal.")
            self._client._session = {}
            await self._client.ensure_session()
            return

        logger.info(
            "[%d] %s Lv%d | HP %d/%d (%.0f%%) | area=%s | money=%.2f",
            self._iteration,
            self._char.nick,
            self._char.level,
            self._char.hp, self._char.hp_max, self._char.hp_percent,
            self._state.area_id,
            self._state.money,
        )

        # 2. Decide what to do based on current state
        await self._decide_action()

    async def _decide_action(self) -> None:
        """Simple state-machine: prefer arena → front → idle."""

        # Check if there's an active fight (flags encode in-fight state)
        if self._state.flags & 0x1 or self._state.fight_id:
            logger.info("Active fight detected — skipping action tick.")
            await _sleep(2.0, 5.0)
            return

        # Try to join the front (PvP arena)
        fronts = await self._client.get_front_locations()
        if fronts:
            logger.info("Found %d active fronts — attempting to join.", len(fronts))
            for front in fronts[:1]:
                area_id = str(front.get("area_id", ""))
                if area_id:
                    resp = await self._client.join_front(area_id)
                    logger.info("join_front(%s): status=%d", area_id, resp.status)
                    await _sleep(2.0, 5.0)
                    return

        # Get area info for navigation options
        area = await self._client.get_area_info()
        if area.title:
            logger.info("Area: %s (id=%s, items=%d)", area.title, area.area_id, len(area.items))

        # Get hunt/event NPCs
        hunt = await self._client.get_hunt_conf()
        if hunt.get("npcs"):
            for npc in hunt["npcs"]:
                logger.info(
                    "NPC available: %s (id=%s, time_left=%ds)",
                    npc.get("title"), npc.get("npc_id"), npc.get("time_left", 0)
                )

        # Log available navigation items
        if area.items:
            for item in area.items[:3]:
                logger.debug("  Nav item: %s (type=%s, code=%s)", item.name, item.item_type, item.code)

        # For now: log state and wait — the combat/quest system
        # will be expanded once the specific battle API is discovered
        logger.info(
            "State: flags=%d flags2=%d flags3=%d party=%d clan=%d",
            self._state.flags, self._state.flags2, self._state.flags3,
            self._state.party, self._state.clan,
        )

        await _sleep(3.0, 8.0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_access_token() -> str:
    """Read access_token from the mycom cookie in the cookie file."""
    from dwar_bot.config import COOKIES_DIR
    try:
        files = list(COOKIES_DIR.glob("*.json"))
        if not files:
            return ""
        data = json.loads(files[0].read_text(encoding="utf-8"))
        for c in data:
            if c.get("name") == "mycom":
                val = c.get("value", "")
                token = extract_access_token(val)
                return token or ""
    except Exception as exc:
        logger.debug("_load_access_token error: %s", exc)
    return ""


async def main() -> None:
    setup_logging(
        level=os.getenv("DWAR_LOG_LEVEL", "INFO"),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        telegram_min_level=os.getenv("TELEGRAM_MIN_LEVEL", "WARNING"),
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    _world = os.getenv("DWAR_WORLD", "w1")
    _world_url = os.getenv("DWAR_WORLD_URL", f"https://{_world}.dwar.ru")

    # Wait for cookie file (which contains mycom token for OAuth)
    from dwar_bot.config import COOKIES_DIR
    while True:
        files = list(COOKIES_DIR.glob("*.json")) + list(COOKIES_DIR.glob("*.txt"))
        if files:
            logger.info("Cookie file found: %s", files[0].name)
            break
        logger.warning(
            "Waiting for cookie file in '%s' with mycom OAuth token …", COOKIES_DIR
        )
        await asyncio.sleep(30)

    access_token = _load_access_token()
    if not access_token:
        logger.critical(
            "No access_token found in cookie file. "
            "The mycom cookie must contain access_token=... in its value."
        )
        sys.exit(1)

    logger.info("access_token loaded (length=%d).", len(access_token))

    mycom_value = ""
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
        for c in data:
            if c.get("name") == "mycom":
                mycom_value = c.get("value", "")
    except Exception:
        pass

    client = DwarGameClient(
        world_url=_world_url,
        access_token=access_token,
        mycom_cookie_value=mycom_value,
    )

    bot = DwarBot(client)

    # Attempt initial session — if token is expired, enter waiting mode
    while not _shutdown_event.is_set():
        try:
            await client.ensure_session()
            state = await client.get_state()
            char = await client.get_char_stats()
            logger.info(
                "Connected! nick=%s level=%d hp=%d/%d area=%s money=%.2f",
                char.nick, char.level, char.hp, char.hp_max,
                state.area_id, state.money,
            )
            break  # session established, proceed to game loop
        except TokenExpiredError as exc:
            await bot._handle_token_expired(str(exc))
            # After _handle_token_expired returns, the access_token has been refreshed
            # → loop back and try ensure_session() again
            continue
        except Exception as exc:
            log_exception(logger, "Fatal error during startup", exc)
            sys.exit(2)

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
