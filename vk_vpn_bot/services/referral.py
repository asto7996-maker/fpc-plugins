"""
Реферальная программа VK-бота (интеграция с Bedolaga).

- Ссылка для ВК: vk.me/club{GROUP_ID}?ref={referral_code}
- При первом входе по ссылке — привязка referred_by_id в Bedolaga
- Бонус пригласившему: +N дней к подписке (REFERRAL_INVITER_BONUS_DAYS)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from config import Settings
from services.bedolaga import (
    VK_TELEGRAM_ID_OFFSET,
    BedolagaClient,
    get_bedolaga_client,
    vk_to_pseudo_telegram_id,
)
from services.payments import CabinetPaymentClient

logger = logging.getLogger(__name__)

REFERRAL_CODE_RE = re.compile(r"^ref[A-Za-z0-9]{6,}$", re.IGNORECASE)
START_REF_RE = re.compile(
    r"^(?:/?start\s+)?(ref[A-Za-z0-9]{6,})\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReferralStats:
    referral_code: str
    vk_link: str
    telegram_link: str
    total_referrals: int
    active_referrals: int
    total_earnings_rubles: float
    commission_percent: int
    is_enabled: bool


@dataclass(frozen=True)
class ReferralApplyResult:
    applied: bool
    inviter_bonus_granted: bool
    inviter_vk_id: int | None = None
    referral_code: str | None = None


def normalize_referral_code(raw: str | None) -> str | None:
    """Приводит код к формату refXXXXXXXX."""
    if not raw:
        return None
    code = raw.strip()
    if not code:
        return None
    if not code.lower().startswith("ref"):
        code = f"ref{code}"
    if not REFERRAL_CODE_RE.match(code):
        return None
    return code


def extract_referral_code(
    *,
    text: str | None = None,
    ref: str | None = None,
) -> str | None:
    """Достаёт реферальный код из VK ref или текста «/start ref…»."""
    if ref:
        parsed = normalize_referral_code(ref)
        if parsed:
            return parsed
    if text:
        match = START_REF_RE.match(text.strip())
        if match:
            return normalize_referral_code(match.group(1))
    return None


def build_vk_referral_link(group_id: int, referral_code: str) -> str:
    """Ссылка для приглашения друзей в VK-бот."""
    gid = abs(int(group_id))
    code = normalize_referral_code(referral_code) or referral_code
    return f"https://vk.me/club{gid}?ref={code}"


def bedolaga_id_to_vk_user_id(bedolaga_user: dict[str, Any]) -> int | None:
    """Обратное преобразование псевдо-telegram_id → VK ID."""
    tg_id = bedolaga_user.get("telegram_id")
    if tg_id is None:
        return None
    try:
        tg = int(tg_id)
    except (TypeError, ValueError):
        return None
    if tg >= VK_TELEGRAM_ID_OFFSET:
        return tg - VK_TELEGRAM_ID_OFFSET
    return None


async def fetch_referral_stats(
    settings: Settings,
    bedolaga_user_id: int,
) -> ReferralStats | None:
    """Данные рефералки из кабинета + VK-ссылка."""
    if not settings.cabinet_jwt_secret:
        return None

    client = CabinetPaymentClient(settings)
    try:
        info = await client.get_cabinet_json(bedolaga_user_id, "/cabinet/referral")
        terms = await client.get_cabinet_json(
            bedolaga_user_id, "/cabinet/referral/terms"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral stats unavailable: %s", exc)
        return None

    code = str(info.get("referral_code") or "").strip()
    if not code:
        return None

    vk_link = build_vk_referral_link(settings.group_id, code)
    tg_link = str(info.get("bot_referral_link") or "").strip()

    return ReferralStats(
        referral_code=code,
        vk_link=vk_link,
        telegram_link=tg_link,
        total_referrals=int(info.get("total_referrals") or 0),
        active_referrals=int(info.get("active_referrals") or 0),
        total_earnings_rubles=float(info.get("total_earnings_rubles") or 0),
        commission_percent=int(
            info.get("commission_percent") or terms.get("commission_percent") or 0
        ),
        is_enabled=bool(terms.get("is_enabled", True)),
    )


async def find_referrer_by_code(
    client: BedolagaClient,
    referral_code: str,
) -> dict[str, Any] | None:
    """Ищет пользователя Bedolaga по реферальному коду."""
    code = normalize_referral_code(referral_code)
    if not code:
        return None
    try:
        data = await client._request(  # noqa: SLF001
            "GET",
            "/users",
            params={"search": code, "limit": 20},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral search failed: %s", exc)
        return None

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None

    lowered = code.lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("referral_code") or "").lower() == lowered:
            return item
    return None


async def grant_inviter_bonus_days(
    client: BedolagaClient,
    inviter_bedolaga_id: int,
    days: int,
) -> bool:
    """Продлевает подписку пригласившему на N дней."""
    if days <= 0:
        return False
    try:
        await client.create_paid_subscription(
            inviter_bedolaga_id,
            duration_days=days,
            replace_existing=False,
        )
        logger.info(
            "referral bonus +%s days for bedolaga_user=%s",
            days,
            inviter_bedolaga_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to grant referral bonus to %s: %s",
            inviter_bedolaga_id,
            exc,
        )
        return False


async def apply_referral_for_new_user(
    vk_user_id: int,
    first_name: str | None,
    referral_code: str,
    settings: Settings,
) -> ReferralApplyResult:
    """
    Привязывает реферала при создании аккаунта Bedolaga.

  Возвращает applied=True, если referred_by_id записан впервые.
    """
    code = normalize_referral_code(referral_code)
    if not code:
        return ReferralApplyResult(applied=False, inviter_bonus_granted=False)

    client = get_bedolaga_client()
    if not client or not client.enabled:
        return ReferralApplyResult(
            applied=False,
            inviter_bonus_granted=False,
            referral_code=code,
        )

    tg_id = vk_to_pseudo_telegram_id(vk_user_id)
    existing = await client.get_user_by_telegram_id(tg_id)
    if existing:
        if existing.get("referred_by_id"):
            return ReferralApplyResult(
                applied=False,
                inviter_bonus_granted=False,
                referral_code=code,
            )
        return ReferralApplyResult(
            applied=False,
            inviter_bonus_granted=False,
            referral_code=code,
        )

    referrer = await find_referrer_by_code(client, code)
    if not referrer:
        logger.info("referral code not found: %s", code)
        return ReferralApplyResult(
            applied=False,
            inviter_bonus_granted=False,
            referral_code=code,
        )

    referrer_id = int(referrer.get("id") or 0)
    if not referrer_id:
        return ReferralApplyResult(
            applied=False,
            inviter_bonus_granted=False,
            referral_code=code,
        )

    referrer_vk = bedolaga_id_to_vk_user_id(referrer)
    if referrer_vk is not None and referrer_vk == vk_user_id:
        logger.info("self-referral blocked vk=%s code=%s", vk_user_id, code)
        return ReferralApplyResult(
            applied=False,
            inviter_bonus_granted=False,
            referral_code=code,
        )

    try:
        created = await client.create_user(
            telegram_id=tg_id,
            username=f"vk_{vk_user_id}",
            first_name=first_name or f"VK {vk_user_id}",
            referred_by_id=referrer_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral user create failed: %s", exc)
        return ReferralApplyResult(
            applied=False,
            inviter_bonus_granted=False,
            referral_code=code,
        )

    from database import set_bedolaga_user_id

    new_id = int(created.get("id") or 0)
    if new_id:
        await set_bedolaga_user_id(vk_user_id, new_id)

    bonus_granted = await grant_inviter_bonus_days(
        client,
        referrer_id,
        settings.referral_inviter_bonus_days,
    )

    return ReferralApplyResult(
        applied=True,
        inviter_bonus_granted=bonus_granted,
        inviter_vk_id=referrer_vk,
        referral_code=code,
    )
