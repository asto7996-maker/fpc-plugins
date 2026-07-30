"""
Redis-backed async task broker.

We intentionally avoid Celery here: Celery workers are sync-first and fight
Telethon's asyncio event loop. This broker keeps everything in one async
process (or multiple asyncio workers) while still using Redis for durability
and cross-process visibility.

Celery can still be bolted on later for heavy offline jobs; for Telegram
side-effects prefer this path + APScheduler delays.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from redis.asyncio import Redis

from tg_pool.config import Settings, get_settings

logger = logging.getLogger(__name__)

TaskHandler = Callable[["PoolTask"], Awaitable[None]]

QUEUE_KEY = "tg_pool:tasks"
DELAYED_KEY = "tg_pool:tasks:delayed"
PROCESSING_KEY = "tg_pool:tasks:processing"


@dataclass
class PoolTask:
    """Unit of work routed to a healthy userbot account."""

    kind: str  # e.g. "send_message", "spambot_check", "custom"
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    account_id: Optional[int] = None  # preferred / pinned account (optional)
    attempts: int = 0
    max_attempts: int = 5
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "PoolTask":
        data = json.loads(raw)
        return cls(**data)


class RedisTaskBroker:
    """Simple reliable-ish queue: LPUSH + BRPOPLPUSH processing list."""

    def __init__(
        self,
        redis: Redis,
        *,
        settings: Optional[Settings] = None,
    ) -> None:
        self.redis = redis
        self.settings = settings or get_settings()
        self._handlers: dict[str, TaskHandler] = {}
        self._worker_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    def register(self, kind: str, handler: TaskHandler) -> None:
        self._handlers[kind] = handler

    async def enqueue(self, task: PoolTask) -> str:
        await self.redis.lpush(QUEUE_KEY, task.to_json())
        logger.info("Enqueued task %s kind=%s", task.task_id, task.kind)
        return task.task_id

    async def enqueue_delayed(self, task: PoolTask, delay_sec: float) -> str:
        """Schedule via Redis ZSET score = unix timestamp; APScheduler also ok."""
        run_at = datetime.now(timezone.utc).timestamp() + max(0.0, delay_sec)
        await self.redis.zadd(DELAYED_KEY, {task.to_json(): run_at})
        logger.info(
            "Delayed task %s kind=%s in %.1fs",
            task.task_id,
            task.kind,
            delay_sec,
        )
        return task.task_id

    async def promote_delayed(self) -> int:
        """Move due delayed tasks into the main queue. Called by scheduler tick."""
        now = datetime.now(timezone.utc).timestamp()
        # ZRANGEBYSCORE + ZREMRANGEBYSCORE atomically-ish via pipeline
        items = await self.redis.zrangebyscore(DELAYED_KEY, min="-inf", max=now)
        if not items:
            return 0
        pipe = self.redis.pipeline()
        for raw in items:
            pipe.lpush(QUEUE_KEY, raw)
            pipe.zrem(DELAYED_KEY, raw)
        await pipe.execute()
        return len(items)

    async def start_worker(self) -> None:
        if self._worker_task is not None:
            return
        self._stopped.clear()
        self._worker_task = asyncio.create_task(self._loop(), name="redis-task-worker")

    async def stop_worker(self) -> None:
        self._stopped.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def _loop(self) -> None:
        logger.info("Redis task worker started")
        while not self._stopped.is_set():
            try:
                item = await self.redis.brpoplpush(
                    QUEUE_KEY, PROCESSING_KEY, timeout=2
                )
                if item is None:
                    continue
                raw = item.decode() if isinstance(item, bytes) else item
                task = PoolTask.from_json(raw)
                await self._dispatch(task)
                await self.redis.lrem(PROCESSING_KEY, 1, item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Worker loop error")
                await asyncio.sleep(1)
        logger.info("Redis task worker stopped")

    async def _dispatch(self, task: PoolTask) -> None:
        handler = self._handlers.get(task.kind)
        if handler is None:
            logger.error("No handler for task kind=%s id=%s", task.kind, task.task_id)
            return
        task.attempts += 1
        try:
            await handler(task)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Task %s failed: %s", task.task_id, exc)
            if task.attempts < task.max_attempts:
                # Exponential-ish requeue with delay
                delay = min(300.0, 5.0 * (2 ** (task.attempts - 1)))
                await self.enqueue_delayed(task, delay)
