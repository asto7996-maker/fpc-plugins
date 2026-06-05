"""
Модуль публикации контента.
Забирает посты из очереди и публикует их в целевой канал
с соблюдением задержек, расписания и лимитов.
"""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from telethon import TelegramClient, errors
from telethon.tl.types import (
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    InputMediaUploadedDocument,
    InputMediaUploadedPhoto,
)

from database import Database, PostRecord, PostStatus
from media_processor import MediaProcessor

logger = logging.getLogger(__name__)


class PublisherState:
    """Состояние публикатора."""
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class Publisher:
    """
    Публикует посты из очереди в целевой канал.
    Управляет задержками, расписанием и обработкой ошибок.
    """

    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        media_processor: MediaProcessor,
        config: Dict[str, Any],
        target_channel: Union[str, int],
    ):
        self._client = client
        self._db = db
        self._media_processor = media_processor
        self._config = config
        self._target_channel = target_channel
        self._target_entity = None
        self._state = PublisherState.STOPPED
        self._publish_task: Optional[asyncio.Task] = None
        self._stats_lock = asyncio.Lock()
        self._published_count = 0
        self._failed_count = 0
        self._last_post_time: Optional[float] = None
        self._consecutive_errors = 0
        self._max_consecutive_errors = 10
        self._backoff_base = 60

    @property
    def state(self) -> str:
        return self._state

    @property
    def published_count(self) -> int:
        return self._published_count

    @property
    def failed_count(self) -> int:
        return self._failed_count

    @property
    def last_post_time(self) -> Optional[float]:
        return self._last_post_time

    def update_config(self, config: Dict[str, Any]) -> None:
        """Обновляет конфигурацию публикации."""
        self._config = config

    async def start(self) -> None:
        """Запускает цикл публикации."""
        if self._state == PublisherState.RUNNING:
            logger.warning("Публикатор уже запущен")
            return

        try:
            self._target_entity = await self._client.get_entity(self._target_channel)
            logger.info("Целевой канал подключён: %s", self._target_channel)
        except Exception as e:
            logger.error("Не удалось подключиться к целевому каналу: %s", e)
            raise

        self._state = PublisherState.RUNNING
        self._publish_task = asyncio.create_task(
            self._publish_loop(), name="publisher_loop"
        )
        logger.info("Публикатор запущен")

    async def stop(self) -> None:
        """Останавливает публикатор."""
        self._state = PublisherState.STOPPED

        if self._publish_task and not self._publish_task.done():
            self._publish_task.cancel()
            try:
                await self._publish_task
            except asyncio.CancelledError:
                pass
            self._publish_task = None

        logger.info("Публикатор остановлен")

    def pause(self) -> None:
        """Ставит публикатор на паузу."""
        if self._state == PublisherState.RUNNING:
            self._state = PublisherState.PAUSED
            logger.info("Публикатор приостановлен")

    def resume(self) -> None:
        """Возобновляет работу публикатора."""
        if self._state == PublisherState.PAUSED:
            self._state = PublisherState.RUNNING
            logger.info("Публикатор возобновлён")

    async def publish_now(self, post: PostRecord) -> bool:
        """Принудительно публикует конкретный пост (вне очереди)."""
        return await self._publish_post(post)

    async def _publish_loop(self) -> None:
        """Основной цикл публикации."""
        logger.info("Цикл публикации запущен")

        while self._state != PublisherState.STOPPED:
            try:
                if self._state == PublisherState.PAUSED:
                    await asyncio.sleep(5)
                    continue

                if not self._is_within_schedule():
                    next_check = 60
                    logger.debug("Вне расписания, следующая проверка через %ds", next_check)
                    await asyncio.sleep(next_check)
                    continue

                post = await self._db.get_next_queued_post()
                if not post:
                    await asyncio.sleep(10)
                    continue

                delay = self._calculate_delay()
                if self._last_post_time:
                    elapsed = time.time() - self._last_post_time
                    remaining = delay - elapsed
                    if remaining > 0:
                        logger.debug("Ожидание %.0f сек до следующей публикации", remaining)
                        await self._interruptible_sleep(remaining)
                        if self._state == PublisherState.STOPPED:
                            break
                        if self._state == PublisherState.PAUSED:
                            continue

                success = await self._publish_post(post)

                if success:
                    self._consecutive_errors = 0
                else:
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= self._max_consecutive_errors:
                        backoff = min(
                            self._backoff_base * (2 ** (self._consecutive_errors - self._max_consecutive_errors)),
                            3600,
                        )
                        logger.error(
                            "Слишком много ошибок подряд (%d), пауза %d сек",
                            self._consecutive_errors,
                            backoff,
                        )
                        await self._interruptible_sleep(backoff)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Ошибка в цикле публикации: %s", e, exc_info=True)
                await self._db.log_error("publisher_loop", str(e))
                await asyncio.sleep(30)

        logger.info("Цикл публикации завершён")

    async def _publish_post(self, post: PostRecord) -> bool:
        """Публикует один пост в целевой канал."""
        if not self._target_entity:
            logger.error("Целевой канал не подключён")
            return False

        try:
            media_path = Path(post.media_path) if post.media_path else None
            has_media = media_path and media_path.exists()

            sent_message = None

            if has_media:
                sent_message = await self._send_media_post(
                    media_path, post.media_type, post.caption
                )
            elif post.caption:
                sent_message = await self._client.send_message(
                    self._target_entity,
                    post.caption,
                    link_preview=False,
                )
            else:
                logger.warning("Пост #%d без медиа и текста, пропуск", post.id)
                await self._db.update_post_status(
                    post.id, PostStatus.SKIPPED, error_message="Нет контента"
                )
                return True

            if sent_message:
                target_msg_id = sent_message.id
                await self._db.update_post_status(
                    post.id, PostStatus.POSTED, target_message_id=target_msg_id
                )
                if post.donor_channel_id:
                    await self._db.increment_donor_stats(
                        post.donor_channel_id, posted=1
                    )

                async with self._stats_lock:
                    self._published_count += 1
                self._last_post_time = time.time()

                if has_media and self._media_processor._config.get("cleanup_after_post", True):
                    self._media_processor.cleanup_file(media_path)

                logger.info(
                    "Опубликован пост #%d из %s (target_msg=%d)",
                    post.id,
                    post.donor_channel_name,
                    target_msg_id,
                )
                return True
            else:
                raise Exception("send_message вернул None")

        except errors.FloodWaitError as e:
            wait_time = e.seconds
            logger.warning("FloodWait при публикации: %d сек", wait_time)
            await self._db.update_post_status(
                post.id,
                PostStatus.QUEUED,
                error_message=f"FloodWait {wait_time}s",
            )
            await asyncio.sleep(min(wait_time, 300))
            return False

        except errors.ChatWriteForbiddenError:
            logger.error("Нет прав на отправку в целевой канал!")
            await self._db.update_post_status(
                post.id,
                PostStatus.FAILED,
                error_message="Нет прав на запись",
            )
            self.pause()
            return False

        except errors.MediaEmptyError:
            logger.warning("Telegram отклонил медиа (пустое или невалидное)")
            await self._db.update_post_status(
                post.id,
                PostStatus.FAILED,
                error_message="MediaEmptyError",
            )
            async with self._stats_lock:
                self._failed_count += 1
            return False

        except errors.FilePartsInvalidError:
            logger.warning("Невалидный файл для загрузки")
            await self._db.update_post_status(
                post.id,
                PostStatus.FAILED,
                error_message="FilePartsInvalidError",
            )
            async with self._stats_lock:
                self._failed_count += 1
            return False

        except errors.RPCError as e:
            logger.error("RPC ошибка при публикации: %s", e)
            await self._db.update_post_status(
                post.id,
                PostStatus.FAILED,
                error_message=f"RPC: {e}",
            )
            await self._db.log_error("publish_rpc", str(e), f"post_id={post.id}")
            async with self._stats_lock:
                self._failed_count += 1
            return False

        except Exception as e:
            logger.error("Неожиданная ошибка при публикации поста #%d: %s", post.id, e)
            await self._db.update_post_status(
                post.id,
                PostStatus.FAILED,
                error_message=str(e)[:200],
            )
            await self._db.log_error("publish", str(e), f"post_id={post.id}")
            async with self._stats_lock:
                self._failed_count += 1
            return False

    async def _send_media_post(
        self,
        media_path: Path,
        media_type: str,
        caption: str,
    ) -> Any:
        """Отправляет медиа-пост в целевой канал."""
        file_path = str(media_path)
        kwargs = {
            "entity": self._target_entity,
            "file": file_path,
            "caption": caption if len(caption) <= 1024 else caption[:1020] + "...",
        }

        if media_type == "video":
            duration = await self._media_processor.get_video_duration(media_path)
            dimensions = await self._media_processor.get_video_dimensions(media_path)

            attrs = []
            if duration or dimensions:
                w, h = dimensions if dimensions else (0, 0)
                attrs.append(DocumentAttributeVideo(
                    duration=int(duration or 0),
                    w=w,
                    h=h,
                    supports_streaming=True,
                ))
            if attrs:
                kwargs["attributes"] = attrs

        elif media_type == "voice":
            kwargs["voice_note"] = True
        elif media_type == "video_note":
            kwargs["video_note"] = True

        return await self._client.send_file(**kwargs)

    def _calculate_delay(self) -> float:
        """Рассчитывает задержку перед следующей публикацией."""
        min_d = self._config.get("min_delay_seconds", 300)
        max_d = self._config.get("max_delay_seconds", 900)

        if min_d >= max_d:
            return float(min_d)
        return random.uniform(min_d, max_d)

    def _is_within_schedule(self) -> bool:
        """Проверяет, находится ли текущее время в расписании публикации."""
        schedule = self._config.get("schedule_hours", {"start": 0, "end": 24})
        start_h = schedule.get("start", 0)
        end_h = schedule.get("end", 24)

        if start_h == 0 and end_h == 24:
            return True

        current_hour = datetime.now().hour + datetime.now().minute / 60.0

        if start_h <= end_h:
            return start_h <= current_hour < end_h
        else:
            return current_hour >= start_h or current_hour < end_h

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Прерываемый sleep — проверяет состояние каждые 2 секунды."""
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self._state == PublisherState.STOPPED:
                return
            remaining = end_time - time.time()
            await asyncio.sleep(min(2.0, remaining))

    async def get_status(self) -> Dict[str, Any]:
        """Возвращает статус публикатора."""
        queue_size = await self._db.get_queue_size()
        return {
            "state": self._state,
            "published_total": self._published_count,
            "failed_total": self._failed_count,
            "queue_size": queue_size,
            "consecutive_errors": self._consecutive_errors,
            "last_post_time": (
                datetime.fromtimestamp(self._last_post_time).isoformat()
                if self._last_post_time
                else None
            ),
            "target_channel": str(self._target_channel),
            "min_delay": self._config.get("min_delay_seconds", 300),
            "max_delay": self._config.get("max_delay_seconds", 900),
        }
