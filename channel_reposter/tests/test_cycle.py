"""
Тесты цикла публикации на клиенте-заглушке (без сети).
Запуск: python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pyrogram.errors import ChannelPrivate, ChatWriteForbidden, FloodWait  # noqa: E402

import poster as poster_module  # noqa: E402
from database import Database  # noqa: E402
from fake_client import (  # noqa: E402
    SOURCE_ID,
    TARGET_ID,
    FakeClient,
    photo_message,
    sticker_message,
    text_message,
)
from poster import (  # noqa: E402
    REASON_FATAL,
    REASON_FLOOD,
    REASON_PAUSED,
    REASON_UP_TO_DATE,
    ChannelPoster,
    _chat_ref,
)


class CycleTestCase(unittest.TestCase):
    """Общая обвязка: временная БД + заглушка Telegram."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "cycle.db")
        self.db.ensure_defaults(
            caption="",
            posts_per_cycle=2,
            source_channel=str(SOURCE_ID),
            target_channel=str(TARGET_ID),
            interval_seconds=60,
        )
        self.db.set_running(True)
        self.db.set_progress_id(0)
        # Пауза между публикациями внутри цикла не нужна в тестах
        self._delay_min = poster_module.config.POST_DELAY_MIN
        self._delay_max = poster_module.config.POST_DELAY_MAX
        poster_module.config.POST_DELAY_MIN = 0.0
        poster_module.config.POST_DELAY_MAX = 0.0

    def tearDown(self) -> None:
        poster_module.config.POST_DELAY_MIN = self._delay_min
        poster_module.config.POST_DELAY_MAX = self._delay_max

    def run_cycle(self, client: FakeClient, **kwargs):
        poster = ChannelPoster(client=client, db=self.db)  # type: ignore[arg-type]
        return asyncio.run(poster.run_cycle(**kwargs)), poster


class BasicCycleTests(CycleTestCase):
    def test_publishes_up_to_limit_oldest_first(self) -> None:
        client = FakeClient([text_message(i, f"post {i}") for i in range(1, 6)])
        result, _ = self.run_cycle(client)

        self.assertEqual(result.published, 2)
        self.assertEqual([p["text"] for p in client.published], ["post 1", "post 2"])
        self.assertEqual(self.db.get_progress_id(), 2)
        self.assertEqual(result.latest_id, 5)
        self.assertEqual(result.backlog, 3)

    def test_second_cycle_continues(self) -> None:
        client = FakeClient([text_message(i, f"post {i}") for i in range(1, 6)])
        self.run_cycle(client)
        result, _ = self.run_cycle(client)
        self.assertEqual(result.published, 2)
        self.assertEqual(self.db.get_progress_id(), 4)
        self.assertEqual([p["text"] for p in client.published][-2:], ["post 3", "post 4"])

    def test_up_to_date(self) -> None:
        client = FakeClient([text_message(1)])
        self.db.set_progress_id(1)
        result, _ = self.run_cycle(client)
        self.assertEqual(result.published, 0)
        self.assertEqual(result.reason, REASON_UP_TO_DATE)

    def test_paused_does_nothing(self) -> None:
        client = FakeClient([text_message(1)])
        self.db.set_running(False)
        result, _ = self.run_cycle(client)
        self.assertEqual(result.reason, REASON_PAUSED)
        self.assertEqual(client.published, [])

    def test_force_publishes_on_pause(self) -> None:
        client = FakeClient([text_message(1)])
        self.db.set_running(False)
        result, _ = self.run_cycle(client, force=True)
        self.assertEqual(result.published, 1)
        self.assertFalse(self.db.get_settings().is_running)

    def test_limit_argument_overrides_settings(self) -> None:
        client = FakeClient([text_message(i) for i in range(1, 6)])
        result, _ = self.run_cycle(client, limit=1)
        self.assertEqual(result.published, 1)
        self.assertEqual(self.db.get_settings().posts_per_cycle, 2)

    def test_history_prevents_duplicates(self) -> None:
        client = FakeClient([text_message(1), text_message(2)])
        self.db.add_history(1, target_message_id=7, status="ok")
        result, _ = self.run_cycle(client, limit=2)
        self.assertEqual(result.published, 1)
        self.assertEqual(len(client.published), 1)
        self.assertEqual(self.db.get_progress_id(), 2)

    def test_caption_template_applied(self) -> None:
        self.db.set_caption("<b>шаблон</b>")
        client = FakeClient([photo_message(1)])
        result, _ = self.run_cycle(client, limit=1)
        self.assertEqual(result.published, 1)
        self.assertEqual(client.published[0]["caption"], "<b>шаблон</b>")

    def test_text_post_keeps_its_text_with_template(self) -> None:
        """Шаблон дописывается к тексту, а не затирает пост."""
        self.db.set_caption("<b>подпись</b>")
        client = FakeClient([text_message(1, "важный текст")])
        result, _ = self.run_cycle(client, limit=1)
        self.assertEqual(result.published, 1)
        self.assertEqual(
            client.published[0]["text"], "важный текст\n\n<b>подпись</b>"
        )

    def test_text_post_html_is_escaped(self) -> None:
        client = FakeClient([text_message(1, "5 < 7 & 8 > 2")])
        self.run_cycle(client, limit=1)
        self.assertEqual(client.published[0]["text"], "5 &lt; 7 &amp; 8 &gt; 2")

    def test_sticker_is_copied(self) -> None:
        """Стикеры не теряются, хотя подписи у них не бывает."""
        self.db.set_caption("<b>подпись</b>")
        client = FakeClient([sticker_message(1), text_message(2, "далее")])
        result, _ = self.run_cycle(client, limit=2)

        self.assertEqual(result.published, 2)
        self.assertEqual(client.published[0]["kind"], "sticker")
        self.assertIsNone(client.published[0]["caption"])
        self.assertEqual(self.db.get_progress_id(), 2)

    def test_template_not_duplicated(self) -> None:
        self.db.set_caption("подпись")
        client = FakeClient([text_message(1, "текст и подпись")])
        self.run_cycle(client, limit=1)
        self.assertEqual(client.published[0]["text"], "текст и подпись")


class GapTests(CycleTestCase):
    def test_gaps_do_not_cost_requests(self) -> None:
        """Удалённые посты не должны перебираться по одному ID."""
        client = FakeClient([text_message(1), text_message(500), text_message(1000)])
        result, _ = self.run_cycle(client, limit=3)

        self.assertEqual(result.published, 3)
        self.assertEqual(self.db.get_progress_id(), 1000)
        # 3 поста = 3 обращения get_messages (+0 на дыры)
        self.assertLessEqual(client.get_message_calls, 4)

    def test_falls_back_to_scan_when_window_unsupported(self) -> None:
        client = FakeClient([text_message(1), text_message(4)])
        client.window_broken = True
        result, poster = self.run_cycle(client, limit=2)

        self.assertEqual(result.published, 2)
        self.assertFalse(poster._history_window_ok)
        self.assertEqual(self.db.get_progress_id(), 4)

    def test_falls_back_when_window_skips_posts(self) -> None:
        """Если окно истории «перескочило» посты — только честный перебор."""
        client = FakeClient([text_message(i) for i in range(1, 6)])
        client.window_skips = 2
        result, poster = self.run_cycle(client, limit=3)

        self.assertFalse(poster._history_window_ok)
        self.assertEqual(result.published, 3)
        self.assertEqual([p["text"] for p in client.published], ["post", "post", "post"])
        self.assertEqual(self.db.get_progress_id(), 3)

    def test_tip_reached_without_moving_progress_forward(self) -> None:
        client = FakeClient([text_message(10)])
        self.db.set_progress_id(9)
        result, _ = self.run_cycle(client, limit=5)
        self.assertEqual(result.published, 1)
        self.assertEqual(self.db.get_progress_id(), 10)

    def test_progress_ahead_of_channel_rewinds(self) -> None:
        client = FakeClient([text_message(1), text_message(2)])
        self.db.add_history(2, target_message_id=5, status="ok")
        self.db.set_progress_id(99)
        result, _ = self.run_cycle(client, limit=2)
        self.assertEqual(self.db.get_progress_id(), 2)
        self.assertEqual(result.reason, REASON_UP_TO_DATE)


class AlbumTests(CycleTestCase):
    def test_album_counts_as_one_publication(self) -> None:
        album = [photo_message(i, group="g1") for i in (10, 11, 12)]
        client = FakeClient([text_message(9, "before"), *album, text_message(13, "after")])
        result, _ = self.run_cycle(client, limit=2)

        self.assertEqual(result.published, 2)  # текст + альбом
        kinds = [p["kind"] for p in client.published]
        self.assertEqual(kinds, ["text", "album"])
        self.assertEqual(self.db.get_progress_id(), 12)
        self.assertTrue(self.db.was_group_processed("g1"))
        for mid in (10, 11, 12):
            self.assertTrue(self.db.was_processed(mid))

    def test_album_with_caption_uses_media_group(self) -> None:
        self.db.set_caption("<i>подпись</i>")
        client = FakeClient([photo_message(i, group="g2") for i in (1, 2)])
        result, _ = self.run_cycle(client, limit=1)
        self.assertEqual(result.published, 1)
        self.assertEqual(client.published[0]["kind"], "album")
        self.assertEqual(client.published[0]["caption"], "<i>подпись</i>")

    def test_known_album_is_skipped(self) -> None:
        album = [photo_message(i, group="g3") for i in (5, 6)]
        client = FakeClient([*album, text_message(7, "next")])
        self.db.add_history(5, target_message_id=1, grouped_id="g3", status="ok")
        result, _ = self.run_cycle(client, limit=2)

        self.assertEqual(result.published, 1)
        self.assertEqual(client.published[0]["kind"], "text")
        self.assertEqual(self.db.get_progress_id(), 7)


class FailureTests(CycleTestCase):
    def test_write_forbidden_is_fatal(self) -> None:
        client = FakeClient([text_message(1)])
        client.fail_next = ChatWriteForbidden()
        result, _ = self.run_cycle(client, limit=1)

        self.assertEqual(result.reason, REASON_FATAL)
        self.assertTrue(result.fatal)
        self.assertIn("прав", result.fatal_text)
        self.assertEqual(self.db.get_progress_id(), 0)

    def test_long_flood_is_reported_not_slept(self) -> None:
        client = FakeClient([text_message(1)])
        client.fail_next = FloodWait(value=900)
        result, _ = self.run_cycle(client, limit=1)

        self.assertEqual(result.reason, REASON_FLOOD)
        self.assertEqual(result.flood_seconds, 900.0)
        self.assertEqual(client.published, [])

    def test_flood_budget_hands_cycle_back(self) -> None:
        """Череда коротких flood не должна держать цикл вечно."""
        client = FakeClient([text_message(i) for i in range(1, 6)])
        client.fail_always = FloodWait(value=1)
        original = poster_module.FLOOD_BUDGET
        poster_module.FLOOD_BUDGET = 1
        self.addCleanup(setattr, poster_module, "FLOOD_BUDGET", original)

        result, _ = self.run_cycle(client, limit=5)
        self.assertEqual(result.reason, REASON_FLOOD)
        self.assertEqual(result.published, 0)

    def test_short_flood_retries_same_post(self) -> None:
        client = FakeClient([text_message(1, "post 1")])
        client.fail_next = FloodWait(value=0)
        result, _ = self.run_cycle(client, limit=1)

        self.assertEqual(result.published, 1)
        self.assertEqual(client.published[0]["text"], "post 1")

    def test_network_error_asks_for_reconnect(self) -> None:
        client = FakeClient([text_message(1)])
        client.fail_always = OSError("connection reset")
        original = poster_module.NETWORK_RETRIES
        poster_module.NETWORK_RETRIES = 0
        self.addCleanup(setattr, poster_module, "NETWORK_RETRIES", original)
        result, _ = self.run_cycle(client, limit=1)

        self.assertTrue(result.needs_reconnect)
        self.assertIn("сеть", result.error)
        self.assertEqual(self.db.get_progress_id(), 0)

    def test_network_error_retried_once(self) -> None:
        """Разовый обрыв не должен ронять цикл."""
        client = FakeClient([text_message(1, "post 1")])
        client.fail_next = OSError("temporary")
        original = poster_module.NETWORK_RETRIES
        poster_module.NETWORK_RETRIES = 1
        self.addCleanup(setattr, poster_module, "NETWORK_RETRIES", original)
        result, _ = self.run_cycle(client, limit=1)

        self.assertEqual(result.published, 1)
        self.assertFalse(result.needs_reconnect)

    def test_empty_source_reported(self) -> None:
        client = FakeClient([])
        result, _ = self.run_cycle(client, limit=1)
        self.assertEqual(result.published, 0)
        self.assertIn("источник", result.error)

    def test_unexpected_error_becomes_result(self) -> None:
        """Любая неожиданная ошибка — это причина в отчёте, а не молчание."""
        from database import SETTING_SOURCE_CHANNEL

        self.db.set(SETTING_SOURCE_CHANNEL, "")
        original = poster_module.config.SOURCE_CHANNEL
        poster_module.config.SOURCE_CHANNEL = ""
        self.addCleanup(setattr, poster_module.config, "SOURCE_CHANNEL", original)

        client = FakeClient([text_message(1)])
        result, _ = self.run_cycle(client, limit=1)
        self.assertEqual(result.reason, "error")
        self.assertTrue(result.error)
        self.assertEqual(client.published, [])

    def test_target_channel_private_is_fatal_keeps_progress(self) -> None:
        """Закрытое назначение: не «прожигаем» очередь как up_to_date."""
        client = FakeClient([text_message(1, "post 1"), text_message(2, "post 2")])
        client.publish_fail = ChannelPrivate()
        result, _ = self.run_cycle(client, limit=2)

        self.assertEqual(result.reason, REASON_FATAL)
        self.assertIn("закрытый", result.fatal_text.lower())
        self.assertEqual(result.published, 0)
        self.assertEqual(self.db.get_progress_id(), 0)
        self.assertEqual(client.published, [])

    def test_resolve_fail_is_fatal(self) -> None:
        """Холодная сессия / нет access_hash — явная фатальная ошибка."""
        client = FakeClient([text_message(1)])
        client.resolve_fail = ChannelPrivate("CHANNEL_PRIVATE")
        poster_module._dialogs_warmed_clients.clear()
        result, _ = self.run_cycle(client, limit=1)

        self.assertEqual(result.reason, REASON_FATAL)
        self.assertIn("закрытый", result.fatal_text.lower())
        self.assertIn("подпис", result.fatal_text.lower())
        self.assertEqual(client.published, [])

    def test_short_private_id_normalized_in_chat_ref(self) -> None:
        self.assertEqual(_chat_ref("35839961"), -10035839961)
        self.assertEqual(_chat_ref(35839961), -10035839961)
        self.assertEqual(_chat_ref("-10035839961"), -10035839961)
        self.assertEqual(_chat_ref("10035839961"), -10035839961)


if __name__ == "__main__":
    unittest.main()
