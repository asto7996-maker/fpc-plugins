"""
Automatic @SpamBot health probe.

Sends /start to @SpamBot and classifies the reply to detect
temporary / permanent messaging restrictions on the account.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

from telethon import events

from tg_pool.clients.session_wrapper import SessionWrapper
from tg_pool.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Heuristic phrases from @SpamBot (RU/EN). Keep lowercase.
_GOOD_MARKERS = (
    "good news",
    "no limits",
    "не имеет ограничений",
    "нет ограничений",
    "your account is free",
    "свободен",
)
_BAD_MARKERS = (
    "limited",
    "ограничен",
    "spam",
    "подали жалобу",
    "reported",
    "cannot send",
    "не можете отправлять",
)


@dataclass
class SpamBotReport:
    restricted: bool
    raw_text: str
    summary: str


async def check_spambot(
    wrapper: SessionWrapper,
    *,
    settings: Optional[Settings] = None,
) -> SpamBotReport:
    """
    Probe @SpamBot for the given session.

    Flow
    ----
    1. Connect with sticky fingerprint/proxy
    2. Jitter, then send `/start`
    3. Wait for the next incoming message from SpamBot (timeout)
    4. Classify reply → update wrapper.account.is_spambot_restricted
    """
    settings = settings or get_settings()
    client = await wrapper.connect()
    username = settings.spambot_username.lstrip("@")

    await wrapper.jitter(min_sec=2.0, max_sec=6.0)

    # Resolve entity through the same proxy/session
    entity = await client.get_entity(username)

    reply_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    @client.on(events.NewMessage(from_users=entity, incoming=True))
    async def _on_reply(event: events.NewMessage.Event) -> None:
        text = (event.message.message or "").strip()
        if text and not reply_future.done():
            reply_future.set_result(text)

    try:
        await client.send_message(entity, "/start")
        raw = await asyncio.wait_for(reply_future, timeout=settings.spambot_timeout_sec)
    except asyncio.TimeoutError:
        report = SpamBotReport(
            restricted=False,
            raw_text="",
            summary="SpamBot did not reply in time — inconclusive",
        )
        logger.warning("Account #%s SpamBot timeout", wrapper.account.id)
        return report
    finally:
        client.remove_event_handler(_on_reply)

    restricted = _classify(raw)
    wrapper.account.is_spambot_restricted = restricted
    if restricted:
        from tg_pool.db.models import AccountStatus

        wrapper.account.status = AccountStatus.spambot
        wrapper.account.last_error = f"SpamBot restriction: {raw[:300]}"

    summary = "RESTRICTED" if restricted else "OK"
    logger.info("Account #%s SpamBot → %s", wrapper.account.id, summary)
    return SpamBotReport(restricted=restricted, raw_text=raw, summary=summary)


def _classify(text: str) -> bool:
    lowered = text.lower()
    if any(m in lowered for m in _GOOD_MARKERS):
        return False
    if any(m in lowered for m in _BAD_MARKERS):
        return True
    # Fallback: unknown wording — treat as restricted only if "limit" pattern appears
    return bool(re.search(r"limit|огранич", lowered))
