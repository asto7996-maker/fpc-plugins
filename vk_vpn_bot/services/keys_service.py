"""
Сервис выдачи ключей: Bedolaga (реальные) или локальная генерация (демо).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import NamedTuple

from config import Settings
from database import activate_trial, renew_subscription, set_bedolaga_user_id
from database.models import User
from services.bedolaga import get_bedolaga_client
from services.vpn_keys import generate_vpn_key

logger = logging.getLogger(__name__)


class IssueResult(NamedTuple):
    user: User
    key: str
    is_trial: bool
    source: str  # "bedolaga" | "local"


def _parse_end_date(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except ValueError:
        return None


async def issue_trial_or_key(user: User, settings: Settings) -> IssueResult:
    """
    Выдаёт ключ:
    1) Если есть BEDOLAGA_API_KEY — создаёт юзера в панели и активирует триал.
    2) Иначе — локальная генерация (демо-ключ по шаблону).
    """
    client = get_bedolaga_client()

    if client and client.enabled:
        try:
            panel_user = await client.ensure_user(user.user_id, first_name=user.first_name)
            bedolaga_id = int(panel_user.get("id") or panel_user.get("user_id") or 0)
            if not bedolaga_id:
                raise RuntimeError(f"Bedolaga не вернул id пользователя: {panel_user}")

            await set_bedolaga_user_id(user.user_id, bedolaga_id)
            sub = await client.create_trial_subscription(
                bedolaga_id,
                duration_days=settings.trial_days,
                replace_existing=False,
            )
            key = sub.key_link or generate_vpn_key(user.user_id, settings)
            end = _parse_end_date(sub.end_date)
            updated = await activate_trial(
                user.user_id,
                key,
                settings.trial_days,
                bedolaga_user_id=bedolaga_id,
                subscription_end=end,
            )
            logger.info(
                "Триал через Bedolaga: vk=%s bedolaga=%s",
                user.user_id,
                bedolaga_id,
            )
            return IssueResult(user=updated, key=key, is_trial=True, source="bedolaga")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Bedolaga trial failed, fallback to local: %s", exc)

    # Локальный демо-режим
    key = generate_vpn_key(user.user_id, settings)
    updated = await activate_trial(user.user_id, key, settings.trial_days)
    return IssueResult(user=updated, key=key, is_trial=True, source="local")


async def issue_renewal(user: User, settings: Settings) -> IssueResult:
    """Продление: через Bedolaga (если ключ есть) или локально + ссылка в кабинет."""
    client = get_bedolaga_client()

    if client and client.enabled:
        try:
            panel_user = await client.ensure_user(user.user_id, first_name=user.first_name)
            bedolaga_id = int(panel_user.get("id") or panel_user.get("user_id") or 0)
            sub = await client.create_paid_subscription(
                bedolaga_id,
                duration_days=settings.renew_days,
                replace_existing=True,
            )
            key = sub.key_link or user.vpn_key or generate_vpn_key(user.user_id, settings)
            end = _parse_end_date(sub.end_date)
            updated = await renew_subscription(
                user.user_id,
                settings.renew_days,
                key,
                bedolaga_user_id=bedolaga_id,
                subscription_end=end,
            )
            return IssueResult(user=updated, key=key, is_trial=False, source="bedolaga")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Bedolaga renew failed: %s", exc)
            raise

    # Без API — локально не списываем оплату, только сообщаем что нужен кабинет
    raise RuntimeError(
        "Для продления нужен BEDOLAGA_API_KEY или оплата в кабинете "
        f"{settings.cabinet_url}"
    )
