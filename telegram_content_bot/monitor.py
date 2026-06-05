"""
Модуль мониторинга каналов-доноров.
Отслеживает новые посты в указанных каналах, скачивает медиа,
прогоняет через фильтры и помещает в очередь публикации.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from telethon import TelegramClient, events, errors
from telethon.tl.types import (
    Channel,
    Chat,
    Document,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
    PeerChannel,
    Photo,
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
)

from database import Database, DonorRecord, PostRecord, PostStatus
from filters import ContentFilters, TextProcessor
from media_processor import MediaProcessor

logger = logging.getLogger(__name__)


class ChannelMonitor:
    """
    Мониторит каналы-доноры через Telethon-клиент.
    Подписывается на обновления и обрабатывает новые посты.
    """

    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        filters: ContentFilters,
        media_processor: MediaProcessor,
        config: Dict[str, Any],
        posting_config: Dict[str, Any],
    ):
        self._client = client
        self._db = db
        self._filters = filters
        self._media_processor = media_processor
        self._config = config
        self._posting_config = posting_config
        self._running = False
        self._donor_entities: Dict[int, Any] = {}
        self._event_handler = None
        self._poll_task: Optional[asyncio.Task] = None
        self._stats_lock = asyncio.Lock()
        self._processed_count = 0
        self._error_count = 0
        self._last_check_time: Optional[float] = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def processed_count(self) -> int:
        return self._processed_count

    @property
    def error_count(self) -> int:
        return self._error_count

    async def start(self, donor_list: List[Union[str, int]]) -> None:
        """Запускает мониторинг каналов-доноров."""
        if self._running:
            logger.warning("Мониторинг уже запущен")
            return

        self._running = True
        logger.info("Запуск мониторинга %d каналов-доноров...", len(donor_list))

        await self._resolve_donors(donor_list)

        if not self._donor_entities:
            logger.error("Не удалось подключиться ни к одному каналу-донору")
            self._running = False
            return

        self._register_event_handler()

        check_interval = self._config.get("check_interval_seconds", 30)
        self._poll_task = asyncio.create_task(
            self._poll_loop(check_interval),
            name="monitor_poll_loop",
        )

        logger.info(
            "Мониторинг запущен: %d каналов, интервал %ds",
            len(self._donor_entities),
            check_interval,
        )

    async def stop(self) -> None:
        """Останавливает мониторинг."""
        self._running = False

        if self._event_handler is not None:
            self._client.remove_event_handler(self._event_handler)
            self._event_handler = None

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        logger.info("Мониторинг остановлен")

    async def add_donor(self, channel: Union[str, int]) -> Optional[DonorRecord]:
        """Добавляет новый канал-донор на лету."""
        try:
            entity = await self._client.get_entity(channel)
            channel_id = self._get_channel_id(entity)
            if channel_id is None:
                logger.error("Не удалось определить ID канала: %s", channel)
                return None

            username = getattr(entity, "username", "") or ""
            title = getattr(entity, "title", "") or str(channel)

            donor = DonorRecord(
                channel_id=channel_id,
                channel_username=username,
                channel_title=title,
                enabled=True,
                added_at=datetime.utcnow().isoformat(),
            )

            await self._db.add_donor(donor)
            self._donor_entities[channel_id] = entity

            if self._running and self._event_handler is not None:
                self._client.remove_event_handler(self._event_handler)
                self._register_event_handler()

            logger.info("Донор добавлен: %s (ID: %d)", title, channel_id)
            return donor

        except Exception as e:
            logger.error("Ошибка при добавлении донора %s: %s", channel, e)
            return None

    async def remove_donor(self, channel_id: int) -> bool:
        """Удаляет канал-донор."""
        if channel_id in self._donor_entities:
            del self._donor_entities[channel_id]

        result = await self._db.remove_donor(channel_id)

        if self._running and self._event_handler is not None:
            self._client.remove_event_handler(self._event_handler)
            self._register_event_handler()

        if result:
            logger.info("Донор удалён: %d", channel_id)
        return result

    async def _resolve_donors(self, donor_list: List[Union[str, int]]) -> None:
        """Резолвит сущности каналов-доноров."""
        for donor in donor_list:
            try:
                entity = await self._client.get_entity(donor)
                channel_id = self._get_channel_id(entity)
                if channel_id is None:
                    logger.warning("Пропускаю не-канал: %s", donor)
                    continue

                self._donor_entities[channel_id] = entity

                username = getattr(entity, "username", "") or ""
                title = getattr(entity, "title", "") or str(donor)

                existing = await self._db.get_donor(channel_id)
                if not existing:
                    record = DonorRecord(
                        channel_id=channel_id,
                        channel_username=username,
                        channel_title=title,
                        enabled=True,
                        added_at=datetime.utcnow().isoformat(),
                    )
                    await self._db.add_donor(record)

                logger.info("Донор подключён: %s [%d]", title, channel_id)

            except errors.ChannelPrivateError:
                logger.error("Канал %s приватный или недоступен", donor)
            except errors.UsernameNotOccupiedError:
                logger.error("Канал %s не найден", donor)
            except errors.FloodWaitError as e:
                logger.warning("FloodWait %d сек при резолве %s", e.seconds, donor)
                await asyncio.sleep(min(e.seconds, 60))
            except Exception as e:
                logger.error("Ошибка при подключении к %s: %s", donor, e)

    def _register_event_handler(self) -> None:
        """Регистрирует обработчик новых сообщений в каналах-донорах."""
        chats = list(self._donor_entities.values())
        if not chats:
            return

        @self._client.on(events.NewMessage(chats=chats))
        async def on_new_message(event: events.NewMessage.Event):
            if not self._running:
                return
            try:
                await self._handle_new_post(event.message)
            except Exception as e:
                logger.error("Ошибка обработки нового поста: %s", e, exc_info=True)
                async with self._stats_lock:
                    self._error_count += 1
                await self._db.log_error(
                    "monitor_event",
                    str(e),
                    f"message_id={event.message.id}" if event.message else "",
                )

        self._event_handler = on_new_message

    async def _poll_loop(self, interval: int) -> None:
        """Фоновый цикл polling для проверки пропущенных постов."""
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break

                self._last_check_time = time.time()
                await self._check_missed_posts()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Ошибка в poll_loop: %s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _check_missed_posts(self) -> None:
        """Проверяет пропущенные посты в каналах-донорах."""
        max_age = self._config.get("max_post_age_hours", 24)
        min_date = datetime.now(timezone.utc) - timedelta(hours=max_age)

        donors = await self._db.get_all_donors(enabled_only=True)
        for donor in donors:
            if not self._running:
                break
            if donor.channel_id not in self._donor_entities:
                continue

            try:
                entity = self._donor_entities[donor.channel_id]
                last_id = donor.last_post_id

                messages = await self._client.get_messages(
                    entity,
                    limit=20,
                    min_id=last_id if last_id > 0 else 0,
                )

                if not messages:
                    continue

                new_count = 0
                for msg in reversed(messages):
                    if not isinstance(msg, Message):
                        continue
                    if msg.date and msg.date < min_date:
                        continue
                    if last_id > 0 and msg.id <= last_id:
                        continue

                    already = await self._db.post_exists(donor.channel_id, msg.id)
                    if already:
                        continue

                    await self._handle_new_post(msg)
                    new_count += 1
                    await asyncio.sleep(1)

                if messages:
                    max_id = max(m.id for m in messages if isinstance(m, Message))
                    if max_id > last_id:
                        await self._db.update_donor_last_post(donor.channel_id, max_id)

                if new_count > 0:
                    logger.info(
                        "Обнаружено %d пропущенных постов в %s",
                        new_count,
                        donor.channel_title,
                    )

            except errors.FloodWaitError as e:
                logger.warning(
                    "FloodWait %d сек при проверке %s",
                    e.seconds,
                    donor.channel_title,
                )
                await asyncio.sleep(min(e.seconds, 60))
            except errors.ChannelPrivateError:
                logger.error(
                    "Канал %s стал приватным, отключаю",
                    donor.channel_title,
                )
                await self._db.toggle_donor(donor.channel_id, False)
            except Exception as e:
                logger.error(
                    "Ошибка при проверке %s: %s",
                    donor.channel_title,
                    e,
                )

    async def _handle_new_post(self, message: Message) -> None:
        """Обрабатывает новый пост из канала-донора."""
        if not message or not isinstance(message, Message):
            return

        chat_id = self._get_message_chat_id(message)
        if chat_id is None:
            return

        if self._config.get("skip_edits", True) and message.edit_date:
            return

        already = await self._db.post_exists(chat_id, message.id)
        if already:
            return

        text = message.text or message.message or ""
        has_media = message.media is not None
        media_type = self._detect_media_type(message)
        is_forward = message.forward is not None
        views = message.views or 0

        filter_result = self._filters.check_all(
            text=text,
            media_type=media_type,
            views=views,
            is_forward=is_forward,
            has_media=has_media,
        )

        donor = await self._db.get_donor(chat_id)
        donor_name = donor.channel_title if donor else str(chat_id)

        if not filter_result.passed:
            logger.info(
                "Пост %d из %s отфильтрован (%s): %s",
                message.id,
                donor_name,
                filter_result.filter_name,
                filter_result.reason,
            )
            post = PostRecord(
                donor_channel_id=chat_id,
                donor_channel_name=donor_name,
                original_message_id=message.id,
                media_type=media_type,
                caption=text[:500],
                text_hash=Database.compute_text_hash(text),
                status=PostStatus.SKIPPED.value,
                views_at_capture=views,
                error_message=f"Фильтр: {filter_result.reason}",
            )
            await self._db.add_post(post)
            if donor:
                await self._db.increment_donor_stats(chat_id, captured=1, skipped=1)
            return

        text_hash = Database.compute_text_hash(text)
        if text_hash and await self._db.is_text_duplicate(text_hash):
            logger.info("Пост %d из %s — дубликат текста", message.id, donor_name)
            post = PostRecord(
                donor_channel_id=chat_id,
                donor_channel_name=donor_name,
                original_message_id=message.id,
                media_type=media_type,
                caption=text[:500],
                text_hash=text_hash,
                status=PostStatus.DUPLICATE.value,
                views_at_capture=views,
            )
            await self._db.add_post(post)
            return

        media_path = ""
        media_hash = ""
        if has_media and media_type not in ("webpage", ""):
            try:
                media_path, media_hash = await self._download_and_process_media(
                    message, media_type
                )
                if media_hash and await self._db.is_media_duplicate(media_hash):
                    logger.info(
                        "Пост %d из %s — дубликат медиа", message.id, donor_name
                    )
                    post = PostRecord(
                        donor_channel_id=chat_id,
                        donor_channel_name=donor_name,
                        original_message_id=message.id,
                        media_type=media_type,
                        media_path=media_path,
                        caption=text[:500],
                        text_hash=text_hash,
                        media_hash=media_hash,
                        status=PostStatus.DUPLICATE.value,
                        views_at_capture=views,
                    )
                    await self._db.add_post(post)
                    return
            except Exception as e:
                logger.error(
                    "Ошибка скачивания медиа из поста %d: %s", message.id, e
                )
                await self._db.log_error("media_download", str(e), f"msg={message.id}")

        processed_caption = TextProcessor.process_caption(
            text, self._posting_config, donor_name
        )

        queue_size = await self._db.get_queue_size()
        max_queue = self._posting_config.get("max_queue_size", 500)
        if queue_size >= max_queue:
            logger.warning("Очередь заполнена (%d/%d), пропускаю", queue_size, max_queue)
            return

        post = PostRecord(
            donor_channel_id=chat_id,
            donor_channel_name=donor_name,
            original_message_id=message.id,
            media_type=media_type,
            media_path=media_path,
            caption=processed_caption,
            text_hash=text_hash,
            media_hash=media_hash,
            status=PostStatus.QUEUED.value,
            views_at_capture=views,
        )

        post_id = await self._db.add_post(post)
        if post_id:
            if text_hash:
                await self._db.add_text_hash(text_hash, chat_id, message.id)
            if media_hash:
                await self._db.add_media_hash(media_hash, chat_id, message.id)

            if donor:
                await self._db.increment_donor_stats(chat_id, captured=1)
                await self._db.update_donor_last_post(chat_id, message.id)

            async with self._stats_lock:
                self._processed_count += 1

            logger.info(
                "Пост добавлен в очередь: #%d из %s [%s] (ID: %d)",
                message.id,
                donor_name,
                media_type or "text",
                post_id,
            )

    async def _download_and_process_media(
        self, message: Message, media_type: str
    ) -> Tuple[str, str]:
        """Скачивает и обрабатывает медиафайл. Возвращает (путь, хеш)."""
        timeout = self._config.get("download_timeout_seconds", 120)

        ext = self._get_media_extension(message, media_type)
        download_path = self._media_processor.get_unique_path(ext)

        try:
            path = await asyncio.wait_for(
                self._client.download_media(message, file=str(download_path)),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise Exception(f"Таймаут скачивания ({timeout}s)")

        if not path or not Path(path).exists():
            raise Exception("Файл не был скачан")

        actual_path = Path(path)
        result_path, media_hash = await self._media_processor.process_media(
            actual_path, media_type
        )

        return str(result_path), media_hash

    @staticmethod
    def _detect_media_type(message: Message) -> str:
        """Определяет тип медиа в сообщении."""
        if not message.media:
            return ""

        if isinstance(message.media, MessageMediaPhoto):
            return "photo"

        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            if isinstance(doc, Document):
                for attr in doc.attributes:
                    if isinstance(attr, DocumentAttributeAnimated):
                        return "animation"
                    if isinstance(attr, DocumentAttributeSticker):
                        return "sticker"
                    if isinstance(attr, DocumentAttributeVideo):
                        if attr.round_message:
                            return "video_note"
                        return "video"
                    if isinstance(attr, DocumentAttributeAudio):
                        if attr.voice:
                            return "voice"
                        return "audio"
                mime = doc.mime_type or ""
                if mime.startswith("video/"):
                    return "video"
                if mime.startswith("image/"):
                    return "photo"
                if mime.startswith("audio/"):
                    return "audio"
                return "document"

        if isinstance(message.media, MessageMediaWebPage):
            return "webpage"

        return "other"

    @staticmethod
    def _get_media_extension(message: Message, media_type: str) -> str:
        """Определяет расширение файла для медиа."""
        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            if isinstance(doc, Document):
                for attr in doc.attributes:
                    if isinstance(attr, DocumentAttributeFilename):
                        name = attr.file_name
                        if "." in name:
                            return "." + name.rsplit(".", 1)[1].lower()
                mime = doc.mime_type or ""
                mime_map = {
                    "video/mp4": ".mp4",
                    "video/quicktime": ".mov",
                    "video/x-matroska": ".mkv",
                    "audio/mpeg": ".mp3",
                    "audio/ogg": ".ogg",
                    "audio/mp4": ".m4a",
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                    "application/pdf": ".pdf",
                }
                return mime_map.get(mime, ".bin")

        type_map = {
            "photo": ".jpg",
            "video": ".mp4",
            "animation": ".mp4",
            "audio": ".mp3",
            "voice": ".ogg",
            "video_note": ".mp4",
            "sticker": ".webp",
        }
        return type_map.get(media_type, ".bin")

    @staticmethod
    def _get_channel_id(entity: Any) -> Optional[int]:
        """Извлекает ID канала из сущности Telethon."""
        if isinstance(entity, Channel):
            return entity.id
        if hasattr(entity, "id"):
            return entity.id
        return None

    @staticmethod
    def _get_message_chat_id(message: Message) -> Optional[int]:
        """Извлекает ID чата из сообщения."""
        if message.peer_id:
            if isinstance(message.peer_id, PeerChannel):
                return message.peer_id.channel_id
        if message.chat_id:
            return message.chat_id
        return None

    async def get_donor_info(self) -> List[Dict[str, Any]]:
        """Возвращает информацию обо всех донорах."""
        donors = await self._db.get_all_donors()
        result = []
        for d in donors:
            result.append({
                "channel_id": d.channel_id,
                "username": d.channel_username,
                "title": d.channel_title,
                "enabled": d.enabled,
                "last_post_id": d.last_post_id,
                "total_captured": d.total_captured,
                "total_posted": d.total_posted,
                "total_skipped": d.total_skipped,
                "connected": d.channel_id in self._donor_entities,
            })
        return result
