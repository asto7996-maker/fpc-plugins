"""
Тесты реферальной системы.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.helpers import (
    format_referral,
    format_referral_bonus_notify,
    format_referral_inviter_bonus,
)
from services.referral import (
    ReferralStats,
    build_vk_referral_link,
    extract_referral_code,
    normalize_referral_code,
)


class _Settings:
    group_id = 240702990
    referral_inviter_bonus_days = 5
    cabinet_url = "https://cabinet.paskod.ru"


def test_normalize_referral_code() -> None:
    assert normalize_referral_code("refXqIbAQfu") == "refXqIbAQfu"
    assert normalize_referral_code("XqIbAQfu") == "refXqIbAQfu"
    assert normalize_referral_code("bad") is None
    assert normalize_referral_code("") is None


def test_extract_referral_code() -> None:
    assert extract_referral_code(ref="refXqIbAQfu") == "refXqIbAQfu"
    assert extract_referral_code(text="/start refXqIbAQfu") == "refXqIbAQfu"
    assert extract_referral_code(text="start refXqIbAQfu") == "refXqIbAQfu"
    assert extract_referral_code(text="привет") is None


def test_build_vk_referral_link() -> None:
    link = build_vk_referral_link(240702990, "refXqIbAQfu")
    assert link == "https://vk.me/club240702990?ref=refXqIbAQfu"


def test_format_referral_with_stats() -> None:
    settings = _Settings()
    stats = ReferralStats(
        referral_code="refXqIbAQfu",
        vk_link="https://vk.me/club240702990?ref=refXqIbAQfu",
        telegram_link="https://t.me/bot?start=refXqIbAQfu",
        total_referrals=3,
        active_referrals=2,
        total_earnings_rubles=150.0,
        commission_percent=15,
        is_enabled=True,
    )
    text = format_referral(settings, stats)
    assert "+5 дн." in text
    assert "15%" in text or "друз" in text.lower()
    assert "refXqIbAQfu" in text
    assert "vk.me/club240702990" in text
    assert "Приглашено" in text


def test_format_referral_bonus_messages() -> None:
    settings = _Settings()
    assert "+5 дн." in format_referral_inviter_bonus(settings)
    assert "+5 дн." in format_referral_bonus_notify(settings)


def main() -> None:
    test_normalize_referral_code()
    test_extract_referral_code()
    test_build_vk_referral_link()
    test_format_referral_with_stats()
    test_format_referral_bonus_messages()
    print("test_referral: OK")


if __name__ == "__main__":
    main()
