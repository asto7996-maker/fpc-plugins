"""
poster_botapi.py — перезалив через Bot API (aiogram), без юзербота.

Требования:
  • Бот — администратор канала-источника (может читать/копировать посты);
  • Бот — администратор канала-назначения (может публиковать).

Ограничение: медиагруппы копируются поэлементно (Bot API не отдаёт
media_group_id без объекта Message). Для идеальных альбомов нужен
юзербот (poster.py + Pyrogram).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional, Union

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import config
from database import Database
from links import parse_post_link

logger = logging.getLogger(__name__)

ChatRef = Union[str, int]


def _normalize_chat(ref: str) -> ChatRef:
    """Преобразовать строку настройки в chat_id / @username для Bot API."""
    raw = (ref or "").strip()
    if not raw:
        raise ValueError("Канал не задан")
    if raw.startswith("@"):
        return raw
    # Числовой ID (в т.ч. -100...)
    try:
        return int(raw)
    except ValueError:
        return raw if raw.startswith("@") else f"@{raw}"


class ChannelPosterBotAPI:
    """Движок перезалива на чистом Bot API."""

    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def apply_start_link(self, link: str) -> tuple[ChatRef, int]:
        chat_ref, message_id = parse_post_link(link)
        self.db.set_source_channel(str(chat_ref))
        self.db.set_start_link(link)
        self.db.set_progress_id(message_id)
        logger.info(
            "Стартовая ссылка: chat=%s id=%s → старт с %s",
            chat_ref,
            message_id,
            message_id + 1,
        )
        return chat_ref, message_id

    async def run_cycle(self) -> int:
        settings = self.db.get_settings()
        if not settings.is_running:
            logger.debug("На паузе — цикл пропущен")
            return 0

        try:
            source = _normalize_chat(settings.source_channel or config.SOURCE_CHANNEL)
            target = _normalize_chat(settings.target_channel or config.TARGET_CHANNEL)
        except ValueError as e:
            logger.warning("%s. Укажите каналы в .env или через админку.", e)
            return 0

        progress_id = settings.progress_id
        if progress_id <= 0:
            logger.warning("progress_id не задан — укажите стартовую ссылку")
            return 0

        limit = settings.posts_per_cycle
        caption = settings.caption_template
        logger.info(
            "Цикл BotAPI: source=%s target=%s after=%s limit=%s",
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
                wait = int(e.retry_after) + 1
                logger.warning("Flood/RetryAfter %s сек", wait)
                await asyncio.sleep(wait)
                continue
            except TelegramForbiddenError as e:
                logger.error("Нет доступа к каналу: %s", e)
                break

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

            empty_streak = 0
            published += 1
            self.db.set_progress_id(next_id)
            next_id += 1

            if published < limit:
                delay = random.uniform(config.POST_DELAY_MIN, config.POST_DELAY_MAX)
                await asyncio.sleep(delay)

        logger.info("Цикл завершён: %s пост(ов)", published)
        return published

    async def _copy_one(
        self,
        source: ChatRef,
        target: ChatRef,
        message_id: int,
        caption: str,
    ) -> str:
        """
        copyMessage без метки Forwarded from.
        Подпись заменяется на шаблон (HTML).
        """
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
            target_id = getattr(sent, "message_id", None)
            self.db.add_history(
                source_message_id=message_id,
                target_message_id=target_id,
                status="ok",
            )
            logger.info("Скопирован %s → %s", message_id, target_id)
            return "ok"

        except TelegramRetryAfter:
            raise
        except TelegramForbiddenError:
            raise
        except TelegramBadRequest as e:
            text = (e.message or str(e)).lower()
            # Сообщение не найдено / нечего копировать / служебное
            if "chat not found" in text:
                logger.error("Чат не найден (source=%s / target=%s): %s", source, target, e)
                raise

            if any(
                needle in text
                for needle in (
                    "message to copy not found",
                    "message not found",
                    "message can't be copied",
                    "message can not be copied",
                )
            ):
                logger.debug("ID %s пуст/недоступен: %s", message_id, e)
                return "empty"

            logger.warning("Пропуск ID %s: %s", message_id, e)
            self.db.add_history(
                source_message_id=message_id,
                status="error",
                error=str(e),
            )
            return "skip"
        except Exception as e:
            logger.exception("Ошибка копирования ID %s", message_id)
            self.db.add_history(
                source_message_id=message_id,
                status="error",
                error=str(e),
            )
            return "skip"
