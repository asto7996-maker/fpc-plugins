"""
Main entry point — pure HTTP orchestrator with Telegram bot interface.

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
import time
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dwar_bot.config import (
    DELAY_IDLE,
    DELAY_MAIN_LOOP,
    DELAY_RETRY,
    IDLE_PAUSE_PROBABILITY,
    LOG_FILE,
    MAX_RETRIES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from dwar_bot.logger import setup_logging, log_exception
from dwar_bot.auth.oauth_login import extract_access_token
from dwar_bot.core.game_client import DwarGameClient, GameState, CharStats, TokenExpiredError
from dwar_bot.telegram_bot import TelegramBotHandler

logger = logging.getLogger("dwar_bot.main")

_shutdown_event = asyncio.Event()


def _handle_signal(signum, frame) -> None:
    logger.warning("Signal %d received — graceful shutdown …", signum)
    _shutdown_event.set()


async def _sleep(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _maybe_idle() -> None:
    if random.random() < IDLE_PAUSE_PROBABILITY:
        s = random.uniform(DELAY_IDLE.min, DELAY_IDLE.max)
        logger.info("Idle pause: %.0fs.", s)
        await asyncio.sleep(s)


# ---------------------------------------------------------------------------
# DwarBot game loop
# ---------------------------------------------------------------------------

class DwarBot:
    def __init__(self, client: DwarGameClient) -> None:
        self._client = client
        self._iteration = 0
        self._errors_in_row = 0
        self._char = CharStats()
        self._state = GameState()
        self._area_title: str = ""
        self._area_items: list = []
        self._npcs: list = []
        self._paused = False
        self._started_at: float = time.time()
        self._token_ok: bool = True

    # ------------------------------------------------------------------
    # Status snapshot (for Telegram commands)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        elapsed = int(time.time() - self._started_at)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        return {
            "running":    not self._paused and not _shutdown_event.is_set(),
            "token_ok":   self._token_ok,
            "nick":       self._char.nick,
            "level":      self._char.level,
            "hp":         self._char.hp,
            "hp_max":     self._char.hp_max,
            "mp":         self._char.mp,
            "mp_max":     self._char.mp_max,
            "money":      self._state.money,
            "area_id":    self._state.area_id,
            "area_title": self._area_title,
            "area_items": self._area_items,
            "npcs":       self._npcs,
            "flags":      self._state.flags,
            "flags2":     self._state.flags2,
            "flags3":     self._state.flags3,
            "iteration":  self._iteration,
            "uptime":     f"{h}ч {m}м {s}с",
        }

    async def pause(self) -> None:
        self._paused = True
        logger.info("Game loop paused via Telegram.")

    async def resume_game(self) -> None:
        self._paused = False
        logger.info("Game loop resumed via Telegram.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("DwarBot HTTP loop started (world=%s).", self._client._world_url)
        while not _shutdown_event.is_set():
            if self._paused:
                await asyncio.sleep(5)
                continue

            self._iteration += 1
            try:
                await self._tick()
                self._errors_in_row = 0
            except asyncio.CancelledError:
                break
            except TokenExpiredError as exc:
                self._token_ok = False
                await self._handle_token_expired(str(exc))
                self._token_ok = True
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

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        self._state = await self._client.get_state()
        self._char = await self._client.get_char_stats()

        if not self._char.nick:
            logger.warning("No character data — renewing session.")
            self._client._session = {}
            await self._client.ensure_session()
            return

        logger.info(
            "[%d] %s Lv%d | HP %d/%d (%.0f%%) | area=%s | money=%.2f",
            self._iteration,
            self._char.nick, self._char.level,
            self._char.hp, self._char.hp_max, self._char.hp_percent,
            self._state.area_id, self._state.money,
        )

        await self._decide_action()

    async def _decide_action(self) -> None:
        if self._state.flags & 0x1 or self._state.fight_id:
            logger.info("Active fight — skipping decision.")
            await _sleep(2.0, 5.0)
            return

        fronts = await self._client.get_front_locations()
        if fronts:
            for front in fronts[:1]:
                area_id = str(front.get("area_id", ""))
                if area_id:
                    resp = await self._client.join_front(area_id)
                    logger.info("join_front(%s): status=%d", area_id, resp.status)
                    await _sleep(2.0, 5.0)
                    return

        area = await self._client.get_area_info()
        if area.title:
            self._area_title = area.title
            self._area_items = [
                {"name": i.name, "item_type": i.item_type, "code": i.code}
                for i in area.items
            ]
            logger.info("Area: %s (id=%s, items=%d)", area.title, area.area_id, len(area.items))

        hunt = await self._client.get_hunt_conf()
        if hunt.get("npcs"):
            self._npcs = hunt["npcs"]
            for npc in hunt["npcs"]:
                logger.info(
                    "NPC: %s (id=%s, time_left=%ds)",
                    npc.get("title"), npc.get("npc_id"), npc.get("time_left", 0),
                )

        logger.info(
            "flags=%d/%d/%d party=%d clan=%d",
            self._state.flags, self._state.flags2, self._state.flags3,
            self._state.party, self._state.clan,
        )
        await _sleep(3.0, 8.0)

    # ------------------------------------------------------------------
    # Token expiry handling
    # ------------------------------------------------------------------

    async def _handle_token_expired(self, detail: str) -> None:
        msg = (
            "⚠️ DwarBot: OAuth токен истёк. Бот ждёт новые куки.\n\n"
            "Чтобы возобновить работу:\n"
            "1. Войди на https://w1.dwar.ru в браузере\n"
            "2. Cookie Editor → Export as JSON\n"
            "3. Пришли JSON в чат боту\n\n"
            "Бот продолжит автоматически."
        )
        logger.critical("TOKEN EXPIRED: %s", detail)
        await self._send_telegram(msg)

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
                            logger.info("Fresh token detected — resuming.")
                            await self._send_telegram("✅ DwarBot: новый токен найден, возобновляю работу.")
                            return
                        last_mtime = mtime
            except Exception:
                pass
            await asyncio.sleep(60)

    async def _send_telegram(self, text: str) -> None:
        import httpx
        token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
        chat_id = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
        if not token or not chat_id:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
        except Exception as exc:
            logger.debug("Telegram send failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_access_token() -> str:
    from dwar_bot.config import COOKIES_DIR
    try:
        files = list(COOKIES_DIR.glob("*.json"))
        if not files:
            return ""
        data = json.loads(files[0].read_text(encoding="utf-8"))
        for c in data:
            if c.get("name") == "mycom":
                val = c.get("value", "")
                return extract_access_token(val) or ""
    except Exception as exc:
        logger.debug("_load_access_token error: %s", exc)
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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

    # Wait for cookie file
    from dwar_bot.config import COOKIES_DIR
    while True:
        files = list(COOKIES_DIR.glob("*.json")) + list(COOKIES_DIR.glob("*.txt"))
        if files:
            logger.info("Cookie file found: %s", files[0].name)
            break
        logger.warning("Waiting for cookie file in '%s' …", COOKIES_DIR)
        await asyncio.sleep(30)

    access_token = _load_access_token()
    if not access_token:
        logger.critical("No access_token in cookie file — cannot start.")
        sys.exit(1)

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

    # Start Telegram bot as background task
    tg_token  = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    tg_chatid = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    tg_task: asyncio.Task | None = None
    if tg_token and tg_chatid:
        async def _get_status() -> dict:
            return bot.get_status()

        tg_handler = TelegramBotHandler(
            token=tg_token,
            owner_chat_id=tg_chatid,
            get_status_fn=_get_status,
            stop_fn=bot.pause,
            resume_fn=bot.resume_game,
            log_path=LOG_FILE,
        )
        tg_task = asyncio.ensure_future(tg_handler.start())
        logger.info("Telegram bot started (chat_id=%s).", tg_chatid)

    # Startup session — retry if token expired
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
            break
        except TokenExpiredError as exc:
            await bot._handle_token_expired(str(exc))
        except Exception as exc:
            log_exception(logger, "Fatal startup error", exc)
            sys.exit(2)

    await bot.run()

    if tg_task:
        tg_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
