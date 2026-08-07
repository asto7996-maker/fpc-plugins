"""
Тесты текстов оплаты: СБП/QR, карта, крипта.
Запуск из папки vk_vpn_bot:
    python3 tests/test_payments_text.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.helpers import (
    format_balance,
    format_pay_amount_prompt,
    format_pay_intro,
    format_payment_created,
    format_tariff_chosen,
    format_tariff_needs_topup,
)
from services.catalog import Catalog, Offer, Period, Tariff
from services.payments import (
    PLATEGA_METHOD_CARD,
    PLATEGA_METHOD_CRYPTO,
    PLATEGA_METHOD_SBP_QR,
    METHOD_COPY,
    method_label,
)


class _Settings:
    bot_name = "Paskod VPN"


def _offer() -> Offer:
    tariff = Tariff(
        id=4,
        name="Базовый",
        tier=1,
        traffic_label="♾️ Безлимит",
        unlimited_traffic=True,
        traffic_limit_gb=0,
        device_limit=1,
        extra_device_kopeks=0,
        servers=("ru",),
        periods=(Period(30, "1 месяц", "49 ₽", "49 ₽", 4900),),
    )
    period = tariff.periods[0]
    return Offer(tariff=tariff, period=period)


def test_method_labels_match_cabinet() -> None:
    assert method_label(PLATEGA_METHOD_SBP_QR) == "🏦 СБП"
    assert method_label(PLATEGA_METHOD_CARD) == "💳 Карта"
    assert method_label(PLATEGA_METHOD_CRYPTO) == "🪙 Крипта"
    for code in (PLATEGA_METHOD_SBP_QR, PLATEGA_METHOD_CARD, PLATEGA_METHOD_CRYPTO):
        copy = METHOD_COPY[code]
        assert copy["summary"] and copy["how"] and copy["timing"]


def test_pay_intro_lists_all_methods() -> None:
    text = format_pay_intro(None)
    assert "СБП" in text
    assert "Карт" in text or "карт" in text.lower()
    assert "Крипт" in text or "крипт" in text.lower()
    assert "50 ₽" in text


def test_amount_prompt_per_method() -> None:
    sbp = format_pay_amount_prompt(PLATEGA_METHOD_SBP_QR, None)
    card = format_pay_amount_prompt(PLATEGA_METHOD_CARD, None)
    crypto = format_pay_amount_prompt(PLATEGA_METHOD_CRYPTO, None)
    assert "QR" in sbp
    assert "3-D Secure" in card or "SMS" in card or "код" in card.lower()
    assert "крипт" in crypto.lower() or "монет" in crypto.lower()


def test_payment_created_includes_method_hint() -> None:
    text = format_payment_created(
        method_code=PLATEGA_METHOD_SBP_QR,
        amount_rubles=100,
        payment_url="https://pay.example/1",
    )
    assert "100 ₽" in text
    assert "https://pay.example/1" in text
    assert "QR" in text or "СБП" in text


def test_tariff_flow_texts() -> None:
    offer = _offer()
    chosen = format_tariff_chosen(offer)
    assert "СБП" in chosen and "Карт" in chosen
    topup = format_tariff_needs_topup(offer, "49 ₽", method_code=PLATEGA_METHOD_CARD)
    assert "49 ₽" in topup
    assert "карт" in topup.lower()


def test_balance_mentions_methods() -> None:
    text = format_balance(_Settings(), None)
    assert "СБП" in text
    assert "50" in text


def main() -> None:
    test_method_labels_match_cabinet()
    test_pay_intro_lists_all_methods()
    test_amount_prompt_per_method()
    test_payment_created_includes_method_hint()
    test_tariff_flow_texts()
    test_balance_mentions_methods()
    print("test_payments_text: OK")


if __name__ == "__main__":
    main()
