"""
APScheduler-обёртка для async-задач плагинов.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("starvell.scheduler")


class TaskScheduler:
    """AsyncIO планировщик на базе APScheduler."""

    def __init__(self) -> None:
        self._scheduler = None

    def start(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            self._scheduler = AsyncIOScheduler()
            self._scheduler.start()
            logger.info("TaskScheduler запущен")
        except ImportError:
            logger.warning("APScheduler не установлен — задачи по таймеру недоступны")

    def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def add_interval_job(
        self,
        job_id: str,
        func: Callable,
        seconds: float,
        **kwargs: Any,
    ) -> None:
        if not self._scheduler:
            return
        self._scheduler.add_job(
            func,
            "interval",
            seconds=seconds,
            id=job_id,
            replace_existing=True,
            **kwargs,
        )

    def remove_job(self, job_id: str) -> None:
        if self._scheduler:
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass
