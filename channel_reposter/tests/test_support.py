"""Тарифы магазина, фильтр личек, проверка чека на Photoshop."""

from __future__ import annotations

import asyncio
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from receipt_forensics import inspect_receipt_bytes  # noqa: E402
from shop_catalog import (  # noqa: E402
    INFINITY_DAYS,
    Period,
    Tariff,
    catalog_from_db_rows,
    catalog_prices_summary,
    fetch_shop_catalog,
    format_duration_label,
    format_rub,
    match_period,
    match_tariff,
    parse_period_label,
    parse_shop_price_text,
    parse_tariff_period_label,
    period_button_text,
    shop_title_plain,
    short_name_for,
    tariffs_from_inline_rows,
    tariffs_to_db_rows,
)
from support_inbox import should_handle_top  # noqa: E402
from access import decide_inbox_action, is_privileged  # noqa: E402
from gemini_receipt import (  # noqa: E402
    USER_BAD_ANSWER,
    USER_CHECK_FAIL,
    GeminiVerdict,
    extract_model_text,
    inspect_receipt_gemini_sync,
    normalize_api_key,
    parse_verdict_json,
    vision_should_reject,
)


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

    def test_shop_price_text(self) -> None:
        self.assertEqual(parse_shop_price_text("💰 Тариф: VIP\n💲 Цена: 1 490 ₽"), 1490.0)
        self.assertEqual(parse_shop_price_text("💲 Цена: 245 ₽"), 245.0)
        self.assertEqual(parse_shop_price_text("Цена: 210 руб"), 210.0)
        self.assertIsNone(parse_shop_price_text("нет суммы"))

    def test_tariff_period_labels(self) -> None:
        inf = parse_tariff_period_label("INFINITY")
        assert inf is not None
        self.assertEqual(inf.days, INFINITY_DAYS)
        self.assertEqual(inf.label, "INFINITY")
        week = parse_tariff_period_label("Тариф на 7 дней")
        assert week is not None
        self.assertEqual(week.days, 7)
        month = parse_tariff_period_label("Тариф на 1 месяц")
        assert month is not None
        self.assertEqual(month.days, 30)
        year = parse_tariff_period_label("Тариф на 12 месяцев")
        assert year is not None
        self.assertEqual(year.days, 360)

    def test_period_buttons_include_price(self) -> None:
        t = Tariff(
            shop_id="19065",
            title="MILF Exclusive",
            short_name="MILF Exclusive",
            periods=[Period(days=7, price=245, label="7 дней")],
        )
        days, label, price = t.period_buttons()[0]
        self.assertEqual(days, 7)
        self.assertEqual(price, 245.0)
        self.assertIn("245", label)
        self.assertIn("₽", label)
        self.assertEqual(period_button_text(t.periods[0]), "7 дней — 245 ₽")

    def test_match_period_uses_shop_price(self) -> None:
        t = Tariff(
            shop_id="19063",
            title="VIP ALL-IN",
            short_name="VIP ALL-IN",
            periods=[Period(days=INFINITY_DAYS, price=1490, label="INFINITY")],
        )
        hit = match_period("INFINITY — 1 490 ₽", t)
        assert hit is not None
        self.assertEqual(hit[0], INFINITY_DAYS)
        self.assertEqual(hit[1], 1490.0)
        hit2 = match_period("INFINITY", t)
        assert hit2 is not None
        self.assertEqual(hit2[1], 1490.0)

    def test_catalog_roundtrip_keeps_prices(self) -> None:
        tariffs = [
            Tariff(
                shop_id="19063",
                title="VIP ALL-IN",
                short_name="VIP ALL-IN",
                callback="resource/view?id=19063",
                periods=[Period(days=INFINITY_DAYS, price=1490, label="INFINITY")],
            )
        ]
        rows = tariffs_to_db_rows(tariffs)
        self.assertIn("price_source", rows[0]["extra_json"])
        back = catalog_from_db_rows(rows)
        self.assertEqual(back[0].periods[0].price, 1490.0)
        summary = catalog_prices_summary(back)
        self.assertIn("1 490", summary)
        self.assertEqual(format_rub(1490), "1 490 ₽")
        self.assertEqual(format_duration_label(INFINITY_DAYS), "INFINITY / бессрочно")


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


class AccessPolicyTests(unittest.TestCase):
    def test_seed_unread_before_lock(self) -> None:
        self.assertEqual(
            decide_inbox_action(
                privileged=False,
                known=False,
                blocked=False,
                incoming=True,
                seed_done=False,
                unread=True,
            ),
            "seed",
        )

    def test_block_strangers_after_seed(self) -> None:
        self.assertEqual(
            decide_inbox_action(
                privileged=False,
                known=False,
                blocked=False,
                incoming=True,
                seed_done=True,
                unread=True,
            ),
            "block",
        )

    def test_known_is_reminded(self) -> None:
        self.assertEqual(
            decide_inbox_action(
                privileged=False,
                known=True,
                blocked=False,
                incoming=True,
                seed_done=True,
                unread=False,
            ),
            "remind",
        )

    def test_test_account_privileged(self) -> None:
        self.assertTrue(is_privileged(1, "Hgfthjj"))
        self.assertFalse(is_privileged(1, "random_user"))


class GeminiVerdictTests(unittest.TestCase):
    def test_rejects_chat_screenshot(self) -> None:
        v = parse_verdict_json(
            '{"is_receipt": false, "edited": false, "amount": null, '
            '"amount_matches": false, "verdict": "reject", '
            '"reason": "это скриншот чата"}',
            179,
        )
        self.assertFalse(v.ok)
        self.assertIn("чат", v.reason.lower())

    def test_ok_matching_amount(self) -> None:
        v = parse_verdict_json(
            '{"is_receipt": true, "edited": false, "amount": 179, '
            '"amount_matches": true, "verdict": "ok", "reason": "квитанция СБП"}',
            179,
        )
        self.assertTrue(v.ok)

    def test_rejects_photoshop(self) -> None:
        v = parse_verdict_json(
            '{"is_receipt": true, "edited": true, "amount": 179, '
            '"amount_matches": true, "verdict": "ok", "reason": "слои"}',
            179,
        )
        self.assertFalse(v.ok)

    def test_rejects_wrong_amount(self) -> None:
        v = parse_verdict_json(
            '{"is_receipt": true, "edited": false, "amount": 50, '
            '"amount_matches": true, "verdict": "ok", "reason": "чек"}',
            179,
        )
        self.assertFalse(v.ok)
        self.assertFalse(v.amount_matches)

    def test_rejects_near_amount_no_tolerance(self) -> None:
        v = parse_verdict_json(
            '{"is_receipt": true, "edited": false, "amount": 1499, '
            '"amount_matches": true, "verdict": "ok", "reason": "почти"}',
            1500,
        )
        self.assertFalse(v.ok)
        self.assertFalse(v.amount_matches)
        v_ok = parse_verdict_json(
            '{"is_receipt": true, "edited": false, "amount": 1500.00, '
            '"amount_matches": false, "verdict": "ok", "reason": "чек"}',
            1500,
        )
        self.assertTrue(v_ok.ok)
        self.assertTrue(v_ok.amount_matches)

    def test_parse_errors_do_not_mention_ai(self) -> None:
        v = parse_verdict_json("not-json", 179)
        self.assertEqual(v.reason, USER_BAD_ANSWER)
        self.assertNotIn("нейросет", v.reason.lower())
        self.assertNotIn("gemini", v.reason.lower())

    def test_aq_key_normalized(self) -> None:
        raw = '  "AQ.Ab8ExampleKeyValue0123456789"  \n'
        self.assertTrue(normalize_api_key(raw).startswith("AQ."))
        self.assertNotIn('"', normalize_api_key(raw))
        self.assertNotIn(" ", normalize_api_key(raw))

    def test_extract_interactions_and_generate_text(self) -> None:
        ix = {
            "output_text": '{"is_receipt": true, "edited": false, "amount": 179, '
            '"amount_matches": true, "verdict": "ok", "reason": "квитанция"}'
        }
        self.assertIn("квитанция", extract_model_text(ix))
        gc = {
            "candidates": [
                {"content": {"parts": [{"text": '{"verdict":"ok"}'}]}}
            ]
        }
        self.assertIn("verdict", extract_model_text(gc))

    def _jpeg(self) -> bytes:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("pillow")
        img = Image.new("RGB", (640, 480), (240, 240, 240))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()

    def test_aq_key_uses_header_and_interactions(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def fake_post(url, headers, payload, timeout):
            calls.append((url, dict(headers)))
            if "interactions" in url:
                body = (
                    '{"output_text": "{\\"is_receipt\\": true, \\"edited\\": false, '
                    '\\"amount\\": 179, \\"amount_matches\\": true, '
                    '\\"verdict\\": \\"ok\\", \\"reason\\": \\"квитанция СБП\\"}"}'
                )
                return 200, body
            return 500, "{}"

        with patch("gemini_receipt._post", side_effect=fake_post):
            v = inspect_receipt_gemini_sync(
                self._jpeg(),
                mime="image/jpeg",
                filename="check.jpg",
                declared_price=179,
                api_key="AQ.Ab8FakeTestKeyForHeaderCheck0123456789",
                model="gemini-3.6-flash",
            )
        self.assertTrue(v.ok, v.reason)
        self.assertTrue(any("interactions" in url for url, _ in calls))
        self.assertTrue(
            any(
                h.get("x-goog-api-key", "").startswith("AQ.")
                for _, h in calls
            )
        )

    def test_http_401_is_generic_for_user(self) -> None:
        def fake_post(url, headers, payload, timeout):
            return 401, '{"error":{"status":"UNAUTHENTICATED"}}'

        with patch("gemini_receipt._post", side_effect=fake_post):
            v = inspect_receipt_gemini_sync(
                self._jpeg(),
                mime="image/jpeg",
                filename="check.jpg",
                declared_price=179,
                api_key="AQ.Ab8FakeTestKeyForHeaderCheck0123456789",
            )
        self.assertFalse(v.ok)
        self.assertTrue(v.unavailable)
        self.assertFalse(vision_should_reject(v))
        self.assertEqual(v.reason, USER_CHECK_FAIL)
        self.assertNotIn("нейросет", v.reason.lower())
        self.assertNotIn("gemini", v.reason.lower())
        self.assertNotIn("ai studio", v.reason.lower())

    def test_api_outage_does_not_reject_receipt(self) -> None:
        down = GeminiVerdict(ok=False, reason=USER_CHECK_FAIL, flags=["check-error"])
        no_key = GeminiVerdict(ok=False, reason=USER_CHECK_FAIL, flags=["no-key"])
        fake = parse_verdict_json(
            '{"is_receipt": false, "edited": false, "amount": null, '
            '"amount_matches": false, "verdict": "reject", '
            '"reason": "это скриншот чата"}',
            179,
        )
        ok = parse_verdict_json(
            '{"is_receipt": true, "edited": false, "amount": 179, '
            '"amount_matches": true, "verdict": "ok", "reason": "квитанция СБП"}',
            179,
        )
        self.assertFalse(vision_should_reject(down))
        self.assertFalse(vision_should_reject(no_key))
        self.assertTrue(vision_should_reject(fake))
        self.assertFalse(vision_should_reject(ok))


class _Btn:
    def __init__(self, text: str, data: str) -> None:
        self.text = text
        self.callback_data = data


class _Msg:
    def __init__(self, mid: int, text: str = "", buttons: list[list[tuple[str, str]]] | None = None) -> None:
        self.id = mid
        self.text = text
        self.caption = ""
        if buttons:
            self.reply_markup = SimpleNamespace(
                inline_keyboard=[[_Btn(t, d) for t, d in row] for row in buttons]
            )
        else:
            self.reply_markup = None


class FakeShopClient:
    """Имитирует список → карточку → страницу цены. Клик «Оплатить» — ошибка."""

    PRICES = {
        "resource/tariff?id=11&r_id=19063": 1490,
        "resource/tariff?id=21&r_id=19065": 245,
        "resource/tariff?id=22&r_id=19065": 690,
    }

    def __init__(self) -> None:
        self._n = 0
        self.history: list[_Msg] = []
        self.clicked: list[str] = []

    def _push(self, text: str = "", buttons=None) -> _Msg:
        self._n += 1
        msg = _Msg(self._n, text=text, buttons=buttons)
        self.history.insert(0, msg)
        return msg

    async def send_message(self, _chat: str, text: str) -> _Msg:
        if "Tariffs" in text or text.startswith("🍭"):
            return self._push(
                "list",
                [
                    [("VIP ALL-IN", "resource/view?id=19063")],
                    [("MILF Exclusive", "resource/view?id=19065")],
                ],
            )
        return self._push(text)

    async def request_callback_answer(self, _chat: str, _msg_id: int, data: str) -> None:
        self.clicked.append(data)
        if data.lower().startswith("pay"):
            raise AssertionError(f"must not click pay: {data}")
        if data.startswith("resource/view?id=19063"):
            self._push(
                "vip card",
                [[("INFINITY", "resource/tariff?id=11&r_id=19063")]],
            )
            return
        if data.startswith("resource/view?id=19065"):
            self._push(
                "milf card",
                [
                    [("Тариф на 7 дней", "resource/tariff?id=21&r_id=19065")],
                    [("Тариф на 1 месяц", "resource/tariff?id=22&r_id=19065")],
                ],
            )
            return
        if data in self.PRICES:
            price = self.PRICES[data]
            grouped = f"{price:,}".replace(",", " ")
            self._push(
                f"💰 Тариф: x\n💲 Цена: {grouped} ₽",
                [[("💳Оплатить", "pay/main?id=s-1")]],
            )

    async def get_chat_history(self, _chat: str, limit: int = 15):
        for m in self.history[:limit]:
            yield m


class FetchShopCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_crawls_prices_and_skips_pay(self) -> None:
        client = FakeShopClient()

        async def _no_sleep(*_a, **_k):
            return None

        with patch("shop_catalog.asyncio.sleep", new=_no_sleep):
            tariffs = await fetch_shop_catalog(client)
        self.assertEqual(len(tariffs), 2)
        vip = next(t for t in tariffs if t.shop_id == "19063")
        milf = next(t for t in tariffs if t.shop_id == "19065")
        self.assertEqual(len(vip.periods), 1)
        self.assertEqual(vip.periods[0].days, INFINITY_DAYS)
        self.assertEqual(vip.periods[0].price, 1490.0)
        self.assertEqual(len(milf.periods), 2)
        by_days = {p.days: p.price for p in milf.periods}
        self.assertEqual(by_days[7], 245.0)
        self.assertEqual(by_days[30], 690.0)
        self.assertTrue(any(c.startswith("resource/tariff") for c in client.clicked))
        self.assertFalse(any(c.lower().startswith("pay") for c in client.clicked))


if __name__ == "__main__":
    unittest.main()
