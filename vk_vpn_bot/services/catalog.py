"""
Каталог тарифов и партнёрских условий из кабинета.

Цены и лимиты меняются в админке Bedolaga, поэтому бот не хранит их в тексте,
а забирает через кабинетное API и коротко кэширует: пользователь видит те же
цифры, что и в мини-приложении.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from config import Settings
from services.payments import CabinetPaymentClient

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600


TIER_EMOJI = {1: "🥉", 2: "🥈", 3: "🥇"}

# Если панель отдаёт «безлимит» не на премиуме — подставляем разумные лимиты по уровню.
DEFAULT_TRAFFIC_GB_BY_TIER = {1: 100, 2: 300}

WHITELIST_SERVER_MARKERS = (
    "бел",
    "whitelist",
    "white list",
    "б.с.",
    " бс",
    "бс ",
)


def _server_has_whitelist(name: str) -> bool:
    low = name.lower().replace("ё", "е")
    return any(marker in low for marker in WHITELIST_SERVER_MARKERS)


def short_period(days: int) -> str:
    table = {30: "1 мес", 90: "3 мес", 180: "6 мес", 365: "1 год", 360: "1 год"}
    if days in table:
        return table[days]
    if days % 30 == 0:
        return f"{days // 30} мес"
    return f"{days} дн"


@dataclass(frozen=True)
class Period:
    days: int
    label: str
    price_label: str
    per_month_label: str
    price_kopeks: int = 0


@dataclass(frozen=True)
class Tariff:
    id: int
    name: str
    description: str
    tier: int
    traffic_label: str
    unlimited_traffic: bool
    traffic_limit_gb: int
    device_limit: int
    extra_device_kopeks: int
    servers: tuple[str, ...]
    periods: tuple[Period, ...] = field(default_factory=tuple)

    @property
    def emoji(self) -> str:
        return TIER_EMOJI.get(self.tier, "▸")

    @property
    def is_premium(self) -> bool:
        name = self.name.lower().replace("ё", "е")
        return self.tier >= 3 or "премиум" in name or "premium" in name

    @property
    def has_whitelist_server(self) -> bool:
        return any(_server_has_whitelist(server) for server in self.servers)

    @property
    def effective_traffic_gb(self) -> int | None:
        """None — безлимит (только премиум)."""
        if self.is_premium and (self.unlimited_traffic or self.traffic_limit_gb == 0):
            return None
        if self.traffic_limit_gb > 0:
            return self.traffic_limit_gb
        return DEFAULT_TRAFFIC_GB_BY_TIER.get(self.tier)

    @property
    def traffic_display(self) -> str:
        gb = self.effective_traffic_gb
        if gb is None:
            return "♾️ Бесконечность ГБ"
        return f"{gb} ГБ"

    @property
    def primary_period(self) -> Period | None:
        """Первый период из панели — как в мини-приложении (цена «от …»)."""
        if self.periods:
            return self.periods[0]
        return None

    @property
    def short_description(self) -> str:
        if self.description.strip():
            base = self.description.strip()
        elif self.is_premium:
            base = (
                "Максимум устройств и трафик без ограничений — "
                "для активного пользования на всех гаджетах."
            )
        elif self.tier == 2 or self.device_limit == 3:
            base = (
                "Для нескольких устройств — телефон, планшет и компьютер."
            )
        else:
            base = "Для одного устройства — телефона или ноутбука."
        if self.has_whitelist_server and "бел" not in base.lower():
            return f"{base} Есть сервер с белыми списками."
        return base

    @property
    def price_from_label(self) -> str:
        period = self.primary_period
        if not period or not period.price_label:
            return ""
        return f"от {period.price_label}"

    @property
    def cheapest(self) -> Period | None:
        if not self.periods:
            return None
        return min(self.periods, key=lambda p: p.price_kopeks or 10**9)


@dataclass(frozen=True)
class Offer:
    """Конкретная покупка: тариф + период + цена."""

    tariff: "Tariff"
    period: Period

    @property
    def label(self) -> str:
        """Подпись reply-кнопки (fallback)."""
        return (
            f"{self.tariff.emoji} {self.tariff.name} · "
            f"{short_period(self.period.days)} · {self.period.price_label}"
        )

    @property
    def button_label(self) -> str:
        """Короткая подпись inline-кнопки — как карточка в мини-приложении."""
        return f"{self.tariff.emoji} {self.tariff.name} · от {self.period.price_label}"

    @property
    def per_month_note(self) -> str:
        pm = self.period.per_month_label
        if pm and pm != self.period.price_label:
            return f"{pm}/мес"
        return ""


@dataclass(frozen=True)
class Catalog:
    tariffs: tuple[Tariff, ...]
    min_topup_kopeks: int = 5000
    quick_amounts_kopeks: tuple[int, ...] = ()
    referral_percent: int = 0

    @property
    def entry_price_label(self) -> str:
        """Самая низкая цена среди карточек тарифов — «от N ₽»."""
        prices = [
            o.period.price_kopeks
            for o in self.offers()
            if o.period.price_kopeks
        ]
        if not prices:
            return ""
        return f"от {min(prices) // 100} ₽"

    @property
    def max_devices(self) -> int:
        return max((t.device_limit for t in self.tariffs), default=0)

    def offers(self, limit: int = 9) -> list[Offer]:
        """По одному варианту на тариф (первый период), как в мини-приложении."""
        res: list[Offer] = []
        for tariff in self.tariffs:
            period = tariff.primary_period
            if period:
                res.append(Offer(tariff, period))
        res.sort(key=lambda o: o.period.price_kopeks or 10**9)
        return res[:limit]

    def find_offer(self, label: str) -> Offer | None:
        for offer in self.offers(limit=99):
            if offer.label == label or offer.button_label == label:
                return offer
        return None

    def find_offer_by_tariff(self, tariff_id: int, days: int) -> Offer | None:
        for tariff in self.tariffs:
            if tariff.id != tariff_id:
                continue
            for period in tariff.periods:
                if period.days == days:
                    return Offer(tariff, period)
            if tariff.primary_period:
                return Offer(tariff, tariff.primary_period)
        return None


_cache: tuple[float, Catalog] | None = None


def _parse_tariff(raw: dict) -> Tariff:
    periods = tuple(
        Period(
            days=int(p.get("days") or 0),
            label=str(p.get("label") or ""),
            price_label=str(p.get("price_label") or ""),
            per_month_label=str(p.get("price_per_month_label") or ""),
            price_kopeks=int(p.get("price_kopeks") or 0),
        )
        for p in (raw.get("periods") or [])
    )
    return Tariff(
        id=int(raw.get("id") or 0),
        name=str(raw.get("name") or "Тариф"),
        description=str(raw.get("description") or "").strip(),
        tier=int(raw.get("tier_level") or 0),
        traffic_label=str(raw.get("traffic_limit_label") or ""),
        unlimited_traffic=bool(raw.get("is_unlimited_traffic")),
        traffic_limit_gb=int(raw.get("traffic_limit_gb") or 0),
        device_limit=int(raw.get("device_limit") or 0),
        extra_device_kopeks=int(raw.get("device_price_kopeks") or 0),
        servers=tuple(
            str(s.get("name")) for s in (raw.get("servers") or []) if s.get("name")
        ),
        periods=periods,
    )


async def fetch_catalog(settings: Settings, bedolaga_user_id: int) -> Catalog | None:
    """
    Тарифы, лимиты пополнения и процент партнёрки.

    None — если кабинет недоступен: вызывающий код должен уметь обойтись
    без цифр, а не показывать пустой экран.
    """
    global _cache

    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    if not settings.cabinet_jwt_secret:
        return None

    client = CabinetPaymentClient(settings)

    tariffs: tuple[Tariff, ...] = ()
    min_topup = 5000
    quick: tuple[int, ...] = ()
    percent = 0

    try:
        options = await client.get_cabinet_json(
            bedolaga_user_id, "/cabinet/subscription/purchase-options"
        )
        tariffs = tuple(
            _parse_tariff(t)
            for t in (options.get("tariffs") or [])
            if t.get("is_available", True)
        )
        tariffs = tuple(sorted(tariffs, key=lambda t: t.tier))
    except Exception as exc:  # noqa: BLE001
        logger.warning("не удалось получить тарифы: %s", exc)
        return None

    try:
        methods = await client.get_payment_methods(bedolaga_user_id)
        if methods:
            min_topup = min(m.min_amount_kopeks for m in methods)
            for m in methods:
                if m.quick_amounts:
                    quick = tuple(m.quick_amounts)
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("не удалось получить методы оплаты: %s", exc)

    try:
        ref = await client.get_cabinet_json(bedolaga_user_id, "/cabinet/referral")
        percent = int(ref.get("commission_percent") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("не удалось получить условия партнёрки: %s", exc)

    catalog = Catalog(
        tariffs=tariffs,
        min_topup_kopeks=min_topup,
        quick_amounts_kopeks=quick,
        referral_percent=percent,
    )
    _cache = (now, catalog)
    return catalog


def reset_cache() -> None:
    global _cache
    _cache = None
