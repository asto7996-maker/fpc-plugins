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
import re
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

# Bot API лимит загрузки ~50 МБ
BOT_UPLOAD_LIMIT = 49 * 1024 * 1024
DOWNLOAD_RETRIES = 3
UPLOAD_RETRIES = 3


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


def _media_ext(msg: Message) -> str:
    name = None
    if msg.video and msg.video.file_name:
        name = msg.video.file_name
    elif msg.document and msg.document.file_name:
        name = msg.document.file_name
    elif msg.audio and msg.audio.file_name:
        name = msg.audio.file_name
    elif msg.animation and msg.animation.file_name:
        name = msg.animation.file_name
    if name:
        suf = Path(name).suffix
        if suf:
            return re.sub(r"[^a-zA-Z0-9.]", "", suf)[:12] or ".bin"
    if msg.photo:
        return ".jpg"
    if msg.video or msg.animation:
        return ".mp4"
    if msg.audio or msg.voice:
        return ".ogg"
    return ".bin"


def _approx_size(msg: Message) -> Optional[int]:
    for attr in ("video", "document", "audio", "animation", "voice", "video_note"):
        obj = getattr(msg, attr, None)
        if obj is not None and getattr(obj, "file_size", None):
            return int(obj.file_size)
    if msg.photo:
        # берём самое большое превью
        try:
            return int(msg.photo[-1].file_size or 0)
        except Exception:
            return None
    return None


def _is_transient(err: BaseException) -> bool:
    text = str(err).upper()
    # Постоянные ошибки загрузки — не ретраим media_group, идём в fallback
    permanent = (
        "ENTITY TOO LARGE",
        "TOO BIG",
        "FILE_TOO_LARGE",
        "REQUEST_ENTITY_TOO_LARGE",
        "FORBIDDEN",
        "WRITE_FORBIDDEN",
        "CHAT_WRITE",
        "HAVE NO RIGHTS",
    )
    if any(k in text for k in permanent):
        return False
    keys = (
        "TIMEOUT",
        "TIMED OUT",
        "CONNECTION",
        "NETWORK",
        "SERVER DISCONNECTED",
        "TEMPORARY",
        "RESET BY PEER",
        "TOO MANY REQUESTS",
    )
    return any(k in text for k in keys) or isinstance(
        err, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)
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
        retry_streak = 0
        self._seen_grouped.clear()

        logger.info(
            "Hybrid cycle %s → %s after=%s limit=%s",
            source,
            target,
            settings.progress_id,
            limit,
        )

        while published < limit and empty_streak < 50 and retry_streak < 5:
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
                if "WRITE_FORBIDDEN" in str(e).upper() or "CHAT_WRITE" in str(e).upper():
                    logger.error("Write forbidden: %s", e)
                    break
                logger.error("RPC %s: %s", next_id, e)
                self.db.add_history(next_id, status="error", error=str(e))
                self.db.set_progress_id(next_id)
                next_id += 1
                empty_streak += 1
                continue

            if result == "retry":
                retry_streak += 1
                wait = min(30, 3 * retry_streak)
                logger.warning("retry id=%s in %ss (streak=%s)", next_id, wait, retry_streak)
                await asyncio.sleep(wait)
                continue
            if result == "empty":
                empty_streak += 1
                self.db.set_progress_id(next_id)
                next_id += 1
                continue
            if result == "skip":
                empty_streak = 0
                retry_streak = 0
                self.db.set_progress_id(next_id)
                next_id += 1
                continue
            if result == "fatal":
                break

            empty_streak = 0
            retry_streak = 0
            published += 1
            cur = max(self.db.get_progress_id(), next_id)
            self.db.set_progress_id(cur)
            next_id = cur + 1
            if published < limit:
                await asyncio.sleep(
                    random.uniform(config.POST_DELAY_MIN, config.POST_DELAY_MAX)
                )

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

    async def _download(self, msg: Message, attempt: int = 1) -> Optional[Path]:
        """Скачать медиа в безопасное уникальное имя файла."""
        if not _is_media(msg):
            return None
        dest = self._tmp / f"{msg.id}_{attempt}{_media_ext(msg)}"
        try:
            path = await self.client.download_media(msg, file_name=str(dest))
            if not path:
                return None
            p = Path(path)
            if not p.exists() or p.stat().st_size <= 0:
                logger.warning("empty download id=%s", msg.id)
                return None
            if p.stat().st_size > BOT_UPLOAD_LIMIT:
                logger.error(
                    "file too large for Bot API id=%s size=%s",
                    msg.id,
                    p.stat().st_size,
                )
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
                return None
            return p
        except FloodWait:
            raise
        except ValueError:
            logger.warning("no downloadable media id=%s", msg.id)
            return None
        except Exception as e:
            logger.warning("download fail id=%s attempt=%s: %s", msg.id, attempt, e)
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            for leftover in self._tmp.glob(f"{msg.id}_*"):
                try:
                    leftover.unlink(missing_ok=True)
                except Exception:
                    pass
            if attempt < DOWNLOAD_RETRIES and _is_transient(e):
                await asyncio.sleep(2 * attempt)
                return await self._download(msg, attempt + 1)
            logger.exception("download give up id=%s", msg.id)
            return None

    async def _send_with_retry(self, coro_factory, *, label: str):
        last_err: Optional[BaseException] = None
        for attempt in range(1, UPLOAD_RETRIES + 1):
            try:
                return await coro_factory()
            except TelegramRetryAfter as e:
                await asyncio.sleep(int(e.retry_after) + 1)
                last_err = e
            except Exception as e:
                last_err = e
                if not _is_transient(e) or attempt >= UPLOAD_RETRIES:
                    raise
                logger.warning("%s upload retry %s: %s", label, attempt, e)
                await asyncio.sleep(2 * attempt)
        assert last_err is not None
        raise last_err

    async def _send_media_file(
        self,
        *,
        target,
        msg: Message,
        path: Path,
        caption: Optional[str],
        label: str,
    ):
        """Отправить один файл с шаблоном; при битом HTML — без parse_mode."""
        file = FSInputFile(path)

        async def _do(parse: Optional[ParseMode]):
            kwargs = {"chat_id": target, "caption": caption or None}
            if caption and parse is not None:
                kwargs["parse_mode"] = parse
            if msg.photo:
                return await self.bot.send_photo(photo=file, **kwargs)
            if msg.video or msg.animation:
                return await self.bot.send_video(video=file, **kwargs)
            if msg.audio:
                return await self.bot.send_audio(audio=file, **kwargs)
            if msg.voice:
                return await self.bot.send_voice(voice=file, **kwargs)
            return await self.bot.send_document(document=file, **kwargs)

        try:
            return await self._send_with_retry(
                lambda: _do(ParseMode.HTML if caption else None),
                label=label,
            )
        except Exception as e:
            if caption and "parse" in str(e).lower():
                logger.warning("%s HTML caption failed, plain retry: %s", label, e)
                return await self._send_with_retry(
                    lambda: _do(None),
                    label=f"{label}:plain",
                )
            raise

    async def _publish_single(self, target, msg: Message, caption: str) -> str:
        path: Optional[Path] = None
        try:
            if _is_media(msg):
                size = _approx_size(msg)
                if size and size > BOT_UPLOAD_LIMIT:
                    logger.error("skip oversized id=%s size=%s", msg.id, size)
                    self.db.add_history(msg.id, status="error", error=f"too large: {size}")
                    return "skip"
                path = await self._download(msg)
                if path is None:
                    return "retry"
                if caption:
                    logger.info("caption on single %s (%s chars)", msg.id, len(caption))
                sent = await self._send_media_file(
                    target=target,
                    msg=msg,
                    path=path,
                    caption=caption or None,
                    label=f"single:{msg.id}",
                )
            else:
                text = caption if caption else (msg.text or msg.caption or "")
                if not text:
                    return "skip"
                try:
                    sent = await self._send_with_retry(
                        lambda: self.bot.send_message(
                            chat_id=target,
                            text=text,
                            parse_mode=ParseMode.HTML,
                        ),
                        label=f"text:{msg.id}",
                    )
                except Exception as e:
                    if "parse" in str(e).lower():
                        sent = await self._send_with_retry(
                            lambda: self.bot.send_message(chat_id=target, text=text),
                            label=f"text:{msg.id}:plain",
                        )
                    else:
                        raise

            self.db.add_history(
                source_message_id=msg.id,
                target_message_id=sent.message_id,
                status="ok",
            )
            logger.info("Hybrid published %s → %s", msg.id, sent.message_id)
            return "ok"
        except Exception as e:
            err = str(e).upper()
            if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err or "FORBIDDEN" in err:
                logger.error("Fatal write: %s", e)
                return "fatal"
            if _is_transient(e):
                logger.error("publish single transient %s: %s", msg.id, e)
                return "retry"
            if "TOO BIG" in err or "REQUEST ENTITY TOO LARGE" in err or "FILE_PART" in err:
                logger.error("skip oversized/unsupported %s: %s", msg.id, e)
                self.db.add_history(msg.id, status="error", error=str(e))
                return "skip"
            logger.error("publish single %s: %s", msg.id, e)
            self.db.add_history(msg.id, status="error", error=str(e))
            return "skip"
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _staging_chat(self) -> Optional[int | str]:
        """Чат для временной загрузки (получить file_id без публикации в канал)."""
        raw = self.db.get("staging_chat_id")
        if raw:
            try:
                return int(raw)
            except ValueError:
                return raw
        if config.ADMIN_IDS:
            return config.ADMIN_IDS[0]
        return None

    async def _stage_file_id(self, msg: Message, path: Path) -> Optional[tuple[str, str]]:
        """
        Загрузить файл в staging-чат → file_id → удалить.
        Возвращает (file_id, kind) где kind: photo|video|document|audio.
        """
        stage = self._staging_chat()
        if stage is None:
            logger.error("Нет staging_chat_id — напишите боту /start")
            return None
        file = FSInputFile(path)
        sent = None
        try:
            if msg.photo:
                sent = await self.bot.send_photo(
                    chat_id=stage, photo=file, disable_notification=True
                )
                kind, fid = "photo", sent.photo[-1].file_id
            elif msg.video or msg.animation:
                sent = await self.bot.send_video(
                    chat_id=stage, video=file, disable_notification=True
                )
                kind, fid = "video", sent.video.file_id if sent.video else sent.document.file_id
            elif msg.audio:
                sent = await self.bot.send_audio(
                    chat_id=stage, audio=file, disable_notification=True
                )
                kind, fid = "audio", sent.audio.file_id
            else:
                sent = await self.bot.send_document(
                    chat_id=stage, document=file, disable_notification=True
                )
                kind, fid = "document", sent.document.file_id
            return fid, kind
        except TelegramRetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            return await self._stage_file_id(msg, path)
        except Exception as e:
            logger.error("stage upload id=%s: %s", msg.id, e)
            return None
        finally:
            if sent is not None:
                try:
                    await self.bot.delete_message(stage, sent.message_id)
                except Exception:
                    pass

    def _input_media(self, kind: str, file_id: str, caption: Optional[str]):
        parse = ParseMode.HTML if caption else None
        if kind == "photo":
            return InputMediaPhoto(media=file_id, caption=caption, parse_mode=parse)
        if kind == "video":
            return InputMediaVideo(media=file_id, caption=caption, parse_mode=parse)
        return InputMediaDocument(media=file_id, caption=caption, parse_mode=parse)

    async def _publish_album(self, source, target, anchor: Message, caption: str) -> str:
        """Публикует альбом ОДНИМ media_group (не дробит на отдельные посты)."""
        gid = str(anchor.media_group_id)
        self._seen_grouped.add(gid)
        try:
            album = await self.client.get_media_group(source, anchor.id)
        except Exception as e:
            if _is_transient(e):
                self._seen_grouped.discard(gid)
                return "retry"
            self.db.add_history(anchor.id, grouped_id=gid, status="error", error=str(e))
            return "skip"

        album = sorted(album, key=lambda m: m.id)
        paths: list[Path] = []
        try:
            downloaded: list[tuple[Message, Path]] = []
            for m in album:
                size = _approx_size(m)
                if size and size > BOT_UPLOAD_LIMIT:
                    logger.warning("album skip oversized id=%s size=%s", m.id, size)
                    continue
                path = await self._download(m)
                if path is None:
                    continue
                paths.append(path)
                downloaded.append((m, path))

            if not downloaded:
                self._seen_grouped.discard(gid)
                return "retry"

            # 1 файл → обычная публикация
            if len(downloaded) == 1:
                m, path = downloaded[0]
                sent = await self._send_media_file(
                    target=target,
                    msg=m,
                    path=path,
                    caption=caption or None,
                    label=f"album-single:{m.id}",
                )
                max_src = max(x.id for x in album)
                for x in album:
                    self.db.add_history(
                        source_message_id=x.id,
                        target_message_id=sent.message_id,
                        grouped_id=gid,
                        status="ok",
                    )
                self.db.set_progress_id(max_src)
                logger.info("Hybrid album(1) %s → %s", gid, sent.message_id)
                return "ok"

            # Сначала пробуем прямой media_group с файлами (если суммарно небольшой)
            total_size = sum(p.stat().st_size for _, p in downloaded)
            media_direct = []
            for i, (m, path) in enumerate(downloaded[:10]):
                file = FSInputFile(path)
                cap = caption if i == 0 else None
                parse = ParseMode.HTML if (i == 0 and caption) else None
                if m.photo:
                    media_direct.append(InputMediaPhoto(media=file, caption=cap, parse_mode=parse))
                elif m.video or m.animation:
                    media_direct.append(InputMediaVideo(media=file, caption=cap, parse_mode=parse))
                else:
                    media_direct.append(InputMediaDocument(media=file, caption=cap, parse_mode=parse))

            # Прямой multipart только для маленьких фото-альбомов.
            # Видео/тяжёлые альбомы — через staging file_id, иначе таймаут/Entity Too Large.
            first_id = None
            only_photos = all(m.photo for m, _ in downloaded)
            use_direct = (
                len(downloaded) <= 10
                and total_size <= 8 * 1024 * 1024
                and only_photos
            )
            if use_direct:
                try:
                    logger.info(
                        "album %s direct media_group files=%s size=%s",
                        gid,
                        len(media_direct),
                        total_size,
                    )
                    sent_list = await self._send_with_retry(
                        lambda: self.bot.send_media_group(chat_id=target, media=media_direct),
                        label=f"album-direct:{gid}",
                    )
                    first_id = sent_list[0].message_id
                    max_src = max(x.id for x in album)
                    for x in album:
                        self.db.add_history(
                            source_message_id=x.id,
                            target_message_id=first_id,
                            grouped_id=gid,
                            status="ok",
                        )
                    self.db.set_progress_id(max_src)
                    logger.info(
                        "Hybrid album direct %s (%s files) → %s",
                        gid,
                        len(media_direct),
                        first_id,
                    )
                    return "ok"
                except Exception as e:
                    err = str(e).upper()
                    if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                        return "fatal"
                    logger.warning(
                        "direct media_group failed (%s) — stage file_ids", e
                    )

            # Альбом целиком через file_id → один media_group (не дробим на посты)
            if self._staging_chat() is None:
                logger.error("Нужен /start у админа для staging file_id")
                self._seen_grouped.discard(gid)
                return "retry"

            logger.info(
                "album %s staging file_ids files=%s size=%s",
                gid,
                len(downloaded),
                total_size,
            )
            staged: list[tuple[Message, str, str]] = []
            for m, path in downloaded:
                got = await self._stage_file_id(m, path)
                if got is None:
                    logger.warning("stage failed id=%s", m.id)
                    continue
                staged.append((m, got[0], got[1]))
                logger.info("staged id=%s kind=%s", m.id, got[1])
                await asyncio.sleep(0.6)

            if not staged:
                self._seen_grouped.discard(gid)
                return "retry"

            # Telegram: max 10 в одной группе. Если >10 — несколько АЛЬБОМОВ (не одиночек).
            first_id = None
            for chunk_i in range(0, len(staged), 10):
                chunk = staged[chunk_i : chunk_i + 10]
                media = []
                for j, (_m, fid, kind) in enumerate(chunk):
                    cap = caption if (chunk_i == 0 and j == 0) else None
                    media.append(self._input_media(kind, fid, cap))
                try:
                    sent_list = await self._send_with_retry(
                        lambda m=media: self.bot.send_media_group(chat_id=target, media=m),
                        label=f"album-staged:{gid}:{chunk_i}",
                    )
                    if first_id is None and sent_list:
                        first_id = sent_list[0].message_id
                    logger.info(
                        "Hybrid album staged chunk %s+%s → %s",
                        chunk_i,
                        len(chunk),
                        sent_list[0].message_id if sent_list else None,
                    )
                    await asyncio.sleep(1.5)
                except Exception as e:
                    err = str(e).upper()
                    if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                        return "fatal"
                    if _is_transient(e):
                        self._seen_grouped.discard(gid)
                        return "retry"
                    logger.error("staged media_group fail: %s", e)
                    self._seen_grouped.discard(gid)
                    self.db.add_history(anchor.id, grouped_id=gid, status="error", error=str(e))
                    return "skip"

            max_src = max(x.id for x in album)
            for x in album:
                self.db.add_history(
                    source_message_id=x.id,
                    target_message_id=first_id,
                    grouped_id=gid,
                    status="ok",
                )
            self.db.set_progress_id(max_src)
            logger.info(
                "Hybrid album %s (%s/%s files, kept as media_group) → %s",
                gid,
                len(staged),
                len(album),
                first_id,
            )
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
            if _is_transient(e):
                self._seen_grouped.discard(gid)
                logger.error("album transient %s: %s", gid, e)
                return "retry"
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
