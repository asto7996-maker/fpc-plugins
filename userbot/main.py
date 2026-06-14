"""
Telegram AI userbot entrypoint.

Initializes Telethon, SQLite, Gemini engine, and resilient reconnect loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from ai_engine import GeminiEngine
from database import Database
from handlers import EventHandlers

BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "sessions"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

RECONNECT_BASE_DELAY = 3.0
RECONNECT_MAX_DELAY = 120.0


def configure_logging(level: int = logging.INFO) -> None:
    log_dir = BASE_DIR / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "userbot.log", encoding="utf-8"),
        ],
    )


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


class UserbotApp:
    """Coordinates database, AI engine, Telethon client, and handlers."""

    def __init__(self) -> None:
        self.db = Database(BASE_DIR / "data" / "userbot.db")
        self.ai = GeminiEngine(self.db)
        self.client: TelegramClient | None = None
        self.handlers: EventHandlers | None = None
        self._reconnect_attempt = 0

    async def start(self) -> None:
        await self.db.connect()

        api_id = env("TELEGRAM_API_ID")
        api_hash = env("TELEGRAM_API_HASH")
        session_string = env("TELEGRAM_SESSION_STRING")

        if not api_id or not api_hash:
            raise RuntimeError(
                "Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables"
            )

        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session_path = str(SESSION_DIR / "userbot")

        if session_string:
            session: StringSession | str = StringSession(session_string)
        else:
            session = session_path

        self.client = TelegramClient(session, int(api_id), api_hash)
        self.handlers = EventHandlers(self.client, self.db, self.ai)

        await self._connect_with_retry()
        self.handlers.register()
        await self.handlers.bootstrap_owner()

        if await self.db.is_initialized():
            await self.ai.start()

        me = await self.client.get_me()
        logging.getLogger(__name__).info(
            "Userbot online as %s (%s)",
            getattr(me, "username", None) or me.first_name,
            me.id,
        )

        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        await self.ai.stop()
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        await self.db.close()

    async def _connect_with_retry(self) -> None:
        assert self.client is not None
        logger = logging.getLogger(__name__)

        while True:
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    phone = env("TELEGRAM_PHONE")
                    if not phone:
                        raise RuntimeError(
                            "Telegram session is not authorized. "
                            "Provide TELEGRAM_PHONE or TELEGRAM_SESSION_STRING."
                        )
                    await self.client.send_code_request(phone)
                    code = input("Enter Telegram login code: ").strip()
                    try:
                        await self.client.sign_in(phone=phone, code=code)
                    except SessionPasswordNeededError:
                        password = input("Enter 2FA password: ").strip()
                        await self.client.sign_in(password=password)

                self._reconnect_attempt = 0
                return
            except AuthKeyDuplicatedError as exc:
                await self.db.log_error(exc, error_type="TelethonAuth")
                raise
            except Exception as exc:
                self._reconnect_attempt += 1
                delay = min(
                    RECONNECT_BASE_DELAY * (2 ** (self._reconnect_attempt - 1)),
                    RECONNECT_MAX_DELAY,
                )
                await self.db.log_error(
                    exc,
                    error_type="TelethonConnect",
                    context={"attempt": self._reconnect_attempt, "delay": delay},
                )
                logger.warning("Telethon connect failed, retry in %.1fs", delay)
                await asyncio.sleep(delay)


async def run_forever() -> None:
    app = UserbotApp()
    logger = logging.getLogger(__name__)

    while True:
        try:
            await app.start()
        except AuthKeyDuplicatedError:
            logger.critical("Auth key duplicated — stop process and recreate session")
            await app.stop()
            raise
        except (RPCError, ConnectionError, OSError) as exc:
            await app.db.log_error(exc, error_type="MainLoop")
            delay = min(
                RECONNECT_BASE_DELAY * 2,
                RECONNECT_MAX_DELAY,
            )
            logger.warning("Main loop dropped (%s), reconnect in %.1fs", exc, delay)
            await app.stop()
            await asyncio.sleep(delay)
            app = UserbotApp()
        except asyncio.CancelledError:
            await app.stop()
            raise
        except KeyboardInterrupt:
            await app.stop()
            break
        else:
            logger.info("Client disconnected, restarting...")
            await app.stop()
            await asyncio.sleep(RECONNECT_BASE_DELAY)
            app = UserbotApp()


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Shutdown requested")


if __name__ == "__main__":
    main()
