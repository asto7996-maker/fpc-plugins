"""
scheduler.py — централизованный планировщик фоновых задач Remanga + MangaBuff.

Один AsyncIOScheduler на всё приложение. Задачи регистрируются по стабильным id.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# ID задач
JOB_REMANGA_AUTOBATTLE = "remanga_autobattle"
JOB_MANGABUFF_READ = "mangabuff_autoread"


class AppScheduler:
    """Обёртка над APScheduler для двух модулей."""

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

    def set_interval_job(
        self,
        job_id: str,
        func: Callable[..., Coroutine[Any, Any, Any]],
        seconds: int,
        *,
        replace: bool = True,
    ) -> None:
        """Периодическая async-задача (Remanga автобой)."""
        self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=max(5, int(seconds))),
            id=job_id,
            replace_existing=replace,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Задача %s: каждые %s сек", job_id, seconds)

    def set_oneshot_async(
        self,
        job_id: str,
        func: Callable[..., Coroutine[Any, Any, Any]],
        *,
        replace: bool = True,
    ) -> None:
        """
        Запустить долгую async-корутину как «фоновую задачу» планировщика.

        Для MangaBuff авточтения: одна долгоживущая корутина, не интервал.
        """
        # date trigger «сейчас» — APScheduler выполнит сразу
        from datetime import datetime, timedelta

        run_date = datetime.now() + timedelta(seconds=1)
        self._scheduler.add_job(
            func,
            trigger="date",
            run_date=run_date,
            id=job_id,
            replace_existing=replace,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Oneshot-задача поставлена: %s", job_id)
