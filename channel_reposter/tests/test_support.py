"""Тарифы магазина, фильтр личек, проверка чека на Photoshop."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from receipt_forensics import inspect_receipt_bytes  # noqa: E402
from shop_catalog import (  # noqa: E402
    match_tariff,
    parse_period_label,
    shop_title_plain,
    short_name_for,
    tariffs_from_inline_rows,
)
from support_inbox import should_handle_top  # noqa: E402


class ShopTitleTests(unittest.TestCase):
    def test_strips_fancy_unicode(self) -> None:
        raw = "👑 ˗ˏˋ 𝑽\u200a𝑰\u200a𝑷 ˎˊ˗"
        plain = shop_title_plain(raw)
        self.assertIn("VIP", plain)

    def test_short_names_known_ids(self) -> None:
        self.assertEqual(short_name_for("19063", "x"), "VIP ALL-IN")
        self.assertEqual(short_name_for("19066", "x"), "OnlyFans Альтушки")

    def test_parse_buttons(self) -> None:
        rows = [
            [{"text": "VIP ALL-IN", "data": "resource/view?id=19063"}],
            [{"text": "Закрыть", "data": "system/reset"}],
        ]
        tariffs = tariffs_from_inline_rows(rows)
        self.assertEqual(len(tariffs), 1)
        self.assertEqual(tariffs[0].shop_id, "19063")
        hit = match_tariff("VIP ALL-IN", tariffs)
        self.assertIsNotNone(hit)

    def test_period_label(self) -> None:
        p = parse_period_label("30 дней — 1 490₽")
        assert p is not None
        self.assertEqual(p.days, 30)
        self.assertEqual(p.price, 1490.0)


class InboxFilterTests(unittest.TestCase):
    def test_seed_old_chats(self) -> None:
        go, why = should_handle_top(
            has_session=False,
            last_handled_id=0,
            top_id=10,
            top_outgoing=False,
            unread=0,
            top_age_hours=100,
            catchup_hours=36,
        )
        self.assertFalse(go)
        self.assertEqual(why, "seed-old")

    def test_recent_unread(self) -> None:
        go, why = should_handle_top(
            has_session=False,
            last_handled_id=0,
            top_id=10,
            top_outgoing=False,
            unread=2,
            top_age_hours=100,
            catchup_hours=36,
        )
        self.assertTrue(go)
        self.assertEqual(why, "new-recent")

    def test_skip_our_outgoing(self) -> None:
        go, why = should_handle_top(
            has_session=True,
            last_handled_id=5,
            top_id=11,
            top_outgoing=True,
            unread=0,
            top_age_hours=1,
            catchup_hours=36,
        )
        self.assertFalse(go)
        self.assertEqual(why, "ours")


class ForensicsTests(unittest.TestCase):
    def test_tiny_rejected(self) -> None:
        r = inspect_receipt_bytes(b"12345")
        self.assertFalse(r.ok)

    def test_photoshop_in_bytes(self) -> None:
        payload = b"\xff\xd8\xff" + b"x" * 9000 + b"Adobe Photoshop 24.0"
        r = inspect_receipt_bytes(payload, filename="check.jpg")
        self.assertFalse(r.ok)
        self.assertTrue("редактор" in r.reason.lower() or "photoshop" in r.reason.lower())

    def test_clean_jpeg(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("pillow")
        img = Image.new("RGB", (900, 700), (245, 245, 245))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        r = inspect_receipt_bytes(buf.getvalue(), filename="bank.jpg")
        self.assertTrue(r.ok, r.reason)

    def test_exif_photoshop(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("pillow")
        img = Image.new("RGB", (900, 700), (245, 245, 245))
        exif = img.getexif()
        exif[305] = "Adobe Photoshop 24.0 (Windows)"
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, exif=exif)
        r = inspect_receipt_bytes(buf.getvalue())
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
