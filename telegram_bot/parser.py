"""
User Parser — scrapes active members from source Telegram groups/chats
and stores them in the database for use by the Inviter module.

Activity filter: only users whose last-seen status is within the configured
window (default 24 hours) are stored as "new" targets.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    RPCError,
    UserDeactivatedBanError,
)
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import (
    ChannelParticipantsSearch,
    User,
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from config import AppConfig
from database import Database
from utils import human_delay, normalise_channel_ref

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Activity-level constants (ordered: most active → least)
# ---------------------------------------------------------------------------

ACTIVITY_ONLINE   = 0   # Currently online
ACTIVITY_RECENT   = 1   # Online within 15 min
ACTIVITY_TODAY    = 2   # Online within ~24 h (UserStatusRecently)
ACTIVITY_WEEK     = 3   # Online within last week
ACTIVITY_MONTH    = 4   # Online within last month
ACTIVITY_UNKNOWN  = 5   # UserStatusEmpty — can't determine
ACTIVITY_INACTIVE = 6   # UserStatusLastMonth+ or very long ago


def classify_activity(status) -> int:
    """Map a Telegram UserStatus to our activity level constant."""
    if isinstance(status, UserStatusOnline):
        return ACTIVITY_ONLINE
    if isinstance(status, UserStatusRecently):
        return ACTIVITY_RECENT
    if isinstance(status, UserStatusOffline):
        # was_online attribute tells us when they were last seen
        return ACTIVITY_TODAY  # treat as relatively recent
    if isinstance(status, UserStatusLastWeek):
        return ACTIVITY_WEEK
    if isinstance(status, UserStatusLastMonth):
        return ACTIVITY_MONTH
    if isinstance(status, UserStatusEmpty):
        return ACTIVITY_UNKNOWN
    return ACTIVITY_INACTIVE


def get_last_seen_str(status) -> Optional[str]:
    """Return an ISO datetime string for the user's last-seen time, or None."""
    if isinstance(status, UserStatusOnline):
        return datetime.now(timezone.utc).isoformat()
    if isinstance(status, UserStatusOffline) and status.was_online:
        return status.was_online.isoformat()
    if isinstance(status, UserStatusRecently):
        # Telegram masks exact time — approximate as 10 min ago
        approx = datetime.now(timezone.utc) - timedelta(minutes=10)
        return approx.isoformat()
    return None


def activity_within_hours(status, hours: int) -> bool:
    """
    Return True if we can confidently say the user was active within `hours`.
    Conservative approach: UNKNOWN users are excluded.
    """
    level = classify_activity(status)

    if hours <= 24:
        return level in (ACTIVITY_ONLINE, ACTIVITY_RECENT, ACTIVITY_TODAY)
    if hours <= 168:  # 7 days
        return level in (ACTIVITY_ONLINE, ACTIVITY_RECENT, ACTIVITY_TODAY, ACTIVITY_WEEK)
    if hours <= 720:  # 30 days
        return level <= ACTIVITY_MONTH
    return level != ACTIVITY_INACTIVE


# ---------------------------------------------------------------------------
# UserParser
# ---------------------------------------------------------------------------

class UserParser:
    """
    Iterates over registered source groups, fetches participant lists,
    filters by recent activity, and persists results to the database.
    """

    def __init__(self, client: TelegramClient, db: Database, cfg: AppConfig):
        self.client = client
        self.db = db
        self.cfg = cfg
        self.p_cfg = cfg.parser

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._parse_count_session = 0

    # ------------------------------------------------------------------
    # Public control interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background parsing loop."""
        if self._running:
            logger.warning("UserParser already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="parser_loop")
        logger.info("UserParser started")

    async def stop(self) -> None:
        """Stop the background loop gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("UserParser stopped")

    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def add_source_group(self, group_ref: str) -> bool:
        """Add a source group for parsing."""
        username = normalise_channel_ref(group_ref)
        await self.db.add_source_group(username)
        logger.info("Source group added: @%s", username)
        return True

    async def remove_source_group(self, group_ref: str) -> bool:
        """Remove a source group."""
        username = normalise_channel_ref(group_ref)
        await self.db.remove_source_group(username)
        logger.info("Source group removed: @%s", username)
        return True

    def get_status(self) -> Dict:
        return {
            "running": self.is_running(),
            "session_parsed": self._parse_count_session,
            "activity_hours": self.p_cfg.activity_hours,
            "run_interval": self.p_cfg.run_interval,
        }

    # ------------------------------------------------------------------
    # Internal: main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        logger.info("UserParser loop started")
        while self._running:
            try:
                await self._parse_all_groups()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Parser loop error: %s", exc, exc_info=True)
                await asyncio.sleep(60)
                continue

            logger.info(
                "Parser run complete. Next run in %ds.", self.p_cfg.run_interval
            )
            await asyncio.sleep(self.p_cfg.run_interval)

        logger.info("UserParser loop exited")

    # ------------------------------------------------------------------
    # Internal: parse all registered groups
    # ------------------------------------------------------------------

    async def _parse_all_groups(self) -> None:
        """Iterate over all active source groups and parse members."""
        groups = await self.db.get_active_source_groups()

        # Also add groups from config if not already in DB
        for ref in self.p_cfg.source_groups:
            username = normalise_channel_ref(ref)
            existing = any(g["username"] == username for g in groups)
            if not existing:
                await self.db.add_source_group(username)
                groups.append({"username": username, "group_id": None, "title": None})

        if not groups:
            logger.info("No source groups configured for parser")
            return

        total_new = 0
        for group in groups:
            if not self._running:
                break
            username = group["username"]
            try:
                new_count = await self._parse_group(username)
                total_new += new_count
                await self.db.update_source_group_parsed(username)
                logger.info("Parsed @%s: %d new users", username, new_count)
            except FloodWaitError as exc:
                logger.warning("FloodWait parsing @%s: waiting %ds", username, exc.seconds)
                await asyncio.sleep(exc.seconds + 5)
            except (ChannelPrivateError, ChatAdminRequiredError):
                logger.warning("@%s is private or requires admin — disabling", username)
                await self.db.remove_source_group(username)
            except Exception as exc:
                logger.error("Error parsing @%s: %s", username, exc, exc_info=True)

        self._parse_count_session += total_new
        logger.info("Parser run: %d new users collected this run", total_new)

    # ------------------------------------------------------------------
    # Internal: parse single group
    # ------------------------------------------------------------------

    async def _parse_group(self, group_ref: str) -> int:
        """
        Parse members from a single group.
        Returns the number of newly added users.
        """
        entity = await self._resolve_entity(group_ref)
        if entity is None:
            return 0

        # Update group metadata
        group_id = getattr(entity, "id", None)
        title = getattr(entity, "title", group_ref)
        participants_count = getattr(entity, "participants_count", None)
        if group_id:
            await self.db.add_source_group(group_ref, group_id=group_id, title=title)

        new_count = 0
        collected = 0
        offset = 0
        batch_size = 200

        while collected < self.p_cfg.max_per_run:
            try:
                batch = await self._fetch_participants_batch(entity, offset, batch_size)
            except FloodWaitError as exc:
                logger.warning("FloodWait fetching batch (offset=%d): %ds", offset, exc.seconds)
                await asyncio.sleep(exc.seconds + 2)
                continue

            if not batch:
                break

            for user in batch:
                if not isinstance(user, User):
                    continue
                if self.p_cfg.skip_bots and user.bot:
                    continue
                if not user.id:
                    continue

                # Activity filter
                if not activity_within_hours(user.status, self.p_cfg.activity_hours):
                    continue

                last_seen = get_last_seen_str(user.status)

                await self.db.upsert_parsed_user(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    last_seen=last_seen,
                    source_group=group_ref,
                    is_bot=bool(user.bot),
                )
                new_count += 1

            collected += len(batch)
            offset += len(batch)

            # Polite delay between batches
            await human_delay(self.p_cfg.batch_delay, jitter_ratio=0.5)

            # If we got fewer than requested, we've hit the end
            if len(batch) < batch_size:
                break

        return new_count

    async def _fetch_participants_batch(
        self, entity, offset: int, limit: int
    ) -> List[User]:
        """Fetch a single batch of participants from a group."""
        try:
            result = await self.client(
                GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsSearch(""),
                    offset=offset,
                    limit=limit,
                    hash=0,
                )
            )
            return result.users
        except Exception as exc:
            logger.debug("GetParticipants error at offset %d: %s", offset, exc)
            raise

    async def _resolve_entity(self, group_ref: str) -> Optional[object]:
        """Resolve a group reference to a Telethon entity."""
        try:
            entity = await self.client.get_entity(group_ref)
            return entity
        except ValueError:
            # Try with @ prefix
            try:
                entity = await self.client.get_entity(f"@{group_ref}")
                return entity
            except Exception as exc:
                logger.warning("Cannot resolve group @%s: %s", group_ref, exc)
                return None
        except Exception as exc:
            logger.warning("Cannot resolve group %r: %s", group_ref, exc)
            return None

    # ------------------------------------------------------------------
    # Public: manual parse trigger for admin
    # ------------------------------------------------------------------

    async def run_once(self) -> int:
        """Run a single parse cycle. Returns total new users collected."""
        if self.is_running():
            logger.info("Parser is already running a scheduled cycle")
        before = self._parse_count_session
        await self._parse_all_groups()
        return self._parse_count_session - before

    async def get_source_groups(self) -> List[Dict]:
        """Return list of source groups for display in admin panel."""
        return await self.db.get_active_source_groups()
