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
from links import normalize_channel, parse_post_link, to_channel_chat_id  # noqa: E402
from poster import _attach_caption, _is_unsupported_media  # noqa: E402
from pyrogram.types import InputMediaPhoto  # noqa: E402
from pyrogram import enums  # noqa: E402


class CaptionAttachTests(unittest.TestCase):
    def test_attach_caption_sets_html(self) -> None:
        item = InputMediaPhoto(media="file_id_x")
        _attach_caption(item, "<b>hi</b>")
        self.assertEqual(item.caption, "<b>hi</b>")
        self.assertEqual(item.parse_mode, enums.ParseMode.HTML)

    def test_attach_caption_empty_noop(self) -> None:
        item = InputMediaPhoto(media="file_id_x", caption="keep")
        _attach_caption(item, "")
        self.assertEqual(item.caption, "keep")


class UnsupportedMediaHeuristicTests(unittest.TestCase):
    def test_text_not_unsupported(self) -> None:
        class M:
            empty = False
            service = None
            photo = None
            video = None
            document = None
            audio = None
            animation = None
            voice = None
            video_note = None
            sticker = None
            poll = None
            dice = None
            text = "hello"
            caption = None

        self.assertFalse(_is_unsupported_media(M()))  # type: ignore[arg-type]

    def test_empty_media_album_item_is_unsupported(self) -> None:
        class M:
            empty = False
            service = None
            photo = None
            video = None
            document = None
            audio = None
            animation = None
            voice = None
            video_note = None
            sticker = None
            poll = None
            dice = None
            text = None
            caption = None

        self.assertTrue(_is_unsupported_media(M()))  # type: ignore[arg-type]


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


class NormalizeChannelTests(unittest.TestCase):
    def test_username_variants(self) -> None:
        self.assertEqual(normalize_channel("@sw3atsh0p"), "@sw3atsh0p")
        self.assertEqual(normalize_channel("sw3atsh0p"), "@sw3atsh0p")
        self.assertEqual(normalize_channel("https://t.me/sw3atsh0p"), "@sw3atsh0p")
        self.assertEqual(
            normalize_channel("@https://t.me/sw3atsh0p"), "@sw3atsh0p"
        )
        self.assertEqual(
            normalize_channel("https://t.me/porno_sliv_altushek_v_tg/6185"),
            "@porno_sliv_altushek_v_tg",
        )
        self.assertEqual(
            normalize_channel("@https://t.me/porno_sliv_altushek_v_tg"),
            "@porno_sliv_altushek_v_tg",
        )

    def test_numeric_id(self) -> None:
        # Короткий id закрытого канала (из t.me/c/<id>/…) → полный chat_id
        self.assertEqual(normalize_channel("35839961"), "-10035839961")
        self.assertEqual(normalize_channel(35839961), "-10035839961")
        self.assertEqual(normalize_channel("-10035839961"), "-10035839961")
        self.assertEqual(normalize_channel("-100123"), "-100123")
        self.assertEqual(to_channel_chat_id("35839961"), "-10035839961")
        self.assertEqual(to_channel_chat_id(-10035839961), "-10035839961")

    def test_private_channel_url(self) -> None:
        self.assertEqual(
            normalize_channel("https://t.me/c/35839961"), "-10035839961"
        )
        self.assertEqual(
            normalize_channel("https://t.me/c/35839961/500"), "-10035839961"
        )
        self.assertEqual(
            normalize_channel("t.me/c/35839961/12?single"), "-10035839961"
        )

    def test_db_setters_normalize(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(Path(tmp.name) / "n.db")
        db.ensure_defaults(
            caption="x",
            interval_hours=1,
            posts_per_cycle=1,
            source_channel="@src",
            target_channel="@dst",
        )
        db.set_source_channel("@https://t.me/porno_sliv_altushek_v_tg")
        db.set_target_channel("https://t.me/sw3atsh0p")
        s = db.get_settings()
        self.assertEqual(s.source_channel, "@porno_sliv_altushek_v_tg")
        self.assertEqual(s.target_channel, "@sw3atsh0p")

        db.set_source_channel("35839961")
        db.set_target_channel("https://t.me/c/111222333")
        s = db.get_settings()
        self.assertEqual(s.source_channel, "-10035839961")
        self.assertEqual(s.target_channel, "-100111222333")


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

    def test_max_ok_source_id(self) -> None:
        self.assertEqual(self.db.max_ok_source_id(), 0)
        self.db.add_history(10, target_message_id=1, status="ok")
        self.db.add_history(25, target_message_id=2, status="ok")
        self.db.add_history(30, status="error", error="x")
        self.assertEqual(self.db.max_ok_source_id(), 25)

    def test_clear_history(self) -> None:
        self.db.add_history(1, target_message_id=1, status="ok")
        self.db.add_history(2, status="error", error="e")
        self.assertEqual(self.db.clear_history(), 2)
        self.assertEqual(self.db.history_count(), 0)
        self.assertEqual(self.db.max_ok_source_id(), 0)


if __name__ == "__main__":
    unittest.main()
