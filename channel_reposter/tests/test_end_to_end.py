"""
Сквозной тест: настоящий планировщик + настоящий движок + заглушка Telegram.

Именно то, на что жаловались: посты должны выходить порциями через
заданный интервал, сами, без нажатий в панели.
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

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
import poster as poster_module  # noqa: E402
import scheduling  # noqa: E402
from bridge import BRIDGE  # noqa: E402
from database import Database  # noqa: E402
from fake_client import SOURCE_ID, TARGET_ID, FakeClient, text_message  # noqa: E402
from poster import ChannelPoster  # noqa: E402


class EndToEndSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # В бою интервал не бывает меньше 5 секунд, в тесте — доли секунды
        self._min_interval = database_module.MIN_INTERVAL_SECONDS
        database_module.MIN_INTERVAL_SECONDS = 0.01
        self.addCleanup(
            setattr, database_module, "MIN_INTERVAL_SECONDS", self._min_interval
        )
        self.db = Database(Path(self.tmp.name) / "e2e.db")
        self.db.ensure_defaults(
            caption="",
            posts_per_cycle=2,
            source_channel=str(SOURCE_ID),
            target_channel=str(TARGET_ID),
            interval_seconds=0.3,
        )
        self.db.set_running(True)
        self.db.set_progress_id(0)
        self.db.run_asap()

        self.client = FakeClient([text_message(i, f"post {i}") for i in range(1, 11)])

        # Ускоряем всё, что в бою измеряется минутами
        self._saved = {
            "TICK": main_module.TICK,
            "START_DELAY": main_module.START_DELAY,
            "MIN_DELAY": scheduling.MIN_DELAY,
            "MIN_INTERVAL": scheduling.MIN_INTERVAL,
            "IDLE_STEPS": scheduling.IDLE_STEPS,
            "POST_DELAY_MIN": poster_module.config.POST_DELAY_MIN,
            "POST_DELAY_MAX": poster_module.config.POST_DELAY_MAX,
        }
        main_module.TICK = 0.02
        main_module.START_DELAY = 0.0
        scheduling.MIN_DELAY = 0.01
        scheduling.MIN_INTERVAL = 0.01
        scheduling.IDLE_STEPS = (0.05, 0.05, 0.05)
        poster_module.config.POST_DELAY_MIN = 0.0
        poster_module.config.POST_DELAY_MAX = 0.0
        self.addCleanup(self._restore)

        BRIDGE.db = self.db
        BRIDGE.auth = None
        BRIDGE.poster = ChannelPoster(client=self.client, db=self.db)  # type: ignore[arg-type]
        BRIDGE.notify_fn = self._collect
        self.notifications: list[str] = []
        self.addCleanup(self._reset_bridge)

    def _restore(self) -> None:
        main_module.TICK = self._saved["TICK"]
        main_module.START_DELAY = self._saved["START_DELAY"]
        scheduling.MIN_DELAY = self._saved["MIN_DELAY"]
        scheduling.MIN_INTERVAL = self._saved["MIN_INTERVAL"]
        scheduling.IDLE_STEPS = self._saved["IDLE_STEPS"]
        poster_module.config.POST_DELAY_MIN = self._saved["POST_DELAY_MIN"]
        poster_module.config.POST_DELAY_MAX = self._saved["POST_DELAY_MAX"]

    def _reset_bridge(self) -> None:
        BRIDGE.db = None
        BRIDGE.poster = None
        BRIDGE.notify_fn = None
        BRIDGE.admin_loop = None

    async def _collect(self, chat_id: int, text: str, **kwargs) -> None:
        self.notifications.append(text)

    async def _drive(self, seconds: float) -> None:
        BRIDGE.admin_loop = asyncio.get_running_loop()
        task = asyncio.create_task(main_module._worker_scheduler())
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def test_posts_keep_coming_batch_by_batch(self) -> None:
        asyncio.run(self._drive(1.2))

        published = [p["text"] for p in self.client.published]
        # 0.3 сек интервал × ~1.2 сек → минимум два цикла по 2 поста
        self.assertGreaterEqual(len(published), 4)
        self.assertEqual(published, [f"post {i}" for i in range(1, len(published) + 1)])
        self.assertEqual(self.db.get_progress_id(), len(published))
        self.assertEqual(self.db.history_count(), len(published))
        self.assertGreater(self.db.get_last_published_at(), 0)

    def test_interval_paces_publication(self) -> None:
        """Больше, чем «интервал×циклы», за отведённое время не публикуется."""
        self.db.set_interval_seconds(10)
        asyncio.run(self._drive(0.5))
        self.assertEqual(len(self.client.published), 2)
        left = self.db.get_next_run() - time.time()
        self.assertGreater(left, 8)

    def test_pause_stops_publication(self) -> None:
        async def scenario() -> None:
            BRIDGE.admin_loop = asyncio.get_running_loop()
            task = asyncio.create_task(main_module._worker_scheduler())
            await asyncio.sleep(0.3)
            self.db.set_running(False)
            count = len(self.client.published)
            await asyncio.sleep(0.4)
            self.assertEqual(len(self.client.published), count)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        asyncio.run(scenario())
        self.assertGreaterEqual(len(self.client.published), 2)

    def test_stops_at_tip_and_resumes_on_new_post(self) -> None:
        self.client.messages = {1: text_message(1, "post 1")}

        async def wait_for(check, timeout: float = 3.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if check():
                    return True
                await asyncio.sleep(0.01)
            return False

        async def scenario() -> None:
            BRIDGE.admin_loop = asyncio.get_running_loop()
            task = asyncio.create_task(main_module._worker_scheduler())
            self.assertTrue(await wait_for(lambda: len(self.client.published) == 1))
            # В источнике появился новый пост — бот должен его подхватить сам
            self.client.messages[2] = text_message(2, "post 2")
            self.assertTrue(await wait_for(lambda: len(self.client.published) == 2))
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        asyncio.run(scenario())
        self.assertEqual(
            [p["text"] for p in self.client.published], ["post 1", "post 2"]
        )


if __name__ == "__main__":
    unittest.main()
