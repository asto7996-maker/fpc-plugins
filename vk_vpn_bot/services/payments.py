"""
Оплата через Platega — те же методы, что в мини-приложении cabinet.paskod.ru.

Схема (как в Telegram Mini App):
  1) JWT auto-login → access_token кабинета
  2) GET  /cabinet/balance/payment-methods
  3) POST /cabinet/balance/topup {amount_kopeks, payment_method=platega, payment_option}

Методы Platega (из админки / payment_method_configs):
  2  — СБП (QR)
  11 — Карты (RUB)
  13 — Криптовалюта
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from config import Settings
from services.cabinet_auth import create_auto_login_token
from services.bedolaga import get_bedolaga_client

logger = logging.getLogger(__name__)

# Коды Platega как в Bedolaga settings.get_platega_method_definitions()
PLATEGA_METHOD_SBP_QR = 2
PLATEGA_METHOD_CARD = 11
PLATEGA_METHOD_CRYPTO = 13

METHOD_LABELS: dict[int, str] = {
    PLATEGA_METHOD_SBP_QR: "🏦 СБП",
    PLATEGA_METHOD_CARD: "💳 Карта",
    PLATEGA_METHOD_CRYPTO: "🪙 Крипта",
}

METHOD_COPY: dict[int, dict[str, str]] = {
    PLATEGA_METHOD_SBP_QR: {
        "title": "СБП · QR",
        "summary": "Оплата через QR-код в приложении банка.",
        "how": "На странице оплаты отсканируйте QR-код в банковском приложении.",
        "timing": "Обычно приходит за 1–2 минуты.",
        "note": "Подходит для Сбера, Т‑Банка, ВТБ, Альфы и других.",
    },
    PLATEGA_METHOD_CARD: {
        "title": "Карта",
        "summary": "Оплата картой на защищённой странице.",
        "how": "Введите номер карты. Банк может прислать код из SMS.",
        "timing": "Сразу после подтверждения в банке.",
        "note": "МИР, Visa, Mastercard.",
    },
    PLATEGA_METHOD_CRYPTO: {
        "title": "Крипта",
        "summary": "Оплата криптовалютой, если нет карты РФ.",
        "how": "На странице выберите монету и переведите указанную сумму.",
        "timing": "От нескольких минут до часа.",
        "note": "Сумма фиксируется при создании счёта.",
    },
}


def method_label(code: int) -> str:
    return METHOD_LABELS.get(code, f"Platega {code}")


def method_copy(code: int) -> dict[str, str]:
    return METHOD_COPY.get(code, {
        "title": method_label(code),
        "summary": "Оплата на защищённой странице.",
        "how": "Следуйте подсказкам на странице оплаты.",
        "timing": "Обычно деньги приходят за несколько минут.",
        "note": "",
    })

# Быстрые суммы из payment_method_configs.platega.quick_amounts
DEFAULT_QUICK_AMOUNTS_KOPEKS = (5000, 10000, 15000, 50000)


@dataclass
class PaymentMethodOption:
    id: str
    name: str
    description: str = ""


@dataclass
class PaymentMethodInfo:
    id: str
    name: str
    min_amount_kopeks: int
    max_amount_kopeks: int
    options: list[PaymentMethodOption]
    quick_amounts: list[int]
    open_url_direct: bool = True


@dataclass
class TopUpResult:
    payment_id: str
    payment_url: str
    amount_kopeks: int
    amount_rubles: float
    status: str
    method_code: int
    method_label: str


class CabinetPaymentError(RuntimeError):
    """Ошибка создания платежа / доступа к кабинету."""


class CabinetPaymentClient:
    """Клиент кабинета для Platega top-up (SBP/QR и др.)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.bedolaga_api_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=30)

    async def _cabinet_access_token(self, bedolaga_user_id: int) -> str:
        if not self.settings.cabinet_jwt_secret:
            raise CabinetPaymentError("CABINET_JWT_SECRET не задан")
        auto_token = create_auto_login_token(
            bedolaga_user_id, self.settings.cabinet_jwt_secret
        )
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(
                f"{self.base}/cabinet/auth/login/auto",
                json={"token": auto_token},
                headers={"Accept": "application/json"},
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise CabinetPaymentError(
                        f"auto-login {resp.status}: {text[:200]}"
                    )
                data = await resp.json(content_type=None)
        access = data.get("access_token")
        if not access:
            raise CabinetPaymentError("Кабинет не вернул access_token")
        return str(access)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.request(
                method,
                f"{self.base}{path}",
                json=json_body,
                headers=headers,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.error(
                        "Cabinet %s %s → %s: %s",
                        method,
                        path,
                        resp.status,
                        text[:400],
                    )
                    raise CabinetPaymentError(
                        f"Cabinet API {resp.status}: {text[:300]}"
                    )
                if not text:
                    return {}
                return await resp.json(content_type=None)

    async def get_cabinet_json(self, bedolaga_user_id: int, path: str) -> Any:
        """GET любого кабинетного эндпоинта от имени пользователя."""
        token = await self._cabinet_access_token(bedolaga_user_id)
        return await self._request("GET", path, access_token=token)

    async def get_payment_methods(
        self, bedolaga_user_id: int
    ) -> list[PaymentMethodInfo]:
        token = await self._cabinet_access_token(bedolaga_user_id)
        raw = await self._request(
            "GET", "/cabinet/balance/payment-methods", access_token=token
        )
        methods: list[PaymentMethodInfo] = []
        for item in raw or []:
            options = [
                PaymentMethodOption(
                    id=str(o.get("id")),
                    name=str(o.get("name") or o.get("id")),
                    description=str(o.get("description") or ""),
                )
                for o in (item.get("options") or [])
            ]
            methods.append(
                PaymentMethodInfo(
                    id=str(item.get("id")),
                    name=str(item.get("name") or item.get("id")),
                    min_amount_kopeks=int(item.get("min_amount_kopeks") or 5000),
                    max_amount_kopeks=int(
                        item.get("max_amount_kopeks") or 100_000_000
                    ),
                    options=options,
                    quick_amounts=[
                        int(x) for x in (item.get("quick_amounts") or [])
                    ],
                    open_url_direct=bool(item.get("open_url_direct")),
                )
            )
        return methods

    async def purchase_tariff(
        self,
        bedolaga_user_id: int,
        *,
        tariff_id: int,
        period_days: int,
    ) -> dict[str, Any]:
        """
        Покупает тариф за счёт баланса кабинета.

        Возвращает:
          {"status": "activated", "raw": ...}          — подписка активирована
          {"status": "insufficient", "missing_kopeks"} — не хватает на балансе
                                                          (корзина сохранена)
          {"status": "error", "raw": ...}              — иная ошибка
        """
        token = await self._cabinet_access_token(bedolaga_user_id)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = {"tariff_id": int(tariff_id), "period_days": int(period_days)}
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(
                f"{self.base}/cabinet/subscription/purchase-tariff",
                json=body,
                headers=headers,
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    data = {}
        if status < 400:
            return {"status": "activated", "raw": data}
        detail = data.get("detail") if isinstance(data, dict) else None
        if (
            status == 402
            and isinstance(detail, dict)
            and detail.get("code") == "insufficient_funds"
        ):
            return {
                "status": "insufficient",
                "missing_kopeks": int(detail.get("missing_amount") or 0),
            }
        logger.error("purchase-tariff %s: %s", status, str(data)[:300])
        return {"status": "error", "raw": data}

    async def create_platega_topup(
        self,
        bedolaga_user_id: int,
        *,
        amount_kopeks: int,
        payment_option: int,
    ) -> TopUpResult:
        """Создаёт платёж Platega (метод из мини-приложения)."""
        token = await self._cabinet_access_token(bedolaga_user_id)
        body = {
            "amount_kopeks": int(amount_kopeks),
            "payment_method": "platega",
            "payment_option": str(int(payment_option)),
            "language": "ru",
        }
        data = await self._request(
            "POST",
            "/cabinet/balance/topup",
            access_token=token,
            json_body=body,
        )
        payment_url = data.get("payment_url") or data.get("invoice_url") or ""
        if not payment_url:
            raise CabinetPaymentError(
                f"Нет ссылки на оплату: {data}"
            )
        code = int(payment_option)
        return TopUpResult(
            payment_id=str(data.get("payment_id") or ""),
            payment_url=str(payment_url),
            amount_kopeks=int(data.get("amount_kopeks") or amount_kopeks),
            amount_rubles=float(
                data.get("amount_rubles")
                or (amount_kopeks / 100)
            ),
            status=str(data.get("status") or "pending"),
            method_code=code,
            method_label=METHOD_LABELS.get(code, f"Platega {code}"),
        )


async def ensure_bedolaga_id_for_payments(
    vk_user_id: int,
    first_name: str | None,
) -> int:
    """Гарантирует пользователя в панели и возвращает bedolaga user id."""
    from database import get_or_create_user, set_bedolaga_user_id

    client = get_bedolaga_client()
    if not client or not client.enabled:
        raise CabinetPaymentError("Bedolaga API недоступен")

    await get_or_create_user(vk_user_id, first_name=first_name)
    panel_user = await client.ensure_user(vk_user_id, first_name=first_name)
    bedolaga_id = int(panel_user.get("id") or 0)
    if not bedolaga_id:
        raise CabinetPaymentError(f"Не удалось создать пользователя: {panel_user}")
    await set_bedolaga_user_id(vk_user_id, bedolaga_id)
    return bedolaga_id


def format_rubles(kopeks: int) -> str:
    rub = kopeks / 100
    if rub == int(rub):
        return f"{int(rub)} ₽"
    return f"{rub:.2f} ₽"


def platega_options_from_methods(
    methods: list[PaymentMethodInfo],
) -> tuple[PaymentMethodInfo | None, list[PaymentMethodOption]]:
    """Достаёт Platega и её опции (СБП QR / карта / крипта)."""
    for m in methods:
        if m.id == "platega":
            return m, m.options
    return None, []
