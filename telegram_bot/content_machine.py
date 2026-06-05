"""
Content Machine — monitors donor Telegram channels and automatically
reposts their media content to a target channel, appending an advertising
footer and stripping media metadata to avoid duplicate detection.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from telethon import TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    MessageIdInvalidError,
    RPCError,
    UserDeactivatedBanError,
)
from telethon.tl.types import (
    DocumentAttributeVideo,
    InputChannel,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    PeerChannel,
    UpdateNewChannelMessage,
)

from config import AppConfig
from database import Database
from utils import (
    human_delay,
    normalise_channel_ref,
    strip_file_metadata,
    truncate,
    vary_caption,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State dataclass for a single donor channel
# ---------------------------------------------------------------------------

@dataclass
class DonorState:
    username: str
    channel_entity: Optional[object] = None
    last_message_id: int = 0
    posts_today: int = 0
    last_post_time: float = 0.0
    consecutive_errors: int = 0
    is_accessible: bool = True


# ---------------------------------------------------------------------------
# Content Machine
# ---------------------------------------------------------------------------

class ContentMachine:
    """
    Monitors donor channels via a polling loop (compatible with multi-account
    setups without requiring the account to join the channel).

    Architecture
    ────────────
    • A single Telethon client authenticates with the bot-owner's user account.
    • Every `check_interval` seconds it fetches messages newer than the last
      seen message ID from every active donor channel.
    • Qualifying posts are downloaded, metadata-stripped, and forwarded to the
      target channel with the configured advertising caption / inline button.
    • Daily post cap and minimum inter-post delay prevent flooding.
    """

    def __init__(self, client: TelegramClient, db: Database, cfg: AppConfig):
        self.client = client
        self.db = db
        self.cfg = cfg
        self.cm_cfg = cfg.content_machine

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._donors: Dict[str, DonorState] = {}

        # In-memory set of file_unique_ids we have already reposted this session
        self._seen_file_ids: Set[str] = set()

        self._posts_today = 0
        self._last_day = datetime.now().date()

    # ------------------------------------------------------------------
    # Public control interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background monitoring loop."""
        if self._running:
            logger.warning("ContentMachine already running")
            return

        await self._load_donors_from_db()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="content_machine_loop")
        logger.info("ContentMachine started (%d donors)", len(self._donors))

    async def stop(self) -> None:
        """Stop the background loop gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ContentMachine stopped")

    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def add_donor(self, username: str) -> bool:
        """Add a donor channel at runtime."""
        username = normalise_channel_ref(username)
        if username in self._donors:
            return True
        await self.db.add_donor_channel(username)
        self._donors[username] = DonorState(username=username)
        logger.info("Donor channel added: @%s", username)
        return True

    async def remove_donor(self, username: str) -> bool:
        """Remove a donor channel."""
        username = normalise_channel_ref(username)
        await self.db.remove_donor_channel(username)
        self._donors.pop(username, None)
        logger.info("Donor channel removed: @%s", username)
        return True

    def get_status(self) -> Dict:
        return {
            "running": self.is_running(),
            "donors": len(self._donors),
            "posts_today": self._posts_today,
            "max_posts_per_day": self.cm_cfg.max_posts_per_day,
            "target_channel": self.cm_cfg.target_channel,
        }

    # ------------------------------------------------------------------
    # Internal: initialisation
    # ------------------------------------------------------------------

    async def _load_donors_from_db(self) -> None:
        rows = await self.db.get_active_donor_channels()
        for row in rows:
            uname = row["username"]
            state = DonorState(
                username=uname,
                last_message_id=row.get("last_message_id", 0) or 0,
            )
            self._donors[uname] = state
        # Also load donors from config if any
        for uname in self.cm_cfg.donor_channels:
            uname = normalise_channel_ref(uname)
            if uname not in self._donors:
                await self.db.add_donor_channel(uname)
                self._donors[uname] = DonorState(username=uname)

    # ------------------------------------------------------------------
    # Internal: main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        logger.info("ContentMachine loop started")
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ContentMachine loop error: %s", exc, exc_info=True)
                await asyncio.sleep(30)
                continue

            await asyncio.sleep(self.cm_cfg.check_interval)

        logger.info("ContentMachine loop exited")

    async def _tick(self) -> None:
        """Single tick: check all donors for new posts."""
        self._reset_daily_if_needed()

        if self._posts_today >= self.cm_cfg.max_posts_per_day:
            logger.debug(
                "Daily post cap reached (%d/%d). Sleeping until next day.",
                self._posts_today, self.cm_cfg.max_posts_per_day,
            )
            return

        for username, state in list(self._donors.items()):
            if not state.is_accessible:
                continue
            await self._check_donor(username, state)

    # ------------------------------------------------------------------
    # Internal: per-donor processing
    # ------------------------------------------------------------------

    async def _check_donor(self, username: str, state: DonorState) -> None:
        """Fetch new messages from one donor channel and process them."""
        try:
            entity = await self._resolve_entity(username, state)
            if entity is None:
                return

            messages = await self._fetch_new_messages(entity, state.last_message_id)
            if not messages:
                return

            logger.info("@%s → %d new messages to process", username, len(messages))

            for msg in messages:
                if not self._running:
                    return
                if self._posts_today >= self.cm_cfg.max_posts_per_day:
                    return

                posted = await self._process_message(msg, username)
                if posted:
                    state.last_message_id = max(state.last_message_id, msg.id)
                    await self.db.update_donor_last_message(username, msg.id)
                    self._posts_today += 1
                    state.posts_today += 1
                    state.last_post_time = time.monotonic()
                    # Respect minimum delay between consecutive posts
                    await human_delay(self.cm_cfg.repost_min_delay, jitter_ratio=0.2)
                else:
                    # Even for skipped messages, update the watermark
                    state.last_message_id = max(state.last_message_id, msg.id)
                    await self.db.update_donor_last_message(username, msg.id)

            state.consecutive_errors = 0

        except FloodWaitError as exc:
            wait = exc.seconds + 5
            logger.warning("FloodWait from @%s: sleeping %ds", username, wait)
            await asyncio.sleep(wait)
        except ChannelPrivateError:
            logger.warning("@%s is private / inaccessible, disabling donor", username)
            state.is_accessible = False
            await self.db.remove_donor_channel(username)
        except RPCError as exc:
            state.consecutive_errors += 1
            logger.error("RPC error processing @%s: %s (streak: %d)",
                         username, exc, state.consecutive_errors)
            if state.consecutive_errors >= 10:
                logger.warning("Too many errors for @%s, disabling temporarily", username)
                state.is_accessible = False
        except Exception as exc:
            logger.error("Unexpected error for donor @%s: %s", username, exc, exc_info=True)

    async def _resolve_entity(self, username: str, state: DonorState) -> Optional[object]:
        """Resolve and cache the channel entity."""
        if state.channel_entity is not None:
            return state.channel_entity
        try:
            entity = await self.client.get_entity(username)
            state.channel_entity = entity
            # Update DB with resolved channel ID and title
            channel_id = getattr(entity, "id", None)
            title = getattr(entity, "title", username)
            if channel_id:
                await self.db.add_donor_channel(username, channel_id=channel_id, title=title)
            return entity
        except Exception as exc:
            logger.warning("Cannot resolve entity @%s: %s", username, exc)
            return None

    async def _fetch_new_messages(
        self, entity, last_message_id: int
    ) -> List[Message]:
        """
        Return messages newer than last_message_id that contain media.
        Oldest-first so we process and persist watermarks in order.
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(
            hours=self.cm_cfg.max_post_age_hours
        )
        messages = []
        async for msg in self.client.iter_messages(
            entity,
            min_id=last_message_id,
            reverse=True,
            limit=50,
        ):
            if not isinstance(msg, Message):
                continue
            if not msg.media:
                continue
            if isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)):
                pass  # accept these
            else:
                continue  # skip polls, geo, etc.

            msg_date = msg.date
            if msg_date and msg_date < cutoff_time:
                continue

            if self._should_skip_message(msg):
                continue

            messages.append(msg)

        return messages

    def _should_skip_message(self, msg: Message) -> bool:
        """Return True if the message should be skipped based on config rules."""
        text = (msg.text or msg.message or "").lower()
        for kw in self.cm_cfg.skip_keywords:
            if kw.lower() in text:
                logger.debug("Skipping msg %d (keyword: %s)", msg.id, kw)
                return True
        return False

    # ------------------------------------------------------------------
    # Internal: message processing
    # ------------------------------------------------------------------

    async def _process_message(self, msg: Message, donor_username: str) -> bool:
        """
        Download, process, and repost a single message.
        Returns True if the message was successfully reposted.
        """
        # Check deduplication
        if await self.db.is_already_reposted(donor_username, msg.id):
            logger.debug("Already reposted: @%s msg %d", donor_username, msg.id)
            return False

        # Check file_unique_id deduplication (Telegram's native dedupe)
        file_uid = self._get_file_unique_id(msg)
        if file_uid and file_uid in self._seen_file_ids:
            logger.debug("Duplicate file_unique_id detected, skipping msg %d", msg.id)
            await self.db.mark_as_reposted(donor_username, msg.id, file_uid)
            return False

        # Download media to temp file
        media_path = await self._download_media(msg)
        if not media_path:
            return False

        try:
            # Strip metadata if configured
            if self.cm_cfg.strip_metadata:
                media_path = await strip_file_metadata(media_path)

            # Build caption
            original_caption = msg.text or msg.message or ""
            caption = vary_caption(
                original_caption,
                self.cm_cfg.ad_text,
                obfuscate=self.cm_cfg.obfuscate_caption,
            )
            # Telegram caption max 1024 chars
            caption = caption[:1024]

            # Build inline keyboard if ad button configured
            buttons = None
            if self.cm_cfg.ad_button_url:
                from telethon.tl.types import ReplyInlineMarkup, KeyboardButtonUrl, KeyboardButtonRow
                buttons = ReplyInlineMarkup(rows=[
                    KeyboardButtonRow(buttons=[
                        KeyboardButtonUrl(
                            text=self.cm_cfg.ad_button_text,
                            url=self.cm_cfg.ad_button_url,
                        )
                    ])
                ])

            # Send to target channel
            target = self.cm_cfg.target_channel
            sent_msg = await self._send_to_target(media_path, caption, buttons, msg, target)

            if sent_msg:
                target_msg_id = sent_msg.id if hasattr(sent_msg, "id") else None
                await self.db.mark_as_reposted(
                    donor_username, msg.id, file_uid, target_msg_id
                )
                if file_uid:
                    self._seen_file_ids.add(file_uid)
                logger.info(
                    "Reposted @%s msg %d → target msg %s",
                    donor_username, msg.id, target_msg_id,
                )
                return True
            return False

        finally:
            # Clean up temp file
            if media_path and Path(media_path).exists():
                try:
                    Path(media_path).unlink()
                except OSError:
                    pass

    async def _download_media(self, msg: Message) -> Optional[str]:
        """Download message media to temp directory. Returns file path."""
        media_dir = Path(self.cm_cfg.media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)

        try:
            path = await self.client.download_media(
                msg,
                file=str(media_dir),
            )
            if path and Path(path).exists():
                logger.debug("Downloaded media: %s", path)
                return path
            logger.warning("Download returned no path for msg %d", msg.id)
            return None
        except FloodWaitError as exc:
            logger.warning("FloodWait during download: sleeping %ds", exc.seconds)
            await asyncio.sleep(exc.seconds + 2)
            return None
        except Exception as exc:
            logger.error("Media download failed for msg %d: %s", msg.id, exc)
            return None

    async def _send_to_target(
        self,
        media_path: str,
        caption: str,
        buttons,
        original_msg: Message,
        target: str,
    ):
        """Upload media file to target channel and return the sent message."""
        target_ref = normalise_channel_ref(target)
        try:
            sent = await self.client.send_file(
                target_ref,
                file=media_path,
                caption=caption,
                buttons=buttons,
                supports_streaming=True,
                parse_mode="md",
            )
            return sent
        except FloodWaitError as exc:
            logger.warning("FloodWait on send: sleeping %ds", exc.seconds + 2)
            await asyncio.sleep(exc.seconds + 2)
            # Retry once
            try:
                return await self.client.send_file(
                    target_ref,
                    file=media_path,
                    caption=caption,
                    buttons=buttons,
                    supports_streaming=True,
                    parse_mode="md",
                )
            except Exception as exc2:
                logger.error("Retry send failed: %s", exc2)
                return None
        except Exception as exc:
            logger.error("Failed to send to target channel: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------

    def _get_file_unique_id(self, msg: Message) -> Optional[str]:
        """Extract a file unique identifier from the message media."""
        media = msg.media
        if isinstance(media, MessageMediaPhoto):
            photo = media.photo
            if hasattr(photo, "file_unique_id"):
                return photo.file_unique_id
            return str(getattr(photo, "id", ""))
        elif isinstance(media, MessageMediaDocument):
            doc = media.document
            if hasattr(doc, "file_unique_id"):
                return doc.file_unique_id
            return str(getattr(doc, "id", ""))
        return None

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now().date()
        if today != self._last_day:
            self._posts_today = 0
            self._last_day = today
            logger.info("Daily post counter reset for ContentMachine")

    # ------------------------------------------------------------------
    # Public: channel management helpers for admin bot
    # ------------------------------------------------------------------

    async def get_donor_list(self) -> List[Dict]:
        """Return list of donor channel states for display."""
        result = []
        for username, state in self._donors.items():
            result.append({
                "username": username,
                "last_message_id": state.last_message_id,
                "posts_today": state.posts_today,
                "is_accessible": state.is_accessible,
                "consecutive_errors": state.consecutive_errors,
            })
        return result

    async def force_check(self) -> int:
        """Manually trigger a check of all donors. Returns number of posts made."""
        before = self._posts_today
        await self._tick()
        return self._posts_today - before
