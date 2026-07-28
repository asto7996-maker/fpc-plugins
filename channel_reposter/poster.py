"""
poster.py — логика чтения постов из канала-источника и публикации в назначение.

Использует Pyrogram (Userbot / Client API), чтобы:
  • читать историю канала (в т.ч. приватного);
  • копировать медиа без метки «Forwarded from»;
  • подставлять единый HTML-шаблон описания.
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

# Реэкспорт для удобства импорта: from poster import parse_post_link
__all__ = ["ChannelPoster", "parse_post_link"]


def _chat_ref(value: str | int) -> str | int:
    """Нормализовать id канала: '-100123' → int, '@name' → str."""
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
        return raw if raw.startswith("@") else f"@{raw}"


def _is_media_message(msg: Message) -> bool:
    """Есть ли у сообщения медиа, которое мы умеем копировать."""
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
    """
    Собрать InputMedia* из уже загруженного сообщения (по file_id).
    caption ставится только на первый элемент альбома.
    """
    parse_mode = enums.ParseMode.HTML
    if msg.photo:
        return InputMediaPhoto(
            media=msg.photo.file_id,
            caption=caption,
            parse_mode=parse_mode if caption is not None else None,
        )
    if msg.video:
        return InputMediaVideo(
            media=msg.video.file_id,
            caption=caption,
            parse_mode=parse_mode if caption is not None else None,
        )
    if msg.animation:
        # animation (GIF) отправляем как документ/видео — через Document
        return InputMediaDocument(
            media=msg.animation.file_id,
            caption=caption,
            parse_mode=parse_mode if caption is not None else None,
        )
    if msg.audio:
        return InputMediaAudio(
            media=msg.audio.file_id,
            caption=caption,
            parse_mode=parse_mode if caption is not None else None,
        )
    if msg.document:
        return InputMediaDocument(
            media=msg.document.file_id,
            caption=caption,
            parse_mode=parse_mode if caption is not None else None,
        )
    return None


class ChannelPoster:
    """
    Движок перезалива: читает посты юзерботом и публикует в целевой канал.
    """

    def __init__(self, client: Client, db: Database) -> None:
        self.client = client
        self.db = db
        # ID медиагрупп, уже обработанных в текущем цикле (чтобы не дублировать)
        self._seen_grouped: set[str] = set()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    async def apply_start_link(self, link: str) -> tuple[str | int, int]:
        """
        Установить стартовую точку: progress_id = message_id из ссылки.
        Сам указанный пост НЕ публикуется — начнём со следующего (id+1).
        """
        chat_ref, message_id = parse_post_link(link)

        # Обновляем источник из ссылки (username или числовой ID приватного канала)
        self.db.set_source_channel(str(chat_ref))
        self.db.set_start_link(link)
        self.db.set_progress_id(message_id)
        logger.info(
            "Стартовая ссылка применена: chat=%s, message_id=%s "
            "(публикация начнётся с ID %s)",
            chat_ref,
            message_id,
            message_id + 1,
        )
        return chat_ref, message_id

    async def run_cycle(self) -> int:
        """
        Один цикл публикации: до posts_per_cycle постов в хронологическом порядке.

        Returns:
            Количество успешно опубликованных постов (медиагрупп считается как 1).
        """
        settings = self.db.get_settings()
        if not settings.is_running:
            logger.debug("Автопостинг на паузе — цикл пропущен")
            return 0

        source = _chat_ref(settings.source_channel or config.SOURCE_CHANNEL)
        target = _chat_ref(settings.target_channel or config.TARGET_CHANNEL)
        progress_id = settings.progress_id
        limit = settings.posts_per_cycle
        caption = settings.caption_template

        if progress_id <= 0:
            logger.warning(
                "progress_id не задан. Укажите стартовую ссылку через админ-панель."
            )
            return 0

        logger.info(
            "Старт цикла USERBOT: source=%s target=%s after_id=%s limit=%s",
            source,
            target,
            progress_id,
            limit,
        )

        published = 0
        # Двигаемся от старых к новым: message_id = progress_id+1, +2, ...
        next_id = progress_id + 1
        # Защита от бесконечного цикла, если в канале большие «дыры» в ID
        empty_streak = 0
        max_empty_streak = 50
        self._seen_grouped.clear()

        while published < limit and empty_streak < max_empty_streak:
            settings = self.db.get_settings()
            if not settings.is_running:
                logger.info("Автопостинг поставлен на паузу во время цикла")
                break

            try:
                result = await self._process_message_id(
                    source=source,
                    target=target,
                    message_id=next_id,
                    caption=caption,
                )
            except FloodWait as e:
                wait = int(e.value) + 1
                logger.warning("FloodWait %s сек — ждём", wait)
                await asyncio.sleep(wait)
                continue
            except (ChannelPrivate, ChannelInvalid) as e:
                logger.error("Канал-источник недоступен: %s", e)
                break
            except RPCError as e:
                logger.error("RPCError на ID %s: %s", next_id, e)
                self.db.add_history(
                    source_message_id=next_id,
                    status="error",
                    error=str(e),
                )
                self.db.set_progress_id(next_id)
                next_id += 1
                empty_streak += 1
                continue

            if result == "empty":
                # Сообщения с таким ID нет — возможно удалено или дыра в нумерации
                empty_streak += 1
                self.db.set_progress_id(next_id)
                next_id += 1
                continue

            if result == "skip":
                # Служебное / уже обработанный элемент альбома / без медиа
                empty_streak = 0
                self.db.set_progress_id(next_id)
                next_id += 1
                continue

            # result == "ok"
            empty_streak = 0
            published += 1
            self.db.set_progress_id(next_id)
            next_id += 1

            if published < limit:
                delay = random.uniform(
                    config.POST_DELAY_MIN, config.POST_DELAY_MAX
                )
                logger.debug("Пауза %.1f сек перед следующим постом", delay)
                await asyncio.sleep(delay)

        logger.info("Цикл завершён: опубликовано %s пост(ов)", published)
        return published

    # ------------------------------------------------------------------
    # Внутренняя обработка одного message_id
    # ------------------------------------------------------------------

    async def _process_message_id(
        self,
        source: str | int,
        target: str | int,
        message_id: int,
        caption: str,
    ) -> str:
        """
        Обработать одно сообщение.

        Returns:
            "ok"    — опубликовано
            "skip"  — пропущено намеренно
            "empty" — сообщения нет
        """
        if self.db.was_processed(message_id):
            logger.debug("ID %s уже в истории — skip", message_id)
            return "skip"

        try:
            msg = await self.client.get_messages(source, message_id)
        except MessageIdInvalid:
            return "empty"
        except FloodWait:
            raise

        if msg is None or getattr(msg, "empty", False):
            return "empty"

        # Элемент уже обработанной медиагруппы
        if msg.media_group_id:
            gid = str(msg.media_group_id)
            if gid in self._seen_grouped:
                return "skip"
            return await self._publish_album(
                source, target, msg, caption
            )

        if not _is_media_message(msg) and not (msg.text or msg.caption):
            # Служебные / сервисные сообщения без контента
            logger.debug("ID %s без контента — skip", message_id)
            return "skip"

        return await self._publish_single(target, msg, caption)

    async def _publish_single(
        self, target: str | int, msg: Message, caption: str
    ) -> str:
        """Опубликовать одиночный пост (фото/видео/документ/текст)."""
        try:
            if _is_media_message(msg):
                # copy_message — без метки Forwarded from, подпись = HTML-шаблон
                try:
                    sent = await self.client.copy_message(
                        chat_id=target,
                        from_chat_id=msg.chat.id,
                        message_id=msg.id,
                        caption=caption or None,
                        parse_mode=enums.ParseMode.HTML if caption else None,
                    )
                except RPCError as cap_err:
                    # Битый HTML — отправим как обычный текст
                    if caption and "parse" in str(cap_err).lower():
                        logger.warning("HTML parse fail, retry plain: %s", cap_err)
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
                        disable_web_page_preview=False,
                    )
                except RPCError as cap_err:
                    if "parse" in str(cap_err).lower():
                        sent = await self.client.send_message(
                            chat_id=target,
                            text=text,
                            disable_web_page_preview=False,
                        )
                    else:
                        raise

            target_id = sent.id if isinstance(sent, Message) else None
            self.db.add_history(
                source_message_id=msg.id,
                target_message_id=target_id,
                status="ok",
            )
            logger.info(
                "Опубликован пост source=%s → target=%s",
                msg.id,
                target_id,
            )
            return "ok"

        except FloodWait:
            raise
        except RPCError as e:
            logger.error("Не удалось опубликовать ID %s: %s", msg.id, e)
            self.db.add_history(
                source_message_id=msg.id,
                status="error",
                error=str(e),
            )
            return "skip"

    async def _publish_album(
        self,
        source: str | int,
        target: str | int,
        anchor: Message,
        caption: str,
    ) -> str:
        """Скачать медиагруппу и отправить как альбом с единой подписью."""
        gid = str(anchor.media_group_id)
        self._seen_grouped.add(gid)

        try:
            album: list[Message] = await self.client.get_media_group(
                source, anchor.id
            )
        except (RPCError, ValueError) as e:
            logger.error("Не удалось получить альбом для ID %s: %s", anchor.id, e)
            self.db.add_history(
                source_message_id=anchor.id,
                grouped_id=gid,
                status="error",
                error=str(e),
            )
            return "skip"

        # Сортируем по ID — хронологический порядок внутри альбома
        album = sorted(album, key=lambda m: m.id)

        media_list = []
        for i, m in enumerate(album):
            cap = caption if i == 0 and caption else ("" if i == 0 else None)
            # Для первого элемента пустая строка допустима; None — без caption
            item = _build_input_media(m, caption=cap if i == 0 else None)
            if item is not None:
                media_list.append(item)

        if not media_list:
            logger.warning("Альбом %s без поддерживаемых медиа — skip", gid)
            for m in album:
                self.db.add_history(
                    source_message_id=m.id,
                    grouped_id=gid,
                    status="skip",
                    error="no supported media",
                )
            return "skip"

        try:
            sent_list = await self.client.send_media_group(
                chat_id=target,
                media=media_list,
            )
            first_target_id = sent_list[0].id if sent_list else None
            for m in album:
                self.db.add_history(
                    source_message_id=m.id,
                    target_message_id=first_target_id,
                    grouped_id=gid,
                    status="ok",
                )
            # Пометим все ID альбома как «увиденные», чтобы цикл их пропустил
            for m in album:
                self._seen_grouped.add(gid)
            logger.info(
                "Опубликован альбом grouped_id=%s (%s файлов), anchor=%s → %s",
                gid,
                len(media_list),
                anchor.id,
                first_target_id,
            )
            return "ok"

        except FloodWait:
            # Сбросим флаг, чтобы можно было повторить
            self._seen_grouped.discard(gid)
            raise
        except RPCError as e:
            logger.error("Ошибка отправки альбома %s: %s", gid, e)
            self.db.add_history(
                source_message_id=anchor.id,
                grouped_id=gid,
                status="error",
                error=str(e),
            )
            return "skip"

    # ------------------------------------------------------------------
    # Массовая замена описаний в канале
    # ------------------------------------------------------------------

    async def rewrite_captions_in_channel(
        self,
        channel: str | int,
        *,
        caption: Optional[str] = None,
        max_posts: Optional[int] = None,
    ) -> dict:
        """
        Пройти посты в канале и заменить текст/подпись на текущий шаблон.

        Для альбомов правится только одно сообщение (с подписью / первое).
        Аккаунт должен быть админом канала с правом редактирования сообщений.

        Returns:
            {"updated": int, "skipped": int, "errors": int, "scanned": int}
        """
        chat = _chat_ref(channel)
        text = (
            caption
            if caption is not None
            else (self.db.get_settings().caption_template or "")
        )
        if not text.strip():
            raise ValueError("Шаблон описания пуст — сначала задайте текст через «✏️ Текст описания»")

        updated = 0
        skipped = 0
        errors = 0
        scanned = 0
        seen_groups: set[str] = set()

        logger.info(
            "Rewrite captions: channel=%s max_posts=%s",
            chat,
            max_posts if max_posts is not None else "all",
        )

        async for msg in self.client.get_chat_history(chat):
            if max_posts is not None and updated >= max_posts:
                break

            scanned += 1

            # Служебные сообщения канала
            if getattr(msg, "service", None):
                skipped += 1
                continue

            edit_msg = msg
            if msg.media_group_id:
                gid = str(msg.media_group_id)
                if gid in seen_groups:
                    skipped += 1
                    continue
                seen_groups.add(gid)
                try:
                    album = await self.client.get_media_group(chat, msg.id)
                    album = sorted(album, key=lambda m: m.id)
                    edit_msg = next((m for m in album if m.caption), album[0])
                except (RPCError, ValueError) as e:
                    logger.warning("Альбом %s недоступен: %s", gid, e)
                    errors += 1
                    continue

            try:
                await self._edit_message_description(chat, edit_msg, text)
                updated += 1
                delay = random.uniform(
                    max(0.4, config.POST_DELAY_MIN / 4),
                    max(1.0, config.POST_DELAY_MAX / 3),
                )
                await asyncio.sleep(delay)
            except FloodWait as e:
                wait = int(e.value) + 1
                logger.warning("FloodWait при edit %s сек", wait)
                await asyncio.sleep(wait)
                try:
                    await self._edit_message_description(chat, edit_msg, text)
                    updated += 1
                except Exception as e2:
                    if _is_not_modified(e2):
                        skipped += 1
                    elif isinstance(e2, ValueError):
                        skipped += 1
                    else:
                        errors += 1
                        logger.warning("Edit fail id=%s: %s", edit_msg.id, e2)
            except ValueError:
                skipped += 1
            except RPCError as e:
                if _is_not_modified(e):
                    skipped += 1
                else:
                    errors += 1
                    logger.warning("Edit fail id=%s: %s", edit_msg.id, e)

        result = {
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "scanned": scanned,
        }
        logger.info("Rewrite done: %s", result)
        return result

    async def _edit_message_description(
        self, chat: str | int, msg: Message, text: str
    ) -> None:
        """Заменить caption у медиа или text у текстового поста (HTML)."""
        if _is_media_message(msg):
            try:
                await self.client.edit_message_caption(
                    chat_id=chat,
                    message_id=msg.id,
                    caption=text,
                    parse_mode=enums.ParseMode.HTML,
                )
            except RPCError as e:
                if "parse" in str(e).lower():
                    await self.client.edit_message_caption(
                        chat_id=chat,
                        message_id=msg.id,
                        caption=text,
                    )
                else:
                    raise
            return

        if msg.text is not None:
            try:
                await self.client.edit_message_text(
                    chat_id=chat,
                    message_id=msg.id,
                    text=text,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=False,
                )
            except RPCError as e:
                if "parse" in str(e).lower():
                    await self.client.edit_message_text(
                        chat_id=chat,
                        message_id=msg.id,
                        text=text,
                        disable_web_page_preview=False,
                    )
                else:
                    raise
            return

        raise ValueError("nothing to edit")



def _is_not_modified(err: BaseException) -> bool:
    text = str(err).upper()
    return "MESSAGE_NOT_MODIFIED" in text or "not modified" in text.lower()
