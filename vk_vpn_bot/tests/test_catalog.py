"""
Тесты каталога тарифов: цены мини-приложения (от 49 ₽) и тексты.
Запуск из папки vk_vpn_bot:
    python3 tests/test_catalog.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.helpers import (
    format_pay_intro,
    format_referral,
    format_tariff_chosen,
    format_tariff_menu,
    format_tariffs,
)
from services.catalog import (
    Catalog,
    Period,
    Tariff,
    _parse_tariff,
    miniapp_fallback_catalog,
    short_period,
)

# Ответ API с «ложной» ценой 39 ₽ на коротком сроке — бот должен показать 49 ₽
RAW_TARIFF = {
    "id": 4,
    "name": "Базовый",
    "description": "Для одного устройства",
    "tier_level": 1,
    "traffic_limit_gb": 0,
    "traffic_limit_label": "♾️ Безлимит",
    "is_unlimited_traffic": True,
    "device_limit": 1,
    "device_price_kopeks": 5000,
    "servers": [{"uuid": "x", "name": "Base"}],
    "periods": [
        {
            "days": 14,
            "label": "2 недели",
            "price_kopeks": 3900,
            "price_label": "39 ₽",
            "price_per_month_label": "39 ₽",
        },
        {
            "days": 30,
            "label": "1 месяц",
            "price_kopeks": 3900,
            "price_label": "39 ₽",
            "price_per_month_label": "39 ₽",
        },
        {
            "days": 180,
            "label": "6 месяцев",
            "price_kopeks": 17900,
            "price_label": "179 ₽",
            "price_per_month_label": "30 ₽",
        },
    ],
    "is_available": True,
}

RAW_STANDARD = {
    "id": 5,
    "name": "Стандарт",
    "tier_level": 2,
    "traffic_limit_gb": 0,
    "traffic_limit_label": "♾️ Безлимит",
    "is_unlimited_traffic": True,
    "device_limit": 3,
    "device_price_kopeks": 5000,
    "servers": [{"uuid": "y", "name": "Std"}],
    "periods": [
        {
            "days": 30,
            "label": "1 месяц",
            "price_kopeks": 7900,
            "price_label": "79 ₽",
            "price_per_month_label": "79 ₽",
        }
    ],
    "is_available": True,
}

RAW_PREMIUM = {
    "id": 2,
    "name": "Премиум",
    "tier_level": 3,
    "traffic_limit_gb": 0,
    "traffic_limit_label": "♾️ Безлимит",
    "is_unlimited_traffic": True,
    "device_limit": 5,
    "device_price_kopeks": 5000,
    "servers": [{"uuid": "z", "name": "Белые списки RU"}],
    "periods": [
        {
            "days": 30,
            "label": "1 месяц",
            "price_kopeks": 11900,
            "price_label": "119 ₽",
            "price_per_month_label": "119 ₽",
        }
    ],
    "is_available": True,
}


class _Settings:
    trial_days = 3
    trial_traffic_gb = 10
    trial_devices = 1
    cabinet_url = "https://cabinet.paskod.ru"
    bedolaga_api_key = "k"
    renew_days = 30
    referral_inviter_bonus_days = 5
    group_id = 240702990


def test_parse_overrides_39_to_49() -> None:
    t = _parse_tariff(RAW_TARIFF)
    assert t.name == "Базовый"
    assert all(p.days >= 30 for p in t.periods)
    assert t.primary_period is not None
    assert t.primary_period.days == 30
    # На экране — 49 ₽ как в мини-приложении; списание может быть из API
    assert t.primary_period.price_label == "49 ₽"
    assert t.price_from_label == "от 49 ₽"
    assert t.traffic_display == "100 ГБ"


def test_premium_unlimited_whitelist() -> None:
    t = _parse_tariff(RAW_PREMIUM)
    assert t.traffic_display == "♾️ Бесконечность ГБ"
    assert t.has_whitelist_server is True
    assert t.price_from_label == "от 119 ₽"


def test_catalog_three_offers_from_miniapp() -> None:
    catalog = Catalog(
        tariffs=(
            _parse_tariff(RAW_TARIFF),
            _parse_tariff(RAW_STANDARD),
            _parse_tariff(RAW_PREMIUM),
        ),
        min_topup_kopeks=5000,
        quick_amounts_kopeks=(5000, 10000),
        referral_percent=15,
    )
    offers = catalog.offers()
    assert len(offers) == 3
    assert [o.period.price_label for o in offers] == ["49 ₽", "79 ₽", "119 ₽"]
    assert catalog.entry_price_label == "от 49 ₽"

    menu = format_tariff_menu(catalog, _Settings(), renew=False)
    assert "от 49 ₽" in menu
    assert "39" not in menu
    assert "мини-приложении" in menu.lower()
    assert "Бесконечность ГБ" in menu
    assert "белые списки" in menu.lower()

    tariffs = format_tariffs(catalog, _Settings())
    assert "от 49 ₽" in tariffs
    assert "от 79 ₽" in tariffs
    assert "от 119 ₽" in tariffs


def test_fallback_catalog() -> None:
    cat = miniapp_fallback_catalog()
    assert cat.entry_price_label == "от 49 ₽"
    assert len(cat.offers()) == 3
    assert cat.offers()[0].period.price_label == "49 ₽"


def test_texts_survive_without_catalog() -> None:
    settings = _Settings()
    tariffs = format_tariffs(None, settings)
    assert "кабинет" in tariffs.lower()
    pay = format_pay_intro(None)
    assert "50 ₽" in pay
    ref = format_referral(settings, None, None)
    assert "+5 дн." in ref


def test_chosen_and_buttons() -> None:
    cat = Catalog(
        tariffs=(_parse_tariff(RAW_TARIFF), _parse_tariff(RAW_PREMIUM)),
        min_topup_kopeks=5000,
    )
    offer = cat.offers()[0]
    assert "от 49 ₽" in offer.button_label
    chosen = format_tariff_chosen(offer)
    assert "49 ₽" in chosen
    assert "100 ГБ" in chosen
    assert cat.find_offer(offer.button_label) == offer


def test_short_period() -> None:
    assert short_period(30) == "1 мес"
    assert short_period(180) == "6 мес"


def main() -> None:
    test_parse_overrides_39_to_49()
    test_premium_unlimited_whitelist()
    test_catalog_three_offers_from_miniapp()
    test_fallback_catalog()
    test_texts_survive_without_catalog()
    test_chosen_and_buttons()
    test_short_period()
    print("test_catalog: OK")


if __name__ == "__main__":
    main()
