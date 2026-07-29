"""
poster.py — чистый USERBOT-перезалив через Pyrogram.

Аккаунт:
  • в источнике — подписчик (админ не обязателен);
  • в назначении — админ с правом постить.

Копирует без метки «Forwarded from», альбомы целиком, HTML-подпись.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from pyrogram import Client, enums
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatWriteForbidden,
    FloodWait,
    MessageIdInvalid,
    RPCError,
)
from pyrogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

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


def _build_input_media(msg: Message, caption: Optional[str] = None):
    parse_mode = enums.ParseMode.HTML if caption else None
    if msg.photo:
        return InputMediaPhoto(media=msg.photo.file_id, caption=caption, parse_mode=parse_mode)
    if msg.video:
        return InputMediaVideo(media=msg.video.file_id, caption=caption, parse_mode=parse_mode)
    if msg.animation:
        return InputMediaDocument(media=msg.animation.file_id, caption=caption, parse_mode=parse_mode)
    if msg.audio:
        return InputMediaAudio(media=msg.audio.file_id, caption=caption, parse_mode=parse_mode)
    if msg.document:
        return InputMediaDocument(media=msg.document.file_id, caption=caption, parse_mode=parse_mode)
    if msg.voice:
        return InputMediaDocument(media=msg.voice.file_id, caption=caption, parse_mode=parse_mode)
    return None


class ChannelPoster:
    """Движок: юзербот читает источник и публикует в назначение."""

    def __init__(self, client: Client, db: Database) -> None:
        self.client = client
        self.db = db
        self._seen_grouped: set[str] = set()
        self._rewrite_cancel = False
        self._busy = False

    # ------------------------------------------------------------------ API

    async def apply_start_link(self, link: str) -> tuple[str | int, int]:
        """Старт ПОСЛЕ указанного поста (сам пост не публикуется)."""
        chat_ref, message_id = parse_post_link(link)
        if isinstance(chat_ref, int):
            src = str(chat_ref)
        else:
            src = chat_ref if chat_ref.startswith("@") else f"@{chat_ref}"
        self.db.set_source_channel(src)
        self.db.set_start_link(link)
        self.db.set_progress_id(message_id)
        logger.info("Start link → chat=%s after_id=%s (next=%s)", src, message_id, message_id + 1)
        return chat_ref, message_id

    async def seek_oldest(self) -> int:
        """Найти первый существующий пост в источнике; progress = id-1."""
        settings = self.db.get_settings()
        source = _chat_ref(settings.source_channel or config.SOURCE_CHANNEL)
        found = 0
        empty = 0
        for mid in range(1, 20001):
            try:
                msg = await self.client.get_messages(source, mid)
            except FloodWait as e:
                await asyncio.sleep(min(int(e.value), 30) + 1)
                continue
            except MessageIdInvalid:
                empty += 1
                if empty > 30 and found == 0:
                    break
                continue
            except RPCError:
                empty += 1
                continue

            if msg is None or getattr(msg, "empty", False):
                empty += 1
                if empty > 30 and found == 0:
                    break
                continue

            found = mid
            break

        if found <= 0:
            raise RuntimeError("Не удалось найти первый пост в источнике")
        self.db.set_progress_id(found - 1)
        self.db.set_start_link(f"oldest:{found}")
        logger.info("Oldest post id=%s → progress=%s", found, found - 1)
        return found

    def cancel_rewrite(self) -> None:
        self._rewrite_cancel = True

    async def run_cycle(self) -> int:
        if self._busy:
            logger.warning("cycle skipped: busy")
            return 0
        self._busy = True
        try:
            return await self._run_cycle_inner()
        finally:
            self._busy = False

    async def _run_cycle_inner(self) -> int:
        settings = self.db.get_settings()
        if not settings.is_running:
            return 0

        source = _chat_ref(settings.source_channel or config.SOURCE_CHANNEL)
        target = _chat_ref(settings.target_channel or config.TARGET_CHANNEL)
        if settings.progress_id < 0:
            logger.warning("progress_id не задан")
            return 0

        limit = settings.posts_per_cycle
        caption = settings.caption_template or ""
        next_id = settings.progress_id + 1
        published = 0
        empty_streak = 0
        self._seen_grouped.clear()

        logger.info(
            "USERBOT cycle %s → %s after=%s limit=%s",
            source,
            target,
            settings.progress_id,
            limit,
        )

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
                wait = min(int(e.value), 120)
                logger.warning("FloodWait %ss", wait)
                await asyncio.sleep(wait + 1)
                continue
            except ChatWriteForbidden:
                logger.error("Нет прав писать в назначение (нужен админ юзербота)")
                break
            except (ChannelPrivate, ChannelInvalid) as e:
                logger.error("Источник недоступен: %s", e)
                break
            except RPCError as e:
                err = str(e).upper()
                if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
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
                cur = max(self.db.get_progress_id(), next_id)
                self.db.set_progress_id(cur)
                next_id = cur + 1
                continue
            if result == "fatal":
                logger.error("Fatal — ставлю автопост на паузу")
                self.db.set_running(False)
                break

            empty_streak = 0
            published += 1
            cur = max(self.db.get_progress_id(), next_id)
            self.db.set_progress_id(cur)
            next_id = cur + 1
            if published < limit:
                await asyncio.sleep(
                    random.uniform(config.POST_DELAY_MIN, config.POST_DELAY_MAX)
                )

        logger.info("USERBOT done published=%s", published)
        return published

    # ------------------------------------------------------------------ process

    async def _process(
        self, source, target, message_id: int, caption: str
    ) -> str:
        try:
            msg = await self.client.get_messages(source, message_id)
        except MessageIdInvalid:
            return "empty"
        except FloodWait:
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

    async def _publish_single(self, target, msg: Message, caption: str) -> str:
        try:
            if _is_media(msg):
                try:
                    sent = await self.client.copy_message(
                        chat_id=target,
                        from_chat_id=msg.chat.id,
                        message_id=msg.id,
                        caption=caption or None,
                        parse_mode=enums.ParseMode.HTML if caption else None,
                    )
                except RPCError as e:
                    if caption and "parse" in str(e).lower():
                        sent = await self.client.copy_message(
                            chat_id=target,
                            from_chat_id=msg.chat.id,
                            message_id=msg.id,
                            caption=caption,
                        )
                    else:
                        raise
            else:
                text = caption if caption else (msg.text or msg.caption or "")
                if not text:
                    return "skip"
                try:
                    sent = await self.client.send_message(
                        chat_id=target,
                        text=text,
                        parse_mode=enums.ParseMode.HTML,
                    )
                except RPCError as e:
                    if "parse" in str(e).lower():
                        sent = await self.client.send_message(chat_id=target, text=text)
                    else:
                        raise

            tid = sent.id if isinstance(sent, Message) else None
            self.db.add_history(msg.id, target_message_id=tid, status="ok")
            logger.info("published %s → %s", msg.id, tid)
            return "ok"
        except FloodWait:
            raise
        except ChatWriteForbidden:
            return "fatal"
        except RPCError as e:
            err = str(e).upper()
            if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                return "fatal"
            logger.error("publish %s: %s", msg.id, e)
            self.db.add_history(msg.id, status="error", error=str(e))
            return "skip"

    async def _publish_album(
        self, source, target, anchor: Message, caption: str
    ) -> str:
        """Альбом одним media_group — без дробления на отдельные посты."""
        gid = str(anchor.media_group_id)
        self._seen_grouped.add(gid)
        try:
            album = await self.client.get_media_group(source, anchor.id)
        except Exception as e:
            self.db.add_history(anchor.id, grouped_id=gid, status="error", error=str(e))
            return "skip"

        album = sorted(album, key=lambda m: m.id)
        captions: list[str] = [
            (caption if i == 0 and caption else "") for i, _ in enumerate(album)
        ]

        sent_list = None
        try:
            try:
                sent_list = await self.client.copy_media_group(
                    chat_id=target,
                    from_chat_id=anchor.chat.id,
                    message_id=anchor.id,
                    captions=captions,
                )
            except (ValueError, RPCError) as e:
                logger.warning("copy_media_group fallback to file_id: %s", e)
                media_list = []
                for i, m in enumerate(album):
                    item = _build_input_media(
                        m, caption=caption if i == 0 and caption else None
                    )
                    if item is not None:
                        media_list.append(item)
                if not media_list:
                    return "skip"
                try:
                    sent_list = await self.client.send_media_group(
                        chat_id=target, media=media_list
                    )
                except RPCError as e2:
                    if caption and "parse" in str(e2).lower():
                        media_list = []
                        for i, m in enumerate(album):
                            item = _build_input_media(
                                m, caption=caption if i == 0 and caption else None
                            )
                            if item is not None:
                                # без parse_mode
                                if hasattr(item, "parse_mode"):
                                    item.parse_mode = None
                                media_list.append(item)
                        sent_list = await self.client.send_media_group(
                            chat_id=target, media=media_list
                        )
                    else:
                        raise

            first_id = sent_list[0].id if sent_list else None
            # если copy без подписи — допишем на первый
            if caption and sent_list and not (sent_list[0].caption or ""):
                try:
                    await self.client.edit_message_caption(
                        chat_id=target,
                        message_id=sent_list[0].id,
                        caption=caption,
                        parse_mode=enums.ParseMode.HTML,
                    )
                except RPCError:
                    try:
                        await self.client.edit_message_caption(
                            chat_id=target,
                            message_id=sent_list[0].id,
                            caption=caption,
                        )
                    except RPCError as e:
                        logger.warning("caption edit fail: %s", e)

            max_src = max(m.id for m in album)
            for m in album:
                self.db.add_history(
                    source_message_id=m.id,
                    target_message_id=first_id,
                    grouped_id=gid,
                    status="ok",
                )
            self.db.set_progress_id(max_src)
            logger.info(
                "album %s (%s files) → %s progress=%s",
                gid,
                len(album),
                first_id,
                max_src,
            )
            return "ok"
        except FloodWait:
            self._seen_grouped.discard(gid)
            raise
        except ChatWriteForbidden:
            return "fatal"
        except (RPCError, ValueError) as e:
            err = str(e).upper()
            if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                return "fatal"
            logger.error("album %s: %s", gid, e)
            self.db.add_history(anchor.id, grouped_id=gid, status="error", error=str(e))
            return "skip"

    # ------------------------------------------------------------------ rewrite

    async def rewrite_captions_in_channel(
        self,
        channel: str | int,
        *,
        caption: Optional[str] = None,
        max_posts: Optional[int] = None,
    ) -> dict:
        chat = _chat_ref(channel)
        text = caption if caption is not None else (self.db.get_settings().caption_template or "")
        if not text.strip():
            raise ValueError("Сначала задайте шаблон описания")

        self._rewrite_cancel = False
        updated = skipped = errors = scanned = 0
        seen_groups: set[str] = set()
        cancelled = False

        async for msg in self.client.get_chat_history(chat):
            if self._rewrite_cancel:
                cancelled = True
                break
            if max_posts is not None and scanned >= max_posts:
                break
            scanned += 1

            if msg.media_group_id:
                gid = str(msg.media_group_id)
                if gid in seen_groups:
                    continue
                seen_groups.add(gid)

            try:
                if _is_media(msg) or msg.caption is not None:
                    try:
                        await self.client.edit_message_caption(
                            chat_id=chat,
                            message_id=msg.id,
                            caption=text,
                            parse_mode=enums.ParseMode.HTML,
                        )
                    except RPCError:
                        await self.client.edit_message_caption(
                            chat_id=chat,
                            message_id=msg.id,
                            caption=text,
                        )
                    updated += 1
                elif msg.text:
                    try:
                        await self.client.edit_message_text(
                            chat_id=chat,
                            message_id=msg.id,
                            text=text,
                            parse_mode=enums.ParseMode.HTML,
                        )
                    except RPCError:
                        await self.client.edit_message_text(
                            chat_id=chat, message_id=msg.id, text=text
                        )
                    updated += 1
                else:
                    skipped += 1
            except FloodWait as e:
                await asyncio.sleep(min(int(e.value), 60) + 1)
            except RPCError:
                errors += 1
            await asyncio.sleep(random.uniform(1.2, 2.5))

        return {
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "scanned": scanned,
            "cancelled": cancelled,
        }
