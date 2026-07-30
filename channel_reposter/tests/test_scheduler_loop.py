"""
Тесты планировщика: он и есть «постинг через время».

Проверяем главное, из-за чего бот раньше молчал:
  • планировщик работает даже если юзербот вошёл позже;
  • пользовательский интервал соблюдается (а не подменяется на 10 секунд);
  • «▶️ Старт» запускает цикл сразу;
  • критическая ошибка ставит на паузу и уведомляет админа.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import main as main_module  # noqa: E402
from bridge import BRIDGE  # noqa: E402
from database import Database  # noqa: E402
from poster import (  # noqa: E402
    REASON_FATAL,
    REASON_UP_TO_DATE,
    CycleResult,
)


class FakePoster:
    """Движок-заглушка: считает вызовы и отдаёт заранее заданные итоги."""

    def __init__(self, results: list[CycleResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls = 0
        self.client = object()
        self._busy = False
        self.unlocked = 0

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def busy_seconds(self) -> float:
        return 0.0

    def force_unlock(self) -> None:
        self.unlocked += 1

    async def run_cycle(self, limit=None, *, force=False) -> CycleResult:
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return CycleResult(reason=REASON_UP_TO_DATE)


class SchedulerLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "sched.db")
        self.db.ensure_defaults(
            caption="",
            posts_per_cycle=2,
            source_channel="-1001",
            target_channel="-1002",
            interval_seconds=7200,
        )
        self.db.set_running(True)
        self.db.run_asap()

        self.notifications: list[str] = []

        BRIDGE.db = self.db
        BRIDGE.poster = None
        BRIDGE.auth = None
        BRIDGE.notify_fn = self._collect
        self.addCleanup(self._reset_bridge)

        self._tick = main_module.TICK
        self._delay = main_module.START_DELAY
        main_module.TICK = 0.01
        main_module.START_DELAY = 0.0
        self.addCleanup(self._reset_timing)

    def _reset_timing(self) -> None:
        main_module.TICK = self._tick
        main_module.START_DELAY = self._delay

    def _reset_bridge(self) -> None:
        BRIDGE.db = None
        BRIDGE.poster = None
        BRIDGE.auth = None
        BRIDGE.notify_fn = None
        BRIDGE.admin_loop = None

    async def _collect(self, chat_id: int, text: str, **kwargs) -> None:
        self.notifications.append(text)

    async def _run_loop(self, steps, timeout: float = 3.0) -> None:
        """Запустить планировщик, выполнить сценарий, остановить."""
        BRIDGE.admin_loop = asyncio.get_running_loop()
        task = asyncio.create_task(main_module._worker_scheduler())
        try:
            await asyncio.wait_for(steps(), timeout=timeout)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.sleep(0)

    async def _wait_for(self, check, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return True
            await asyncio.sleep(0.01)
        return False

    # ------------------------------------------------------------------ tests

    def test_waits_for_login_then_posts(self) -> None:
        """Регрессия: раньше планировщик не запускался, если входа не было."""
        poster = FakePoster([CycleResult(published=1, latest_id=10, progress_id=5)])

        async def steps() -> None:
            await asyncio.sleep(0.1)
            self.assertEqual(poster.calls, 0)  # юзербота ещё нет
            self.assertTrue(self.db.get_settings().is_running)
            BRIDGE.poster = poster  # «вход» уже во время работы бота
            self.assertTrue(await self._wait_for(lambda: poster.calls == 1))

        asyncio.run(self._run_loop(steps))
        self.assertEqual(poster.calls, 1)

    def test_interval_is_used_as_is(self) -> None:
        """Интервал 2 часа = 2 часа, а не «10 секунд для догона»."""
        poster = FakePoster([CycleResult(published=2, latest_id=10, progress_id=10)])
        BRIDGE.poster = poster

        async def steps() -> None:
            self.assertTrue(await self._wait_for(lambda: poster.calls == 1))
            self.assertTrue(await self._wait_for(lambda: self.db.get_next_run() > 0))

        asyncio.run(self._run_loop(steps))
        left = self.db.get_next_run() - time.time()
        self.assertGreater(left, 7100)
        self.assertLessEqual(left, 7201)
        self.assertEqual(self.db.get_next_run_reason(), "interval")
        self.assertEqual(poster.calls, 1)

    def test_catchup_uses_short_interval_while_backlog(self) -> None:
        poster = FakePoster([CycleResult(published=2, latest_id=500, progress_id=10)])
        BRIDGE.poster = poster
        self.db.set_catchup(True)
        self.db.set_catchup_seconds(30)

        async def steps() -> None:
            self.assertTrue(await self._wait_for(lambda: self.db.get_next_run() > 0))

        asyncio.run(self._run_loop(steps))
        left = self.db.get_next_run() - time.time()
        self.assertLessEqual(left, 31)
        self.assertGreater(left, 20)
        self.assertEqual(self.db.get_next_run_reason(), "catchup")

    def test_idle_polls_faster_than_interval(self) -> None:
        poster = FakePoster([CycleResult(reason=REASON_UP_TO_DATE, latest_id=5, progress_id=5)])
        BRIDGE.poster = poster

        async def steps() -> None:
            self.assertTrue(await self._wait_for(lambda: self.db.get_next_run() > 0))

        asyncio.run(self._run_loop(steps))
        left = self.db.get_next_run() - time.time()
        self.assertLessEqual(left, 16)
        self.assertEqual(self.db.get_next_run_reason(), "idle")

    def test_paused_bot_never_runs_cycles(self) -> None:
        poster = FakePoster()
        BRIDGE.poster = poster
        self.db.set_running(False)

        async def steps() -> None:
            await asyncio.sleep(0.2)

        asyncio.run(self._run_loop(steps))
        self.assertEqual(poster.calls, 0)

    def test_respects_future_next_run(self) -> None:
        poster = FakePoster()
        BRIDGE.poster = poster
        self.db.set_next_run(time.time() + 600, "interval")

        async def steps() -> None:
            await asyncio.sleep(0.2)

        asyncio.run(self._run_loop(steps))
        self.assertEqual(poster.calls, 0)

    def test_start_asap_overrides_pending_wait(self) -> None:
        poster = FakePoster()
        BRIDGE.poster = poster
        self.db.set_next_run(time.time() + 600, "interval")

        async def steps() -> None:
            await asyncio.sleep(0.1)
            self.assertEqual(poster.calls, 0)
            self.db.run_asap()  # это делает кнопка ▶️ Старт
            self.assertTrue(await self._wait_for(lambda: poster.calls >= 1))

        asyncio.run(self._run_loop(steps))

    def test_fatal_pauses_and_notifies(self) -> None:
        poster = FakePoster(
            [CycleResult(reason=REASON_FATAL, fatal_text="нет прав публикации")]
        )
        BRIDGE.poster = poster
        self.db.set("staging_chat_id", "777")

        async def steps() -> None:
            self.assertTrue(
                await self._wait_for(lambda: not self.db.get_settings().is_running)
            )
            self.assertTrue(await self._wait_for(lambda: bool(self.notifications)))

        asyncio.run(self._run_loop(steps))
        self.assertIn("остановлен", self.notifications[0])
        self.assertIn("нет прав", self.db.get_last_error()[0])

    def test_error_backoff_and_notification(self) -> None:
        poster = FakePoster(
            [CycleResult(reason="error", error="сеть: timeout")] * 3
        )
        BRIDGE.poster = poster
        self.db.set("staging_chat_id", "777")

        async def steps() -> None:
            self.assertTrue(await self._wait_for(lambda: bool(self.notifications)))

        asyncio.run(self._run_loop(steps))
        self.assertIn("не удался", self.notifications[0])
        left = self.db.get_next_run() - time.time()
        self.assertGreater(left, 25)
        self.assertEqual(self.db.get_next_run_reason(), "error")

    def test_flood_wait_schedules_after_pause(self) -> None:
        poster = FakePoster([CycleResult(reason="flood", flood_seconds=120)])
        BRIDGE.poster = poster
        self.db.set("staging_chat_id", "777")

        async def steps() -> None:
            self.assertTrue(await self._wait_for(lambda: self.db.get_next_run() > 0))

        asyncio.run(self._run_loop(steps))
        left = self.db.get_next_run() - time.time()
        self.assertGreater(left, 120)
        self.assertEqual(self.db.get_next_run_reason(), "flood")

    def test_scheduler_heartbeat_is_recorded(self) -> None:
        poster = FakePoster()
        BRIDGE.poster = poster

        async def steps() -> None:
            self.assertTrue(await self._wait_for(lambda: self.db.scheduler_age() is not None))

        asyncio.run(self._run_loop(steps))
        age = self.db.scheduler_age()
        self.assertIsNotNone(age)
        self.assertLess(float(age or 0.0), 5.0)

    def test_cycle_reports_when_enabled(self) -> None:
        poster = FakePoster([CycleResult(published=3, latest_id=20, progress_id=10)])
        BRIDGE.poster = poster
        self.db.set_notify_cycles(True)
        self.db.set("staging_chat_id", "777")

        async def steps() -> None:
            self.assertTrue(await self._wait_for(lambda: bool(self.notifications)))

        asyncio.run(self._run_loop(steps))
        self.assertIn("Опубликовано", self.notifications[0])


if __name__ == "__main__":
    unittest.main()
