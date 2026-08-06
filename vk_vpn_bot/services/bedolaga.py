"""
Асинхронный клиент Bedolaga Web API (Paskod / Remnawave).

Документация: https://docs.bedolagam.ru/api-reference/overview
Базовый URL для Paskod: https://cabinet.paskod.ru/api
Авторизация: заголовок X-API-Key
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Смещение, чтобы VK ID не пересекались с реальными Telegram ID
VK_TELEGRAM_ID_OFFSET = 8_000_000_000


def vk_to_pseudo_telegram_id(vk_user_id: int) -> int:
    """Стабильный псевдо-telegram_id для пользователя VK."""
    return VK_TELEGRAM_ID_OFFSET + int(vk_user_id)


@dataclass
class BedolagaSubscription:
    """Краткие данные подписки из Web API."""

    id: int
    user_id: int
    is_trial: bool
    status: str
    end_date: str | None
    subscription_url: str | None
    subscription_crypto_link: str | None = None

    @property
    def key_link(self) -> str:
        """Лучшая ссылка для импорта в Happ / клиент."""
        return (
            self.subscription_url
            or self.subscription_crypto_link
            or ""
        )


class BedolagaClient:
    """Клиент Web API Bedolaga."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url)

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "X-API-Key": self.api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        await self.start()
        assert self._session is not None
        url = f"{self.base_url}{path}"
        async with self._session.request(
            method, url, json=json_body, params=params
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.error(
                    "Bedolaga API %s %s → %s: %s",
                    method,
                    path,
                    resp.status,
                    text[:500],
                )
                raise RuntimeError(f"Bedolaga API {resp.status}: {text[:300]}")
            if not text:
                return {}
            return await resp.json(content_type=None)

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def get_user_by_telegram_id(self, telegram_id: int) -> dict[str, Any] | None:
        try:
            return await self._request("GET", f"/users/by-telegram-id/{telegram_id}")
        except RuntimeError as exc:
            if "404" in str(exc):
                logger.debug("Bedolaga user tg_id=%s not found", telegram_id)
                return None
            raise

    async def create_user(
        self,
        *,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "telegram_id": telegram_id,
            "language": "ru",
        }
        if username:
            payload["username"] = username
        if first_name:
            payload["first_name"] = first_name
        return await self._request("POST", "/users", json_body=payload)

    async def ensure_user(
        self,
        vk_user_id: int,
        first_name: str | None = None,
    ) -> dict[str, Any]:
        """Находит или создаёт пользователя Bedolaga для VK ID."""
        tg_id = vk_to_pseudo_telegram_id(vk_user_id)
        existing = await self.get_user_by_telegram_id(tg_id)
        if existing:
            return existing
        return await self.create_user(
            telegram_id=tg_id,
            username=f"vk_{vk_user_id}",
            first_name=first_name or f"VK {vk_user_id}",
        )

    async def create_trial_subscription(
        self,
        bedolaga_user_id: int,
        *,
        duration_days: int,
        replace_existing: bool = False,
    ) -> BedolagaSubscription:
        """Активирует триал через POST /users/{id}/subscription."""
        data = await self._request(
            "POST",
            f"/users/{bedolaga_user_id}/subscription",
            json_body={
                "is_trial": True,
                "duration_days": duration_days,
                "replace_existing": replace_existing,
            },
        )
        sub = self._parse_subscription(data)
        # Иногда ссылка появляется после синка с Remnawave — добираем GET'ом
        if not sub.key_link:
            import asyncio

            for delay in (0.8, 1.5, 2.5):
                await asyncio.sleep(delay)
                fresh = await self.get_user(bedolaga_user_id)
                sub = self._parse_subscription(fresh)
                if sub.key_link:
                    break
        return sub

    async def create_paid_subscription(
        self,
        bedolaga_user_id: int,
        *,
        duration_days: int,
        replace_existing: bool = True,
    ) -> BedolagaSubscription:
        data = await self._request(
            "POST",
            f"/users/{bedolaga_user_id}/subscription",
            json_body={
                "is_trial": False,
                "duration_days": duration_days,
                "replace_existing": replace_existing,
            },
        )
        sub = self._parse_subscription(data)
        if not sub.key_link:
            import asyncio

            for delay in (0.8, 1.5, 2.5):
                await asyncio.sleep(delay)
                fresh = await self.get_user(bedolaga_user_id)
                sub = self._parse_subscription(fresh)
                if sub.key_link:
                    break
        return sub

    async def get_user(self, bedolaga_user_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/users/{bedolaga_user_id}")

    async def get_panel_user_for_vk(
        self, vk_user_id: int, first_name: str | None = None
    ) -> dict[str, Any]:
        """Гарантирует пользователя в панели и возвращает карточку с подпиской."""
        user = await self.ensure_user(vk_user_id, first_name=first_name)
        user_id = int(user.get("id") or 0)
        if user_id:
            try:
                return await self.get_user(user_id)
            except Exception:  # noqa: BLE001
                return user
        return user

    @staticmethod
    def _parse_subscription(data: dict[str, Any]) -> BedolagaSubscription:
        """
        Ответ POST /users/{id}/subscription часто возвращает UserResponse:
        { subscription: {...}|null, subscriptions: [ {...} ] }
        либо сам объект подписки.
        """
        sub: dict[str, Any] | None = None
        if isinstance(data.get("subscription"), dict):
            sub = data["subscription"]
        elif isinstance(data.get("subscriptions"), list) and data["subscriptions"]:
            # Берём активную / первую
            active = [
                s
                for s in data["subscriptions"]
                if isinstance(s, dict)
                and str(s.get("actual_status") or s.get("status") or "") == "active"
            ]
            sub = active[0] if active else data["subscriptions"][0]
        elif "subscription_url" in data or "is_trial" in data:
            sub = data

        if not isinstance(sub, dict):
            sub = data if isinstance(data, dict) else {}

        return BedolagaSubscription(
            id=int(sub.get("id") or 0),
            user_id=int(
                sub.get("user_id")
                or data.get("id")
                or 0
            ),
            is_trial=bool(sub.get("is_trial")),
            status=str(sub.get("status") or sub.get("actual_status") or ""),
            end_date=sub.get("end_date"),
            subscription_url=sub.get("subscription_url"),
            subscription_crypto_link=sub.get("subscription_crypto_link"),
        )


# Глобальный клиент
_client: BedolagaClient | None = None


def get_bedolaga_client() -> BedolagaClient | None:
    """Возвращает клиент, если задан BEDOLAGA_API_KEY, иначе None."""
    global _client
    from config import get_settings

    settings = get_settings()
    if not settings.bedolaga_api_key:
        return None
    if _client is None:
        _client = BedolagaClient(
            base_url=settings.bedolaga_api_url,
            api_key=settings.bedolaga_api_key,
        )
    return _client


async def close_bedolaga_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
