"""
poster_hybrid.py — юзербот ЧИТАЕТ источник, бот ПУБЛИКУЕТ в ваш канал.

Так можно:
  • не делать бота админом в чужом канале-источнике;
  • публиковать туда, где админ именно @бот (ваш канал).
"""

from __future__ import annotations

import asyncio
import logging
import random
import tempfile
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from pyrogram import Client
from pyrogram.errors import ChannelPrivate, ChatWriteForbidden, FloodWait, RPCError
from pyrogram.types import Message

import config
from database import Database
from links import parse_post_link

logger = logging.getLogger(__name__)


def _chat_ref(value: str | int) -> str | int:
    if isinstance(value, int):
        return value
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Канал не задан")
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        return f"@{raw}"


def _is_media(msg: Message) -> bool:
    return bool(
        msg.photo
        or msg.video
        or msg.document
        or msg.audio
        or msg.animation
        or msg.voice
        or msg.video_note
    )


class HybridPoster:
    """Чтение через Pyrogram, публикация через aiogram Bot."""

    def __init__(self, client: Client, bot: Bot, db: Database) -> None:
        self.client = client
        self.bot = bot
        self.db = db
        self._seen_grouped: set[str] = set()
        self._rewrite_cancel = False
        self._tmp = Path(tempfile.mkdtemp(prefix="reposter_"))

    async def apply_start_link(self, link: str) -> tuple[str | int, int]:
        chat_ref, message_id = parse_post_link(link)
        self.db.set_source_channel(str(chat_ref) if isinstance(chat_ref, int) else str(chat_ref))
        # normalize @
        src = str(chat_ref)
        if isinstance(chat_ref, str) and not chat_ref.startswith("@"):
            src = "@" + chat_ref
            self.db.set_source_channel(src)
        self.db.set_start_link(link)
        self.db.set_progress_id(message_id)
        return chat_ref, message_id

    def cancel_rewrite(self) -> None:
        self._rewrite_cancel = True

    async def run_cycle(self) -> int:
        settings = self.db.get_settings()
        if not settings.is_running:
            return 0
        source = _chat_ref(settings.source_channel or config.SOURCE_CHANNEL)
        target = _chat_ref(settings.target_channel or config.TARGET_CHANNEL)
        if settings.progress_id <= 0:
            logger.warning("Нет progress_id")
            return 0

        limit = settings.posts_per_cycle
        caption = settings.caption_template
        next_id = settings.progress_id + 1
        published = 0
        empty_streak = 0
        self._seen_grouped.clear()

        logger.info("Hybrid cycle %s → %s after=%s limit=%s", source, target, settings.progress_id, limit)

        while published < limit and empty_streak < 50:
            if not self.db.get_settings().is_running:
                break
            if self.db.was_processed(next_id):
                self.db.set_progress_id(next_id)
                next_id += 1
                continue

            try:
                result = await self._process(source, target, next_id, caption)
            except FloodWait as e:
                await asyncio.sleep(int(e.value) + 1)
                continue
            except ChatWriteForbidden:
                logger.error("Нет прав писать в назначение (для бота проверьте админку)")
                break
            except (ChannelPrivate, RPCError) as e:
                # Не двигаем progress на фатальных ошибках записи
                if "WRITE_FORBIDDEN" in str(e).upper() or "CHAT_WRITE" in str(e).upper():
                    logger.error("Write forbidden: %s", e)
                    break
                logger.error("RPC %s: %s", next_id, e)
                self.db.add_history(next_id, status="error", error=str(e))
                self.db.set_progress_id(next_id)
                next_id += 1
                empty_streak += 1
                continue

            if result == "empty":
                empty_streak += 1
                self.db.set_progress_id(next_id)
                next_id += 1
                continue
            if result == "skip":
                empty_streak = 0
                self.db.set_progress_id(next_id)
                next_id += 1
                continue
            if result == "fatal":
                break

            empty_streak = 0
            published += 1
            self.db.set_progress_id(next_id)
            next_id += 1
            if published < limit:
                await asyncio.sleep(random.uniform(config.POST_DELAY_MIN, config.POST_DELAY_MAX))

        logger.info("Hybrid done published=%s", published)
        return published

    async def _process(self, source, target, message_id: int, caption: str) -> str:
        try:
            msg = await self.client.get_messages(source, message_id)
        except FloodWait:
            raise
        except RPCError as e:
            if "MSG_ID_INVALID" in str(e).upper():
                return "empty"
            raise

        if msg is None or getattr(msg, "empty", False):
            return "empty"

        if msg.media_group_id:
            gid = str(msg.media_group_id)
            if gid in self._seen_grouped:
                return "skip"
            return await self._publish_album(source, target, msg, caption)

        if not _is_media(msg) and not (msg.text or msg.caption):
            return "skip"
        return await self._publish_single(target, msg, caption)

    async def _download(self, msg: Message) -> Optional[Path]:
        """Скачать медиа сообщения во временный файл."""
        try:
            path = await self.client.download_media(msg, file_name=str(self._tmp) + "/")
            if not path:
                return None
            return Path(path)
        except FloodWait:
            raise
        except Exception:
            logger.exception("download fail id=%s", msg.id)
            return None

    async def _publish_single(self, target, msg: Message, caption: str) -> str:
        try:
            if _is_media(msg):
                path = await self._download(msg)
                if path is None:
                    self.db.add_history(msg.id, status="error", error="download failed")
                    return "skip"
                try:
                    file = FSInputFile(path)
                    kwargs = {"chat_id": target, "caption": caption or None}
                    if caption:
                        kwargs["parse_mode"] = ParseMode.HTML
                    if msg.photo:
                        sent = await self.bot.send_photo(photo=file, **kwargs)
                    elif msg.video or msg.animation:
                        sent = await self.bot.send_video(video=file, **kwargs)
                    elif msg.audio:
                        sent = await self.bot.send_audio(audio=file, **kwargs)
                    else:
                        sent = await self.bot.send_document(document=file, **kwargs)
                finally:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                text = caption if caption else (msg.text or msg.caption or "")
                if not text:
                    return "skip"
                sent = await self.bot.send_message(
                    chat_id=target,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )

            self.db.add_history(
                source_message_id=msg.id,
                target_message_id=sent.message_id,
                status="ok",
            )
            logger.info("Hybrid published %s → %s", msg.id, sent.message_id)
            return "ok"
        except TelegramRetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            return await self._publish_single(target, msg, caption)
        except Exception as e:
            err = str(e).upper()
            if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err or "FORBIDDEN" in err:
                logger.error("Fatal write: %s", e)
                return "fatal"
            logger.error("publish single %s: %s", msg.id, e)
            self.db.add_history(msg.id, status="error", error=str(e))
            return "skip"

    async def _publish_album(self, source, target, anchor: Message, caption: str) -> str:
        gid = str(anchor.media_group_id)
        self._seen_grouped.add(gid)
        try:
            album = await self.client.get_media_group(source, anchor.id)
        except Exception as e:
            self.db.add_history(anchor.id, grouped_id=gid, status="error", error=str(e))
            return "skip"

        album = sorted(album, key=lambda m: m.id)
        media = []
        paths: list[Path] = []
        try:
            for i, m in enumerate(album):
                path = await self._download(m)
                if path is None:
                    continue
                paths.append(path)
                file = FSInputFile(path)
                cap = caption if i == 0 else None
                parse = ParseMode.HTML if (i == 0 and caption) else None
                if m.photo:
                    media.append(InputMediaPhoto(media=file, caption=cap, parse_mode=parse))
                elif m.video or m.animation:
                    media.append(InputMediaVideo(media=file, caption=cap, parse_mode=parse))
                else:
                    media.append(InputMediaDocument(media=file, caption=cap, parse_mode=parse))

            if not media:
                return "skip"

            sent_list = await self.bot.send_media_group(chat_id=target, media=media)
            first_id = sent_list[0].message_id if sent_list else None
            for m in album:
                self.db.add_history(
                    source_message_id=m.id,
                    target_message_id=first_id,
                    grouped_id=gid,
                    status="ok",
                )
            logger.info("Hybrid album %s (%s files) → %s", gid, len(media), first_id)
            return "ok"
        except TelegramRetryAfter as e:
            self._seen_grouped.discard(gid)
            await asyncio.sleep(int(e.retry_after) + 1)
            return await self._publish_album(source, target, anchor, caption)
        except Exception as e:
            err = str(e).upper()
            if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                logger.error("Fatal album write: %s", e)
                return "fatal"
            logger.error("album fail %s: %s", gid, e)
            self.db.add_history(anchor.id, grouped_id=gid, status="error", error=str(e))
            return "skip"
        finally:
            for p in paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    async def rewrite_captions_in_channel(self, channel, *, caption=None, max_posts=None):
        """Правка подписей через Bot API (сообщения бота)."""
        from poster_botapi import ChannelPosterBotAPI

        api = ChannelPosterBotAPI(self.bot, self.db)
        return await api.rewrite_captions_in_channel(
            channel, caption=caption, max_posts=max_posts
        )
