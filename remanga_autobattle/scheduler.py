"""
scheduler.py — фоновые задачи MangaBuff Autopilot.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

JOB_MANGABUFF_READ = "mangabuff_autoread"
JOB_MANGABUFF_MARKET = "mangabuff_market_maintain"


class AppScheduler:
    def __init__(self, timezone: str = "Europe/Moscow") -> None:
        self._scheduler = AsyncIOScheduler(timezone=timezone)

    @property
    def raw(self) -> AsyncIOScheduler:
        return self._scheduler

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("AppScheduler запущен")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("AppScheduler остановлен")

    def is_job_running(self, job_id: str) -> bool:
        return self._scheduler.get_job(job_id) is not None

    def remove_job(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
            logger.info("Задача снята: %s", job_id)
