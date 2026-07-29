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
from links import normalize_channel, parse_post_link

logger = logging.getLogger(__name__)


def _chat_ref(value: str | int) -> str | int:
    if isinstance(value, int):
        return value
    raw = normalize_channel(value)
    if not raw:
        raise ValueError("Канал не задан")
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


async def _resolve_chat(client: Client, value: str | int) -> int:
    """
    Вернуть числовой chat_id. Для @username сначала ResolveUsername,
    чтобы в сессии был access_hash (иначе бывает CHANNEL_INVALID).
    """
    ref = _chat_ref(value)
    if isinstance(ref, int):
        return ref

    username = str(ref).lstrip("@")
    try:
        from pyrogram import raw

        r = await client.invoke(
            raw.functions.contacts.ResolveUsername(username=username)
        )
        if hasattr(r.peer, "channel_id"):
            return int(f"-100{r.peer.channel_id}")
        if hasattr(r.peer, "chat_id"):
            return -r.peer.chat_id
        if hasattr(r.peer, "user_id"):
            return int(r.peer.user_id)
    except Exception as e:
        logger.debug("ResolveUsername(%s) fallback: %s", username, e)

    chat = await client.get_chat(ref)
    return int(chat.id)


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


def _is_unsupported_media(msg: Message) -> bool:
    """Медиа, которое текущий слой Pyrogram не разобрал (MessageMediaUnsupported)."""
    if getattr(msg, "empty", False) or msg.service:
        return False
    if _is_media(msg) or msg.sticker or msg.poll or msg.dice:
        return False
    if msg.text or msg.caption:
        return False
    # В альбоме / «пустой» пост без текста — почти наверняка unsupported media
    return True


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


def _attach_caption(item, caption: Optional[str]):
    """Повесить подпись на уже собранный InputMedia*."""
    if item is None or not caption:
        return item
    item.caption = caption
    item.parse_mode = enums.ParseMode.HTML
    return item


class ChannelPoster:
    """Движок: юзербот читает источник и публикует в назначение."""

    def __init__(self, client: Client, db: Database) -> None:
        self.client = client
        self.db = db
        self._seen_grouped: set[str] = set()
        self._rewrite_cancel = False
        self._busy = False

    async def _materialize_message(self, msg: Message) -> Optional[Message]:
        """
        Получить сообщение с нормальным file_id.
        MessageMediaUnsupported → forward в Saved Messages (drop_author),
        откуда Pyrogram уже видит video/document.
        """
        if _build_input_media(msg) is not None:
            return msg
        if not _is_unsupported_media(msg):
            return None
        try:
            from pyrogram import raw

            r = await self.client.invoke(
                raw.functions.messages.ForwardMessages(
                    to_peer=await self.client.resolve_peer("me"),
                    from_peer=await self.client.resolve_peer(msg.chat.id),
                    id=[msg.id],
                    random_id=[self.client.rnd_id()],
                    drop_author=True,
                    drop_media_captions=True,
                )
            )
            new_id = None
            for u in r.updates:
                if hasattr(u, "id") and hasattr(u, "random_id") and not hasattr(u, "pts"):
                    # UpdateMessageID
                    new_id = u.id
                if hasattr(u, "message") and getattr(u.message, "id", None):
                    new_id = u.message.id
            if not new_id:
                logger.warning("materialize %s: no new message id in updates", msg.id)
                return None
            temp = await self.client.get_messages("me", new_id)
            if temp is None or getattr(temp, "empty", False):
                return None
            if _build_input_media(temp) is None:
                await self.client.delete_messages("me", new_id)
                logger.warning("materialize %s: still unsupported after forward", msg.id)
                return None
            # помечаем id для очистки после сборки file_id
            temp._reposter_tmp_id = new_id  # type: ignore[attr-defined]
            return temp
        except Exception as e:
            logger.warning("materialize %s failed: %s", msg.id, e)
            return None

    async def _input_media_from_msg(
        self, msg: Message, caption: Optional[str] = None
    ):
        """InputMedia* из сообщения; unsupported — через Saved Messages."""
        item = _build_input_media(msg, caption=None)
        tmp_id = None
        if item is None:
            temp = await self._materialize_message(msg)
            if temp is None:
                return None
            tmp_id = getattr(temp, "_reposter_tmp_id", None)
            item = _build_input_media(temp, caption=None)
            if tmp_id:
                try:
                    await self.client.delete_messages("me", tmp_id)
                except Exception:
                    pass
        if item is None:
            return None
        return _attach_caption(item, caption)

    # ------------------------------------------------------------------ API

    async def apply_start_link(self, link: str) -> tuple[str | int, int]:
        """Старт ПОСЛЕ указанного поста (сам пост не публикуется)."""
        chat_ref, message_id = parse_post_link(link)
        src = normalize_channel(chat_ref)
        self.db.set_source_channel(src)
        self.db.set_start_link(link)
        self.db.set_progress_id(message_id)
        logger.info("Start link → chat=%s after_id=%s (next=%s)", src, message_id, message_id + 1)
        return chat_ref, message_id

    async def seek_oldest(self) -> int:
        """Найти первый существующий пост в источнике; progress = id-1."""
        settings = self.db.get_settings()
        source = await _resolve_chat(
            self.client, settings.source_channel or config.SOURCE_CHANNEL
        )
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

        source = await _resolve_chat(
            self.client, settings.source_channel or config.SOURCE_CHANNEL
        )
        target = await _resolve_chat(
            self.client, settings.target_channel or config.TARGET_CHANNEL
        )
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

        if _is_media(msg) or (msg.text or msg.caption):
            return await self._publish_single(target, msg, caption)

        if _is_unsupported_media(msg):
            return await self._publish_single(target, msg, caption)

        return "skip"

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
            elif msg.text or msg.caption:
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
            else:
                # MessageMediaUnsupported и т.п. — материализуем и шлём с шаблоном
                item = await self._input_media_from_msg(msg, caption=caption or None)
                if item is None:
                    logger.warning("skip unsupported single %s", msg.id)
                    self.db.add_history(msg.id, status="error", error="unsupported media")
                    return "skip"
                media_type = type(item).__name__
                try:
                    if isinstance(item, InputMediaPhoto):
                        sent = await self.client.send_photo(
                            target,
                            item.media,
                            caption=caption or None,
                            parse_mode=enums.ParseMode.HTML if caption else None,
                        )
                    elif isinstance(item, InputMediaVideo):
                        sent = await self.client.send_video(
                            target,
                            item.media,
                            caption=caption or None,
                            parse_mode=enums.ParseMode.HTML if caption else None,
                        )
                    else:
                        sent = await self.client.send_document(
                            target,
                            item.media,
                            caption=caption or None,
                            parse_mode=enums.ParseMode.HTML if caption else None,
                        )
                except RPCError as e:
                    if caption and "parse" in str(e).lower():
                        if isinstance(item, InputMediaPhoto):
                            sent = await self.client.send_photo(
                                target, item.media, caption=caption
                            )
                        elif isinstance(item, InputMediaVideo):
                            sent = await self.client.send_video(
                                target, item.media, caption=caption
                            )
                        else:
                            sent = await self.client.send_document(
                                target, item.media, caption=caption
                            )
                    else:
                        logger.error("unsupported single %s (%s): %s", msg.id, media_type, e)
                        raise

            tid = sent.id if isinstance(sent, Message) else None
            # страховка: шаблон должен быть на медиа
            if (
                caption
                and isinstance(sent, Message)
                and _is_media(sent)
                and not (sent.caption or "").strip()
            ):
                logger.warning(
                    "single %s published without caption — template was expected",
                    msg.id,
                )
            self.db.add_history(msg.id, target_message_id=tid, status="ok")
            logger.info("published %s → %s", msg.id, tid)
            return "ok"
        except FloodWait:
            raise
        except ChatWriteForbidden:
            return "fatal"
        except (RPCError, ValueError) as e:
            err = str(e).upper()
            if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                return "fatal"
            logger.error("publish %s: %s", msg.id, e)
            self.db.add_history(msg.id, status="error", error=str(e))
            return "skip"

    async def _publish_album(
        self, source, target, anchor: Message, caption: str
    ) -> str:
        """Альбом одним media_group — подпись сразу при отправке, без edit."""
        gid = str(anchor.media_group_id)
        self._seen_grouped.add(gid)
        try:
            album = await self.client.get_media_group(source, anchor.id)
        except Exception as e:
            self.db.add_history(anchor.id, grouped_id=gid, status="error", error=str(e))
            return "skip"

        album = sorted(album, key=lambda m: m.id)
        sent_list = None
        try:
            if caption:
                # Шаблон задан — публикуем через send_media_group с caption сразу,
                # чтобы не было метки «изменено» от последующего edit.
                sent_list = await self._send_album_with_caption(target, album, caption)
            else:
                try:
                    sent_list = await self.client.copy_media_group(
                        chat_id=target,
                        from_chat_id=anchor.chat.id,
                        message_id=anchor.id,
                    )
                except (ValueError, RPCError) as e:
                    logger.warning("copy_media_group fallback to file_id: %s", e)
                    sent_list = await self._send_album_with_caption(target, album, "")

            first_id = sent_list[0].id if sent_list else None
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

    async def _send_album_with_caption(
        self, target, album: list[Message], caption: str
    ) -> list[Message]:
        """
        Собрать InputMedia* и отправить альбом.
        Подпись — на ПЕРВОМ успешно собранном медиа (не на index 0 альбома:
        первый файл часто MessageMediaUnsupported и раньше «съедал» шаблон).
        """
        media_list = []
        caption_pending = caption or None
        for m in album:
            item = await self._input_media_from_msg(m, caption=None)
            if item is None:
                logger.warning(
                    "album item %s skipped (unsupported/unreadable media)", m.id
                )
                continue
            if caption_pending:
                _attach_caption(item, caption_pending)
                caption_pending = None
            media_list.append(item)

        if not media_list:
            raise ValueError("album has no copyable media")
        if caption and caption_pending:
            # на всякий случай — шаблон так и не повесили
            _attach_caption(media_list[0], caption)

        try:
            sent = await self.client.send_media_group(chat_id=target, media=media_list)
        except RPCError as e:
            if caption and "parse" in str(e).lower():
                for item in media_list:
                    if getattr(item, "caption", None):
                        item.parse_mode = None
                sent = await self.client.send_media_group(
                    chat_id=target, media=media_list
                )
            else:
                raise

        if caption and sent and not (sent[0].caption or "").strip():
            # не редактируем (метки «изменено» нет) — пересылаем заново один раз
            logger.warning(
                "album sent without caption, retrying with explicit template on first media"
            )
            try:
                await self.client.delete_messages(target, [m.id for m in sent])
            except Exception:
                pass
            for item in media_list:
                item.caption = None
                item.parse_mode = None
            _attach_caption(media_list[0], caption)
            try:
                sent = await self.client.send_media_group(
                    chat_id=target, media=media_list
                )
            except RPCError:
                media_list[0].parse_mode = None
                sent = await self.client.send_media_group(
                    chat_id=target, media=media_list
                )
            if not (sent[0].caption or "").strip():
                logger.error("album still without caption after retry")

        return sent

    # ------------------------------------------------------------------ rewrite

    async def rewrite_captions_in_channel(
        self,
        channel: str | int,
        *,
        caption: Optional[str] = None,
        max_posts: Optional[int] = None,
    ) -> dict:
        chat = await _resolve_chat(self.client, channel)
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
