import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram_content_machine_bot import (
    SQLiteStore,
    Settings,
    detect_content_type,
    format_template,
    safe_truncate,
)


class SettingsTests(unittest.TestCase):
    def test_settings_loads_env_file_and_decodes_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "BOT_TOKEN=123:abc",
                        "TARGET_CHANNEL_ID=@target",
                        "ADMIN_IDS=100,200",
                        "SOURCE_CHANNELS=@SourceOne,https://t.me/source_two,-100123",
                        "AD_TEXT=hello\\nworld",
                        "TEXT_TEMPLATE={text}\\n\\n{ad_text}",
                        "CAPTION_TEMPLATE={caption}\\n\\n{ad_text}",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env(env_file)

            self.assertEqual(settings.bot_token, "123:abc")
            self.assertEqual(settings.target_channel_id, "@target")
            self.assertEqual(settings.admin_ids, (100, 200))
            self.assertEqual(settings.source_channel_refs, ("sourceone", "source_two", "-100123"))
            self.assertEqual(settings.ad_text, "hello\nworld")
            self.assertEqual(settings.text_template, "{text}\n\n{ad_text}")

    def test_settings_requires_core_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                Settings.from_env(None)

        message = str(ctx.exception)
        self.assertIn("BOT_TOKEN is required", message)
        self.assertIn("TARGET_CHANNEL_ID is required", message)
        self.assertIn("ADMIN_IDS", message)
        self.assertIn("SOURCE_CHANNELS", message)


class StoreTests(unittest.TestCase):
    def test_post_reservation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "db.sqlite3", logger=_fake_logger())
            message = _fake_message(chat_id=-1001, message_id=10, username="source")

            first = store.reserve_post(message, "photo", "@target")
            second = store.reserve_post(message, "photo", "@target")

            self.assertTrue(first)
            self.assertFalse(second)

            store.mark_post_sent("-1001", 10, 777)
            stats = store.stats()
            self.assertEqual(stats["posts"]["sent"], 1)

    def test_subscribe_unsubscribe_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStore(Path(tmp) / "db.sqlite3", logger=_fake_logger())
            user = SimpleNamespace(id=123, username="user", first_name="First", last_name="Last")

            store.upsert_subscriber(user)
            self.assertEqual(store.active_subscribers(), [123])

            store.unsubscribe(123)
            self.assertEqual(store.active_subscribers(), [])
            stats = store.stats()
            self.assertEqual(stats["subscribers"]["unsubscribed"], 1)


class HelperTests(unittest.TestCase):
    def test_format_template_ignores_missing_values(self) -> None:
        rendered = format_template("{text}|{missing}|{ad_text}", text="A", ad_text="B")
        self.assertEqual(rendered, "A||B")

    def test_safe_truncate_adds_suffix(self) -> None:
        self.assertEqual(safe_truncate("abcdef", 4), "a...")
        self.assertEqual(safe_truncate("abc", 4), "abc")

    def test_detect_content_type_prefers_text(self) -> None:
        message = SimpleNamespace(text="hello", photo=None, video=None, animation=None, document=None, audio=None, voice=None, video_note=None)
        self.assertEqual(detect_content_type(message), "text")


def _fake_message(chat_id: int, message_id: int, username: str = "source") -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, username=username, title="Source"),
    )


def _fake_logger() -> SimpleNamespace:
    return SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )


if __name__ == "__main__":
    unittest.main()
