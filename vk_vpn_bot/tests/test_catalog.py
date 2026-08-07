"""
Тесты каталога тарифов: разбор ответа панели и подстановка цифр в тексты.
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
from services.catalog import Catalog, Offer, Period, Tariff, _parse_tariff, short_period

# Ответ /cabinet/subscription/purchase-options, снятый с рабочей панели
RAW_TARIFF = {
    "id": 4,
    "name": "Базовый",
    "tier_level": 1,
    "traffic_limit_gb": 0,
    "traffic_limit_label": "♾️ Безлимит",
    "is_unlimited_traffic": True,
    "device_limit": 1,
    "device_price_kopeks": 5000,
    "servers": [{"uuid": "x", "name": "Base"}],
    "periods": [
        {
            "days": 30,
            "label": "1 месяц",
            "price_kopeks": 4900,
            "price_label": "49 ₽",
            "price_per_month_label": "49 ₽",
        },
        {
            "days": 180,
            "label": "6 месяцев",
            "price_kopeks": 22900,
            "price_label": "229 ₽",
            "price_per_month_label": "38 ₽",
        },
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


def test_parse_tariff() -> None:
    t = _parse_tariff(RAW_TARIFF)
    assert t.id == 4
    assert t.name == "Базовый"
    assert t.unlimited_traffic is True
    assert t.device_limit == 1
    assert t.extra_device_kopeks == 5000
    assert t.servers == ("Base",)
    assert len(t.periods) == 2
    assert t.cheapest is not None and t.cheapest.price_kopeks == 4900


def test_catalog_aggregates() -> None:
    base = _parse_tariff(RAW_TARIFF)
    premium = Tariff(
        id=2,
        name="Премиум",
        tier=3,
        traffic_label="♾️ Безлимит",
        unlimited_traffic=True,
        device_limit=5,
        extra_device_kopeks=5000,
        servers=("prem",),
        periods=(
            Period(30, "1 месяц", "149 ₽", "149 ₽", 14900),
        ),
    )
    catalog = Catalog(
        tariffs=(base, premium),
        min_topup_kopeks=5000,
        quick_amounts_kopeks=(5000, 10000, 15000, 50000),
        referral_percent=15,
    )
    assert catalog.entry_price_label == "49 ₽"
    assert catalog.max_devices == 5


def test_texts_use_real_numbers() -> None:
    catalog = Catalog(
        tariffs=(_parse_tariff(RAW_TARIFF),),
        min_topup_kopeks=5000,
        quick_amounts_kopeks=(5000, 10000),
        referral_percent=15,
    )
    settings = _Settings()

    tariffs = format_tariffs(catalog, settings)
    assert "Базовый" in tariffs
    assert "49 ₽" in tariffs
    assert "229 ₽" in tariffs
    assert "38 ₽" in tariffs, "нужна цена за месяц на длинном периоде"
    assert "до 1" in tariffs

    pay = format_pay_intro(catalog)
    assert "50 ₽" in pay and "100 ₽" in pay

    ref = format_referral(settings, None, catalog)
    assert "15%" in ref
    assert "+5 дн." in ref


def test_texts_survive_without_catalog() -> None:
    settings = _Settings()
    # Кабинет недоступен — цифр нет, но экран остаётся рабочим
    tariffs = format_tariffs(None, settings)
    assert "кабинет" in tariffs.lower()
    pay = format_pay_intro(None)
    assert "50 ₽" in pay
    ref = format_referral(settings, None, None)
    assert "+5 дн." in ref
    assert "друг" in ref.lower()


def _make_catalog() -> Catalog:
    base = _parse_tariff(RAW_TARIFF)
    premium = Tariff(
        id=2,
        name="Премиум",
        tier=3,
        traffic_label="♾️ Безлимит",
        unlimited_traffic=True,
        device_limit=5,
        extra_device_kopeks=5000,
        servers=("prem",),
        periods=(
            Period(30, "1 месяц", "149 ₽", "149 ₽", 14900),
            Period(180, "6 месяцев", "699 ₽", "116 ₽", 69900),
        ),
    )
    return Catalog(
        tariffs=(base, premium),
        min_topup_kopeks=5000,
        quick_amounts_kopeks=(5000, 10000),
        referral_percent=15,
    )


def test_offers_sorted_and_labeled() -> None:
    cat = _make_catalog()
    offers = cat.offers()
    prices = [o.period.price_kopeks for o in offers]
    assert prices == sorted(prices), "офферы должны идти от дешёвых к дорогим"
    # Самый дешёвый — базовый на месяц
    assert offers[0].tariff.id == 4 and offers[0].period.days == 30
    # Ярлык узнаётся обратно
    label = offers[0].label
    assert "🥉" in label and "49 ₽" in label and "1 мес" in label
    assert cat.find_offer(label) == offers[0]
    assert cat.find_offer("несуществующий · 1 мес · 1 ₽") is None


def test_short_period() -> None:
    assert short_period(30) == "1 мес"
    assert short_period(180) == "6 мес"
    assert short_period(365) == "1 год"


def test_tariff_menu_and_choice() -> None:
    cat = _make_catalog()
    settings = _Settings()
    menu = format_tariff_menu(cat, settings, renew=True)
    assert "Продление" in menu
    assert "49 ₽" in menu
    assert "38 ₽" in menu  # цена за месяц на длинном периоде

    offer = cat.offers()[0]
    chosen = format_tariff_chosen(offer)
    assert "Базовый" in chosen
    assert "49 ₽" in chosen
    assert "оплатить" in chosen.lower()


def main() -> None:
    test_parse_tariff()
    test_catalog_aggregates()
    test_texts_use_real_numbers()
    test_texts_survive_without_catalog()
    test_offers_sorted_and_labeled()
    test_short_period()
    test_tariff_menu_and_choice()
    print("test_catalog: OK")


if __name__ == "__main__":
    main()
