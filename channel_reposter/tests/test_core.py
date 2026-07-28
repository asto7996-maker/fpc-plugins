"""
Небольшие unit-тесты без сети: разбор ссылок и SQLite.
Запуск: python -m pytest tests/ -q
       или: python tests/test_core.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Корень проекта в PYTHONPATH
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402
from links import parse_post_link  # noqa: E402


class ParseLinkTests(unittest.TestCase):
    def test_private_link(self) -> None:
        chat, mid = parse_post_link("https://t.me/c/123456789/500")
        self.assertEqual(chat, -100123456789)
        self.assertEqual(mid, 500)

    def test_public_link(self) -> None:
        chat, mid = parse_post_link("https://t.me/channel_username/500")
        self.assertEqual(chat, "channel_username")
        self.assertEqual(mid, 500)

    def test_public_link_with_query(self) -> None:
        chat, mid = parse_post_link("t.me/my_channel/42?single")
        self.assertEqual(chat, "my_channel")
        self.assertEqual(mid, 42)

    def test_invalid(self) -> None:
        with self.assertRaises(ValueError):
            parse_post_link("https://example.com/foo")


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.ensure_defaults(
            caption="<b>hi</b>",
            interval_hours=6,
            posts_per_cycle=5,
            source_channel="@src",
            target_channel="@dst",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_defaults(self) -> None:
        s = self.db.get_settings()
        self.assertEqual(s.caption_template, "<b>hi</b>")
        self.assertEqual(s.interval_hours, 6.0)
        self.assertEqual(s.posts_per_cycle, 5)
        self.assertFalse(s.is_running)
        self.assertEqual(s.progress_id, 0)

    def test_progress_and_history(self) -> None:
        self.db.set_progress_id(100)
        self.assertEqual(self.db.get_progress_id(), 100)
        self.db.add_history(100, target_message_id=1, status="ok")
        self.assertTrue(self.db.was_processed(100))
        self.assertFalse(self.db.was_processed(101))
        self.assertEqual(self.db.history_count(), 1)

    def test_settings_update(self) -> None:
        self.db.set_caption('<a href="https://t.me/x">x</a>')
        self.db.set_interval_hours(0.5)
        self.db.set_posts_per_cycle(3)
        self.db.set_running(True)
        s = self.db.get_settings()
        self.assertIn("href=", s.caption_template)
        self.assertEqual(s.interval_hours, 0.5)
        self.assertEqual(s.posts_per_cycle, 3)
        self.assertTrue(s.is_running)


if __name__ == "__main__":
    unittest.main()
