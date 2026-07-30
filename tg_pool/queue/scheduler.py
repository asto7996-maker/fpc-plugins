"""APScheduler glue — promotes delayed Redis tasks and daily counter maintenance."""

from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from tg_pool.queue.broker import RedisTaskBroker

logger = logging.getLogger(__name__)


class PoolScheduler:
    def __init__(self, broker: RedisTaskBroker) -> None:
        self.broker = broker
        self.scheduler = AsyncIOScheduler()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        # Promote delayed tasks every 2 seconds
        self.scheduler.add_job(
            self.broker.promote_delayed,
            trigger="interval",
            seconds=2,
            id="promote_delayed",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        self._started = True
        logger.info("APScheduler started")

    def shutdown(self) -> None:
        if not self._started:
            return
        self.scheduler.shutdown(wait=False)
        self._started = False
        logger.info("APScheduler stopped")
