"""
bridge.py — юзербот в отдельном потоке, админка aiogram не блокируется.

Все долгие операции Pyrogram (цикл, rewrite, FloodWait) идут в worker-loop.
Админ-бот остаётся на главном asyncio-loop и всегда отвечает на команды.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class WorkerBridge:
    """Фоновый поток с собственным asyncio event loop для Pyrogram."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._stopped = False

        # Объекты, живущие только в worker-потоке
        self.auth = None
        self.poster = None
        self.db = None

        # Для уведомлений обратно в aiogram
        self.admin_loop: Optional[asyncio.AbstractEventLoop] = None
        self.notify_fn: Optional[Callable[..., Any]] = None

    @property
    def is_running(self) -> bool:
        return self._loop is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._ready.clear()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name="pyrogram-worker",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("Worker thread failed to start")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        logger.info("Pyrogram worker loop started")
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                logger.exception("worker shutdown")
            loop.close()
            self._loop = None
            logger.info("Pyrogram worker loop stopped")

    def stop(self) -> None:
        self._stopped = True
        if self._loop is None:
            return

        async def _shutdown() -> None:
            if self.auth is not None:
                try:
                    await self.auth.stop()
                except Exception:
                    logger.exception("auth.stop")

        try:
            fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            fut.result(timeout=30)
        except Exception:
            logger.exception("worker stop")

        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=15)

    def submit(self, coro) -> Future:
        """Запустить корутину в worker-loop (возвращает concurrent Future)."""
        if self._loop is None:
            raise RuntimeError("Worker не запущен")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def call(self, coro, timeout: Optional[float] = 120.0) -> Any:
        """Await результата worker-корутины из admin-loop (с таймаутом)."""
        fut = self.submit(coro)
        wrapped = asyncio.wrap_future(fut)
        if timeout is None:
            return await wrapped
        try:
            return await asyncio.wait_for(wrapped, timeout=timeout)
        except asyncio.TimeoutError:
            fut.cancel()
            raise TimeoutError(f"Worker timeout after {timeout}s")

    def notify(self, chat_id: int, text: str, **kwargs) -> None:
        """Отправить сообщение админу через aiogram (thread-safe)."""
        if self.admin_loop is None or self.notify_fn is None:
            logger.warning("notify skipped: admin bot not bound")
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.notify_fn(chat_id, text, **kwargs),
                self.admin_loop,
            )
        except Exception:
            logger.exception("notify failed")


BRIDGE = WorkerBridge()
