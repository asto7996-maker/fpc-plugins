"""
poster.py — чистый USERBOT-перезалив через Pyrogram.

Аккаунт:
  • в источнике — подписчик (админ не обязателен);
  • в назначении — админ с правом постить.

Копирует без метки «Forwarded from», альбомы целиком, HTML-подпись.
Порядок публикации — от старых постов к новым.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from dataclasses import dataclass
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

# Итоги цикла
REASON_OK = "ok"
REASON_PAUSED = "paused"
REASON_NO_START = "no_start"
REASON_SOURCE_EMPTY = "source_empty"
REASON_UP_TO_DATE = "up_to_date"
REASON_ABORTED = "aborted"
REASON_FLOOD = "flood"
REASON_FATAL = "fatal"
REASON_ERROR = "error"

# Долгий FloodWait не «пересиживаем» внутри цикла — отдаём планировщику
FLOOD_INLINE_LIMIT = 90
# Суммарный лимит ожидания flood внутри одного цикла
FLOOD_BUDGET = 300
# Предохранитель от бесконечного цикла при странных ответах API
MAX_STEPS_PER_CYCLE = 600
# Сетевые сбои внутри одного поста
NETWORK_RETRIES = 2
# Таймаут одного сетевого запроса к Telegram (иначе worker-loop клинит)
RPC_TIMEOUT = 90.0


async def _rpc(coro, *, timeout: float = RPC_TIMEOUT, label: str = "rpc"):
    """Обёртка с таймаутом: зависший await не держит весь поток юзербота."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"{label} timeout after {timeout:.0f}s") from e

_NETWORK_ERRORS = (
    OSError,
    ConnectionError,
    asyncio.TimeoutError,
    TimeoutError,
)


@dataclass
class CycleResult:
    """Что произошло за один цикл публикации."""

    published: int = 0
    reason: str = REASON_OK
    error: str = ""
    fatal_text: str = ""
    flood_seconds: float = 0.0
    latest_id: int = 0
    progress_id: int = 0
    needs_reconnect: bool = False
    errors: int = 0

    @property
    def fatal(self) -> bool:
        return self.reason == REASON_FATAL

    @property
    def backlog(self) -> int:
        return max(0, self.latest_id - self.progress_id)

    def __int__(self) -> int:  # обратная совместимость со «сколько опубликовано»
        return self.published

    def __bool__(self) -> bool:
        return self.published > 0


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


def _source_text_html(msg: Message) -> str:
    """Текст поста в HTML: жирный/ссылки из entities сохраняются."""
    raw = msg.text if msg.text is not None else msg.caption
    if raw is None:
        return ""
    rich = getattr(raw, "html", None)
    if isinstance(rich, str) and rich.strip():
        return rich
    return html.escape(str(raw))


def _merge_text(body: str, template: str) -> str:
    """
    Текстовый пост + шаблон.

    Шаблон дописывается снизу, а не затирает пост: иначе перезалив
    текстовых постов терял бы содержимое.
    """
    body = (body or "").strip()
    template = (template or "").strip()
    if not template:
        return body
    if not body:
        return template
    if template in body:
        return body
    return f"{body}\n\n{template}"


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
        self._abort_cycle = False
        self._busy_since = 0.0
        self._cycle_gen = 0
        self.last_result: Optional[CycleResult] = None
        # Быстрый путь поиска новых ID: None — ещё не проверяли, False — не работает
        self._history_window_ok: Optional[bool] = None
        self._history_window_trusted = 0

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

    def request_abort(self) -> None:
        """Прервать текущий цикл как можно скорее."""
        self._abort_cycle = True

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def busy_seconds(self) -> float:
        if not self._busy or self._busy_since <= 0:
            return 0.0
        return max(0.0, time.monotonic() - self._busy_since)

    async def wait_until_idle(self, timeout: float = 120.0) -> bool:
        """Дождаться окончания цикла. True = свободен."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._busy:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.25)
        return True

    async def apply_start_link(self, link: str) -> tuple[str | int, int]:
        """
        Старт ПОСЛЕ указанного поста (сам пост не публикуется).
        Останавливает текущий цикл и сбрасывает историю после этой точки.
        """
        chat_ref, message_id = parse_post_link(link)
        src = normalize_channel(chat_ref)

        self.request_abort()
        await self.wait_until_idle(timeout=180.0)

        cleared = self.db.clear_history_after(message_id)
        self.db.set_source_channel(src)
        self.db.set_start_link(link)
        self.db.set_progress_id(message_id)
        self.db.run_asap()
        self._seen_grouped.clear()
        self._abort_cycle = False
        logger.info(
            "Start link → chat=%s after_id=%s (next=%s), history_cleared_after=%s",
            src,
            message_id,
            message_id + 1,
            cleared,
        )
        return chat_ref, message_id

    async def seek_oldest(self) -> int:
        """
        Начать с самого старого поста источника → к новым.
        Сбрасывает историю ok, чтобы не пропускать уже «обработанные» id.
        """
        self.request_abort()
        await self.wait_until_idle(timeout=180.0)

        settings = self.db.get_settings()
        source = await _resolve_chat(
            self.client, settings.source_channel or config.SOURCE_CHANNEL
        )
        found = await self._find_oldest_message_id(source)
        if found <= 0:
            raise RuntimeError("Не удалось найти первый пост в источнике")

        cleared = self.db.clear_history()
        self.db.set_progress_id(found - 1)
        self.db.set_start_link(f"oldest:{found}")
        self.db.run_asap()
        self._seen_grouped.clear()
        self._abort_cycle = False
        logger.info(
            "Oldest post id=%s → progress=%s (history cleared=%s)",
            found,
            found - 1,
            cleared,
        )
        return found

    async def _find_oldest_message_id(self, source: int | str) -> int:
        """
        ID самого старого доступного поста.

        Быстрый путь — история с offset_id=1 (Telegram отдаёт самые старые).
        Если API не поддержал такой запрос — редкий линейный поиск по ID.
        """
        try:
            ids = await self._history_ids_after(source, after_id=0, want=5)
            if ids:
                older = await self._first_id_older_than(source, ids[0])
                if older <= 0:
                    return ids[0]
                logger.warning(
                    "oldest fast path вернул %s, но есть более старый %s",
                    ids[0],
                    older,
                )
        except Exception as e:
            logger.warning("oldest fast path failed: %s", e)

        logger.info("oldest: линейный поиск по ID (медленный путь)")
        empty = 0
        for mid in range(1, 20001):
            try:
                msg = await self.client.get_messages(source, mid)
            except FloodWait as e:
                await asyncio.sleep(min(int(e.value), 30) + 1)
                continue
            except MessageIdInvalid:
                empty += 1
                if empty > 200:
                    break
                continue
            except RPCError:
                empty += 1
                if empty > 200:
                    break
                continue

            if msg is None or getattr(msg, "empty", False):
                empty += 1
                if empty > 200:
                    break
                continue
            return mid
        return 0

    def cancel_rewrite(self) -> None:
        self._rewrite_cancel = True

    def force_unlock(self) -> None:
        """Снять залипший busy после обрыва сети / вечного FloodWait."""
        logger.warning(
            "force_unlock busy=%s since=%.0f abort=%s gen=%s",
            self._busy,
            self._busy_since,
            self._abort_cycle,
            self._cycle_gen,
        )
        self._abort_cycle = True
        self._cycle_gen += 1  # любой старый цикл сразу выйдет
        self._busy = False
        self._busy_since = 0.0

    async def run_cycle(
        self,
        limit: Optional[int] = None,
        *,
        force: bool = False,
    ) -> CycleResult:
        """
        Один цикл публикации.

        Args:
            limit: разовый лимит публикаций (иначе берётся из настроек).
            force: игнорировать «пауза» — для ручного «Цикл сейчас» и теста.
        """
        # Если уже идёт цикл — прерываем и ждём, не возвращаем тихо 0
        if self._busy:
            logger.info("cycle waiting: busy")
            self.request_abort()
            ok = await self.wait_until_idle(timeout=180.0)
            if not ok or self._busy:
                logger.warning("cycle still busy — force unlock")
                self.force_unlock()

        self._cycle_gen += 1
        my_gen = self._cycle_gen
        self._abort_cycle = False
        self._busy = True
        self._busy_since = time.monotonic()
        try:
            result = await self._run_cycle_inner(my_gen, limit=limit, force=force)
        except FloodWait as e:
            result = CycleResult(
                reason=REASON_FLOOD,
                flood_seconds=float(getattr(e, "value", 0) or 0),
                error=str(e),
            )
        except _NETWORK_ERRORS as e:
            result = CycleResult(
                reason=REASON_ERROR, error=f"сеть: {e}", needs_reconnect=True
            )
        except Exception as e:
            # Панель и логи должны показывать причину, а не «тишину»
            logger.exception("Цикл упал")
            result = CycleResult(
                reason=REASON_ERROR, error=str(e) or type(e).__name__
            )
        finally:
            # Снимаем busy только если это всё ещё «наш» цикл
            if my_gen == self._cycle_gen:
                self._busy = False
                self._busy_since = 0.0
                self._abort_cycle = False
        self.last_result = result
        return result

    async def _latest_message_id(self, source: int | str) -> int:
        """ID последнего поста в канале (0 если пусто)."""
        try:
            async def _fetch():
                async for msg in self.client.get_chat_history(source, limit=1):
                    return int(msg.id)
                return 0

            return await _rpc(_fetch(), label="latest_message_id")
        except FloodWait:
            raise
        except Exception as e:
            logger.warning("latest_message_id failed: %s", e)
        return 0

    async def _history_ids_after(
        self, source: int | str, after_id: int, want: int
    ) -> list[int]:
        """
        Реальные ID сообщений источника с id > after_id, по возрастанию.

        Пропуски (удалённые посты, сервисные ID) не тратят запросы: Telegram
        сам отдаёт только существующие сообщения.
        """
        want = max(1, min(int(want), 100))
        found: set[int] = set()
        # offset_id + отрицательный offset = «окно» сообщений новее after_id
        async for msg in self.client.get_chat_history(
            source,
            limit=want,
            offset_id=max(1, after_id + 1),
            offset=-want,
        ):
            mid = int(getattr(msg, "id", 0) or 0)
            if mid > after_id:
                found.add(mid)
        return sorted(found)

    async def _first_id_older_than(self, source: int | str, message_id: int) -> int:
        """ID ближайшего поста СТАРШЕ указанного (0 — старше ничего нет)."""
        async for msg in self.client.get_chat_history(
            source, limit=1, offset_id=int(message_id)
        ):
            return int(getattr(msg, "id", 0) or 0)
        return 0

    def _crawl_ids(self, after_id: int, want: int, latest: int) -> list[int]:
        """Резервный путь: последовательный перебор ID (ничего не пропускает)."""
        span = min(max(want * 5, 20), 200)
        end = min(after_id + span, latest)
        return list(range(after_id + 1, end + 1))

    async def _next_ids(
        self, source: int | str, after_id: int, want: int, latest: int
    ) -> list[int]:
        """
        Кандидаты на публикацию (по возрастанию ID).

        Быстрый путь — окно истории Telegram: дыры из удалённых постов не
        стоят ни одного запроса. Он используется только если проверка
        подтвердила, что между after_id и окном нет пропущенных постов, —
        иначе честный перебор ID, чтобы ни один пост не потерялся.
        """
        if self._history_window_ok is False:
            return self._crawl_ids(after_id, want, latest)

        try:
            ids = await self._history_ids_after(source, after_id, want)
        except FloodWait:
            raise
        except Exception as e:
            logger.warning("history window failed (%s) → перебор ID", e)
            self._history_window_ok = False
            return self._crawl_ids(after_id, want, latest)

        if not ids:
            # Пустое окно, хотя посты впереди есть — уходим в перебор
            if after_id < latest:
                return self._crawl_ids(after_id, want, latest)
            return []

        if self._history_window_trusted < 2:
            try:
                older = await self._first_id_older_than(source, ids[0])
            except FloodWait:
                raise
            except Exception as e:
                logger.warning("history window check failed (%s) → перебор ID", e)
                self._history_window_ok = False
                return self._crawl_ids(after_id, want, latest)
            if older > after_id:
                logger.warning(
                    "history window пропустил посты (%s..%s) → перебор ID",
                    after_id + 1,
                    ids[0] - 1,
                )
                self._history_window_ok = False
                return self._crawl_ids(after_id, want, latest)
            self._history_window_trusted += 1
        return ids

    async def _run_cycle_inner(
        self,
        my_gen: int,
        *,
        limit: Optional[int] = None,
        force: bool = False,
    ) -> CycleResult:
        settings = self.db.get_settings()
        if not settings.is_running and not force:
            return CycleResult(reason=REASON_PAUSED, progress_id=settings.progress_id)

        source = await _rpc(
            _resolve_chat(
                self.client, settings.source_channel or config.SOURCE_CHANNEL
            ),
            label="resolve_source",
        )
        target = await _rpc(
            _resolve_chat(
                self.client, settings.target_channel or config.TARGET_CHANNEL
            ),
            label="resolve_target",
        )
        if settings.progress_id < 0:
            logger.warning("progress_id не задан")
            return CycleResult(reason=REASON_NO_START)

        latest = await self._latest_message_id(source)
        if latest <= 0:
            logger.warning("Источник пуст или недоступен")
            return CycleResult(
                reason=REASON_SOURCE_EMPTY,
                error="источник пуст или недоступен",
                progress_id=settings.progress_id,
            )
        self.db.set_latest_source_id(latest)

        # Progress ускакал вперёд по ещё несуществующим ID (дыра у «конца» канала)
        if settings.progress_id > latest:
            rewind = self.db.max_ok_source_id()
            if rewind > latest:
                rewind = latest
            logger.warning(
                "progress %s > latest %s — откат к %s",
                settings.progress_id,
                latest,
                rewind,
            )
            self.db.set_progress_id(rewind)
            settings = self.db.get_settings()

        progress = settings.progress_id
        if progress >= latest:
            logger.info("Нет новых постов: progress=%s latest=%s", progress, latest)
            return CycleResult(
                reason=REASON_UP_TO_DATE, latest_id=latest, progress_id=progress
            )

        limit_value = max(1, int(limit if limit else settings.posts_per_cycle))
        caption = settings.caption_template or ""
        published = 0
        errors = 0
        steps = 0
        flood_slept = 0.0
        pending: list[int] = []
        result = CycleResult(latest_id=latest, progress_id=progress)
        self._seen_grouped.clear()

        logger.info(
            "USERBOT cycle gen=%s %s → %s after=%s latest=%s limit=%s%s",
            my_gen,
            source,
            target,
            progress,
            latest,
            limit_value,
            " (force)" if force else "",
        )

        while published < limit_value and steps < MAX_STEPS_PER_CYCLE:
            steps += 1
            if self._abort_cycle or my_gen != self._cycle_gen:
                logger.info(
                    "cycle aborted gen=%s/%s published=%s",
                    my_gen,
                    self._cycle_gen,
                    published,
                )
                result.reason = REASON_ABORTED
                break

            live = self.db.get_settings()
            if not live.is_running and not force:
                result.reason = REASON_PAUSED
                break
            # Лимит можно уменьшить на лету — подхватываем каждый шаг
            if limit is None:
                limit_value = max(1, int(live.posts_per_cycle))
                if published >= limit_value:
                    break

            if not pending:
                if progress >= latest:
                    fresh = await self._latest_message_id(source)
                    if fresh > latest:
                        latest = fresh
                        self.db.set_latest_source_id(latest)
                        result.latest_id = latest
                    else:
                        logger.info(
                            "Дошли до конца источника (progress=%s latest=%s)",
                            progress,
                            latest,
                        )
                        break
                pending = await self._next_ids(
                    source, progress, want=limit_value - published + 3, latest=latest
                )
                if not pending:
                    logger.info(
                        "Нет доступных постов после %s (latest=%s)", progress, latest
                    )
                    break

            message_id = pending.pop(0)
            if message_id <= progress:
                continue
            if self.db.was_processed(message_id):
                progress = max(progress, message_id)
                self.db.set_progress_id(progress)
                continue

            try:
                status = await self._process_with_retry(
                    source, target, message_id, caption
                )
            except FloodWait as e:
                wait = float(getattr(e, "value", 0) or 0)
                # Короткие ожидания пересиживаем, длинные (и сумму долгих
                # коротких) отдаём планировщику — цикл не должен висеть
                if wait > FLOOD_INLINE_LIMIT or flood_slept + wait > FLOOD_BUDGET:
                    logger.warning("FloodWait %.0fs — отдаём планировщику", wait)
                    result.reason = REASON_FLOOD
                    result.flood_seconds = wait
                    break
                logger.warning("FloodWait %.0fs", wait)
                flood_slept += wait
                await asyncio.sleep(wait + 1)
                if self._abort_cycle or my_gen != self._cycle_gen:
                    result.reason = REASON_ABORTED
                    break
                pending.insert(0, message_id)
                continue
            except ChatWriteForbidden:
                logger.error("Нет прав писать в назначение (нужен админ юзербота)")
                result.reason = REASON_FATAL
                result.fatal_text = (
                    "нет прав публиковать в канале-назначении "
                    "(добавьте юзербота админом с правом «Публикация сообщений»)"
                )
                break
            except (ChannelPrivate, ChannelInvalid) as e:
                logger.error("Источник недоступен: %s", e)
                result.reason = REASON_FATAL
                result.fatal_text = f"источник недоступен: {e}"
                break
            except _NETWORK_ERRORS as e:
                logger.error("Сеть недоступна: %s", e)
                result.reason = REASON_ERROR
                result.error = f"сеть: {e}"
                result.needs_reconnect = True
                break
            except RPCError as e:
                err = str(e).upper()
                if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                    logger.error("Write forbidden: %s", e)
                    result.reason = REASON_FATAL
                    result.fatal_text = f"нет прав публикации: {e}"
                    break
                logger.error("RPC %s: %s", message_id, e)
                self.db.add_history(message_id, status="error", error=str(e))
                errors += 1
                result.error = str(e)
                progress = max(progress, message_id)
                self.db.set_progress_id(progress)
                continue

            if status == "empty":
                # Пустой ID — дыра в нумерации; после «кончика» канала не двигаемся
                if message_id >= latest:
                    fresh = await self._latest_message_id(source)
                    if fresh > latest:
                        latest = fresh
                        self.db.set_latest_source_id(latest)
                        result.latest_id = latest
                    else:
                        logger.info(
                            "Ждём новые посты (пусто id=%s, latest=%s)",
                            message_id,
                            latest,
                        )
                        break
                progress = max(progress, message_id)
                self.db.set_progress_id(progress)
                continue

            if status == "skip":
                progress = max(self.db.get_progress_id(), message_id, progress)
                self.db.set_progress_id(progress)
                continue

            published += 1
            progress = max(self.db.get_progress_id(), message_id, progress)
            self.db.set_progress_id(progress)
            if published < limit_value:
                await asyncio.sleep(
                    random.uniform(config.POST_DELAY_MIN, config.POST_DELAY_MAX)
                )

        result.published = published
        result.errors = errors
        result.progress_id = progress
        if result.reason == REASON_OK and published == 0:
            result.reason = REASON_UP_TO_DATE if progress >= latest else REASON_OK
        logger.info(
            "USERBOT done gen=%s published=%s limit=%s reason=%s progress=%s latest=%s",
            my_gen,
            published,
            limit_value,
            result.reason,
            progress,
            latest,
        )
        return result

    # ------------------------------------------------------------------ process

    async def _process_with_retry(
        self, source, target, message_id: int, caption: str
    ) -> str:
        """_process с повтором при разовых сетевых сбоях."""
        attempt = 0
        while True:
            try:
                return await self._process(source, target, message_id, caption)
            except _NETWORK_ERRORS as e:
                attempt += 1
                if attempt > NETWORK_RETRIES:
                    raise
                logger.warning(
                    "сетевой сбой на %s (%s), попытка %s", message_id, e, attempt
                )
                await asyncio.sleep(2.0 * attempt)

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
            if self.db.was_group_processed(gid):
                self._seen_grouped.add(gid)
                logger.info("album %s уже публиковался — пропуск", gid)
                return "skip"
            return await self._publish_album(source, target, msg, caption)

        if _is_media(msg) or msg.sticker or (msg.text or msg.caption):
            return await self._publish_single(target, msg, caption)

        if _is_unsupported_media(msg):
            return await self._publish_single(target, msg, caption)

        return "skip"

    async def _publish_single(self, target, msg: Message, caption: str) -> str:
        try:
            if msg.sticker:
                # У стикера не бывает подписи — копируем как есть, чтобы
                # не терять контент источника
                sent = await self.client.copy_message(
                    chat_id=target,
                    from_chat_id=msg.chat.id,
                    message_id=msg.id,
                )
            elif _is_media(msg):
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
                text = _merge_text(_source_text_html(msg), caption)
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
            raise
        except _NETWORK_ERRORS:
            raise
        except (RPCError, ValueError) as e:
            err = str(e).upper()
            if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                raise
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
        except FloodWait:
            self._seen_grouped.discard(gid)
            raise
        except _NETWORK_ERRORS:
            self._seen_grouped.discard(gid)
            raise
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
            raise
        except _NETWORK_ERRORS:
            self._seen_grouped.discard(gid)
            raise
        except (RPCError, ValueError) as e:
            err = str(e).upper()
            if "WRITE_FORBIDDEN" in err or "CHAT_WRITE" in err:
                raise
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
