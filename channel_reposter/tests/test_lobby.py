"""
Тесты лобби компенсации: срок, цена, чек, рассылка по диалогам, БД.
Запуск: python -m pytest tests/test_lobby.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402
from lobby import (  # noqa: E402
    ReceiptInput,
    collect_private_user_ids,
    granted_days_for,
    mailing_invite_text,
    normalize_tariff,
    parse_price,
    parse_tariff_duration,
    receipt_guide_text,
    validate_receipt,
    welcome_text,
)
from support_inbox import invite_to_bot_text  # noqa: E402


class ParseTariffDurationTests(unittest.TestCase):
    def test_days_and_bare_number(self) -> None:
        self.assertEqual(parse_tariff_duration("30 дней"), 30)
        self.assertEqual(parse_tariff_duration("30"), 30)
        self.assertEqual(parse_tariff_duration("1 день"), 1)
        self.assertEqual(parse_tariff_duration("на 14 дней"), 14)

    def test_months_weeks_years(self) -> None:
        self.assertEqual(parse_tariff_duration("1 месяц"), 30)
        self.assertEqual(parse_tariff_duration("месяц"), 30)
        self.assertEqual(parse_tariff_duration("3 мес"), 90)
        self.assertEqual(parse_tariff_duration("2 недели"), 14)
        self.assertEqual(parse_tariff_duration("1 год"), 365)

    def test_rejects_garbage_and_range(self) -> None:
        for raw in ("", "потом", "0", "-5 дней", "тариф"):
            with self.assertRaises(ValueError):
                parse_tariff_duration(raw)
        with self.assertRaises(ValueError):
            parse_tariff_duration("400 дней")


class ParsePriceTests(unittest.TestCase):
    def test_price(self) -> None:
        self.assertEqual(parse_price("1500"), 1500.0)
        self.assertEqual(parse_price("1 500 ₽"), 1500.0)
        self.assertEqual(parse_price("1500 руб"), 1500.0)
        self.assertEqual(parse_price("99.5"), 99.5)

    def test_rejects(self) -> None:
        for raw in ("", "даром", "0", "-10"):
            with self.assertRaises(ValueError):
                parse_price(raw)


class TariffNameTests(unittest.TestCase):
    def test_ok_and_reject_command(self) -> None:
        self.assertEqual(normalize_tariff("  VIP  "), "VIP")
        with self.assertRaises(ValueError):
            normalize_tariff("/start")
        with self.assertRaises(ValueError):
            normalize_tariff("x")


class ReceiptValidationTests(unittest.TestCase):
    def test_photo_ok(self) -> None:
        ok, reason = validate_receipt(
            ReceiptInput(has_photo=True, file_size=50_000, mime="image/jpeg"),
            1500,
        )
        self.assertTrue(ok, reason)

    def test_pdf_ok(self) -> None:
        ok, reason = validate_receipt(
            ReceiptInput(
                has_document=True,
                mime="application/pdf",
                file_name="check.pdf",
                file_size=20_000,
            ),
            500,
        )
        self.assertTrue(ok, reason)

    def test_rejects_sticker_and_tiny(self) -> None:
        ok, _ = validate_receipt(ReceiptInput(has_sticker=True), 100)
        self.assertFalse(ok)
        ok, _ = validate_receipt(
            ReceiptInput(has_photo=True, file_size=100, mime="image/jpeg"),
            100,
        )
        self.assertFalse(ok)

    def test_rejects_wrong_amount_in_caption(self) -> None:
        ok, reason = validate_receipt(
            ReceiptInput(
                has_photo=True,
                file_size=20_000,
                caption="оплачено 200 ₽",
            ),
            1500,
        )
        self.assertFalse(ok)
        self.assertIn("сумма", reason)

    def test_caption_amount_matches(self) -> None:
        ok, reason = validate_receipt(
            ReceiptInput(
                has_photo=True,
                file_size=20_000,
                caption="заказ на 1 500 руб",
            ),
            1500,
        )
        self.assertTrue(ok, reason)

    def test_rejects_video(self) -> None:
        ok, _ = validate_receipt(ReceiptInput(has_video=True, file_size=90_000), 100)
        self.assertFalse(ok)

    def test_grant_one_tariff_same_period(self) -> None:
        self.assertEqual(granted_days_for(30), 30)
        self.assertEqual(granted_days_for(90), 90)


class MailingPeerTests(unittest.TestCase):
    def test_skips_groups_bots_self_and_already_sent(self) -> None:
        dialogs = [
            SimpleNamespace(chat=SimpleNamespace(id=111, type="private", is_bot=False)),
            SimpleNamespace(chat=SimpleNamespace(id=222, type="private", is_bot=False)),
            SimpleNamespace(chat=SimpleNamespace(id=333, type="private", is_bot=True)),
            SimpleNamespace(chat=SimpleNamespace(id=-1001, type="channel", is_bot=False)),
            SimpleNamespace(chat=SimpleNamespace(id=999, type="private", is_bot=False)),
            SimpleNamespace(chat=SimpleNamespace(id=444, type="group", is_bot=False)),
        ]
        ids = collect_private_user_ids(
            dialogs, self_id=999, skip_ids={222}
        )
        self.assertEqual(ids, [111])

    def test_invite_mentions_shop_and_one_tariff(self) -> None:
        text = mailing_invite_text("ZzzLV_bot")
        self.assertIn("@ZzzLV_bot", text)
        self.assertIn("/lobby", text)
        self.assertIn("квитанц", text.lower())
        for blob in (text, welcome_text(), invite_to_bot_text("ZzzLV_bot")):
            low = blob.lower()
            self.assertNotIn("нейросет", low)
            self.assertNotIn("gemini", low)
            self.assertNotIn("ai studio", low)

    def test_receipt_guide_has_no_funpay(self) -> None:
        text = receipt_guide_text()
        self.assertNotIn("FunPay", text)
        self.assertNotIn("фанпей", text.lower())
        self.assertIn("Банк", text)


class LobbyDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "lobby.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mailing_and_claims(self) -> None:
        self.assertFalse(self.db.lobby_was_mailed(111))
        self.db.lobby_mark_mailed(111)
        self.assertTrue(self.db.lobby_was_mailed(111))
        self.assertEqual(self.db.lobby_mailing_count(), 1)

        cid = self.db.lobby_save_claim(
            user_id=111,
            username="buyer",
            tariff="VIP",
            duration_days=30,
            price=1500,
            receipt_file_id="file123",
            receipt_type="photo",
            status="granted",
            granted_days=60,
        )
        self.assertGreater(cid, 0)
        self.assertTrue(self.db.lobby_has_granted(111))
        self.assertFalse(self.db.lobby_has_granted(222))
        row = self.db.lobby_latest_claim(111)
        assert row is not None
        self.assertEqual(row["tariff"], "VIP")
        self.assertEqual(row["granted_days"], 60)
        self.assertEqual(row["status"], "granted")

    def test_shop_tariffs_roundtrip(self) -> None:
        self.db.shop_save_tariffs(
            [
                {
                    "shop_id": "19063",
                    "title": "VIP ALL-IN",
                    "short_name": "VIP ALL-IN",
                    "sort_order": 0,
                    "extra_json": '{"periods":[{"days":30,"price":990,"label":"30 д. — 990 ₽"}]}',
                }
            ]
        )
        rows = self.db.shop_list_tariffs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shop_id"], "19063")

    def test_known_users_and_comp_channels(self) -> None:
        self.assertIsNone(self.db.known_get(42))
        self.db.known_upsert(42, username="Hgfthjj", source="unread")
        row = self.db.known_get(42)
        assert row is not None
        self.assertEqual(row["username"], "Hgfthjj")
        self.db.known_mark_blocked(99)
        self.assertEqual(int(self.db.known_get(99)["blocked"]), 1)
        self.db.known_unblock(99)
        self.assertEqual(int(self.db.known_get(99)["blocked"]), 0)
        cid = self.db.comp_add_channel(-100123, title="VIP", username="vipchan")
        self.assertGreater(cid, 0)
        listed = self.db.comp_list_channels(enabled_only=True)
        self.assertEqual(len(listed), 1)
        self.db.comp_toggle_channel(cid)
        self.assertEqual(self.db.comp_list_channels(enabled_only=True), [])


if __name__ == "__main__":
    unittest.main()
