"""
poster_botapi.py — перезалив через Bot API.

Нужно:
  • Бот — админ только в ВАШЕМ канале-назначении (публикация);
  • Источник — обычно публичный канал (@username); админом там быть не нужно;
  • Стартовая ссылка на пост в источнике.

Копирование: copyMessage (без «Forwarded from»), подпись = ваш шаблон.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional, Union

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

import config
from database import Database
from links import parse_post_link

logger = logging.getLogger(__name__)

ChatRef = Union[str, int]


def normalize_chat(ref: str) -> ChatRef:
    raw = (ref or "").strip()
    if not raw:
        raise ValueError("Канал не задан")
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        return f"@{raw}"


class ChannelPosterBotAPI:
    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._rewrite_cancel = False

    async def apply_start_link(self, link: str) -> tuple[ChatRef, int]:
        chat_ref, message_id = parse_post_link(link)
        self.db.set_source_channel(str(chat_ref))
        self.db.set_start_link(link)
        self.db.set_progress_id(message_id)
        logger.info(
            "Старт: chat=%s id=%s → публикация с %s",
            chat_ref,
            message_id,
            message_id + 1,
        )
        return chat_ref, message_id

    async def run_cycle(self) -> int:
        settings = self.db.get_settings()
        if not settings.is_running:
            return 0

        try:
            source = normalize_chat(settings.source_channel or config.SOURCE_CHANNEL)
            target = normalize_chat(settings.target_channel or config.TARGET_CHANNEL)
        except ValueError as e:
            logger.warning("%s", e)
            return 0

        if settings.progress_id <= 0:
            logger.warning("Нет стартовой ссылки")
            return 0

        limit = settings.posts_per_cycle
        caption = settings.caption_template
        progress_id = settings.progress_id
        logger.info(
            "Цикл: %s → %s after=%s limit=%s",
            source,
            target,
            progress_id,
            limit,
        )

        published = 0
        next_id = progress_id + 1
        empty_streak = 0
        max_empty = 50

        while published < limit and empty_streak < max_empty:
            settings = self.db.get_settings()
            if not settings.is_running:
                break

            if self.db.was_processed(next_id):
                self.db.set_progress_id(next_id)
                next_id += 1
                empty_streak = 0
                continue

            try:
                result = await self._copy_one(source, target, next_id, caption)
            except TelegramRetryAfter as e:
                await asyncio.sleep(int(e.retry_after) + 1)
                continue
            except TelegramForbiddenError as e:
                logger.error("Нет доступа: %s", e)
                break

            if result == "empty":
                empty_streak += 1
                self.db.set_progress_id(next_id)
                next_id += 1
                continue

            empty_streak = 0
            self.db.set_progress_id(next_id)
            if result == "ok":
                published += 1
                if published < limit:
                    await asyncio.sleep(
                        random.uniform(config.POST_DELAY_MIN, config.POST_DELAY_MAX)
                    )
            next_id += 1

        logger.info("Цикл готов: %s", published)
        return published

    async def _copy_one(
        self,
        source: ChatRef,
        target: ChatRef,
        message_id: int,
        caption: str,
    ) -> str:
        try:
            kwargs = {
                "chat_id": target,
                "from_chat_id": source,
                "message_id": message_id,
            }
            if caption:
                kwargs["caption"] = caption
                kwargs["parse_mode"] = ParseMode.HTML

            sent = await self.bot.copy_message(**kwargs)
            self.db.add_history(
                source_message_id=message_id,
                target_message_id=getattr(sent, "message_id", None),
                status="ok",
            )
            logger.info("copy %s → %s", message_id, getattr(sent, "message_id", None))
            return "ok"

        except TelegramRetryAfter:
            raise
        except TelegramForbiddenError:
            raise
        except TelegramBadRequest as e:
            text = (e.message or str(e)).lower()
            if "chat not found" in text:
                raise
            if any(
                x in text
                for x in (
                    "message to copy not found",
                    "message not found",
                    "message can't be copied",
                    "message can not be copied",
                )
            ):
                return "empty"
            # caption parse error → retry without HTML
            if caption and "parse" in text:
                try:
                    sent = await self.bot.copy_message(
                        chat_id=target,
                        from_chat_id=source,
                        message_id=message_id,
                        caption=caption,
                    )
                    self.db.add_history(
                        source_message_id=message_id,
                        target_message_id=getattr(sent, "message_id", None),
                        status="ok",
                    )
                    return "ok"
                except Exception as e2:
                    self.db.add_history(
                        source_message_id=message_id,
                        status="error",
                        error=str(e2),
                    )
                    return "skip"
            self.db.add_history(
                source_message_id=message_id,
                status="error",
                error=str(e),
            )
            return "skip"
        except Exception as e:
            logger.exception("copy fail %s", message_id)
            self.db.add_history(
                source_message_id=message_id,
                status="error",
                error=str(e),
            )
            return "skip"

    # ------------------------------------------------------------------
    # Массовая замена подписей у уже скопированных постов
    # ------------------------------------------------------------------

    def cancel_rewrite(self) -> None:
        self._rewrite_cancel = True

    async def rewrite_captions_in_channel(
        self,
        channel: str | int,
        *,
        caption: Optional[str] = None,
        max_posts: Optional[int] = None,
    ) -> dict:
        """
        Обновить подписи постов, которые бот уже публиковал (из history).
        Редактировать чужие посты Bot API не умеет.
        """
        chat = normalize_chat(str(channel))
        text = caption if caption is not None else (self.db.get_settings().caption_template or "")
        if not text.strip():
            raise ValueError("Сначала задайте шаблон описания")

        self._rewrite_cancel = False
        ids = self.db.list_target_message_ids(limit=max_posts)
        updated = skipped = errors = 0

        logger.info("Rewrite BotAPI: channel=%s posts=%s", chat, len(ids))
        for mid in ids:
            if self._rewrite_cancel:
                break
            try:
                await self.bot.edit_message_caption(
                    chat_id=chat,
                    message_id=mid,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )
                updated += 1
                await asyncio.sleep(random.uniform(1.5, 3.0))
            except TelegramRetryAfter as e:
                await asyncio.sleep(int(e.retry_after) + 1)
                try:
                    await self.bot.edit_message_caption(
                        chat_id=chat,
                        message_id=mid,
                        caption=text,
                        parse_mode=ParseMode.HTML,
                    )
                    updated += 1
                except Exception:
                    errors += 1
            except TelegramBadRequest as e:
                msg = (e.message or str(e)).lower()
                if "there is no caption" in msg or "message is not modified" in msg:
                    # текстовый пост или уже тот же текст
                    try:
                        await self.bot.edit_message_text(
                            chat_id=chat,
                            message_id=mid,
                            text=text,
                            parse_mode=ParseMode.HTML,
                        )
                        updated += 1
                    except TelegramBadRequest:
                        skipped += 1
                else:
                    errors += 1
                    logger.warning("edit %s: %s", mid, e)
            except Exception as e:
                errors += 1
                logger.warning("edit %s: %s", mid, e)

        return {
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "scanned": len(ids),
            "cancelled": self._rewrite_cancel,
        }
