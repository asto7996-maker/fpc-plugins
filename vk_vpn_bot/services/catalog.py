"""
Каталог тарифов: цены как в мини-приложении cabinet.paskod.ru.

Карточки в VK-боте — те же 3 тарифа с ценой «от … ₽/мес».
Короткие сроки (< 30 дней) из API не показываем: из‑за них в боте
появлялись 39 ₽ вместо 49 ₽.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace

from config import Settings
from services.payments import CabinetPaymentClient

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60

TIER_EMOJI = {1: "🥉", 2: "🥈", 3: "🥇"}

# Лимиты ГБ, если панель отдаёт «безлимит» не на премиуме.
DEFAULT_TRAFFIC_GB_BY_TIER = {1: 100, 2: 300}

# Цены мини-приложения (₽ за период). Источник правды для экрана тарифов.
# Ключ — нормализованное имя тарифа.
MINIAPP_PRICES_RUB: dict[str, dict[int, int]] = {
    "базовый": {30: 49, 90: 99, 180: 179},
    "стандарт": {30: 79, 90: 199, 180: 349},
    "премиум": {30: 119, 90: 299, 180: 499},
}

PREFERRED_PERIOD_DAYS = (30, 90, 180, 365)
MIN_PERIOD_DAYS = 30  # не короче месяца — как карточки «от» в мини-приложении

WHITELIST_SERVER_MARKERS = (
    "бел",
    "whitelist",
    "white list",
    "б.с.",
    " бс",
    "бс ",
)


def _norm_name(name: str) -> str:
    return name.lower().replace("ё", "е").strip()


def _server_has_whitelist(name: str) -> bool:
    low = name.lower().replace("ё", "е")
    return any(marker in low for marker in WHITELIST_SERVER_MARKERS)


def _format_price_label(kopeks: int) -> str:
    rub = kopeks // 100
    if kopeks % 100:
        return f"{kopeks / 100:.2f} ₽".replace(".", ",")
    return f"{rub} ₽"


def short_period(days: int) -> str:
    table = {30: "1 мес", 90: "3 мес", 180: "6 мес", 365: "1 год", 360: "1 год"}
    if days in table:
        return table[days]
    if days % 30 == 0:
        return f"{days // 30} мес"
    return f"{days} дн"


def _period_label(days: int) -> str:
    table = {30: "1 месяц", 90: "3 месяца", 180: "6 месяцев", 365: "1 год"}
    return table.get(days, f"{days} дн.")


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
        name = _norm_name(self.name)
        return self.tier >= 3 or "премиум" in name or "premium" in name

    @property
    def has_whitelist_server(self) -> bool:
        if self.is_premium:
            return True
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
        return _pick_primary_period(self.periods)

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
            base = "Для нескольких устройств — телефон, планшет и компьютер."
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
        return (
            f"{self.tariff.emoji} {self.tariff.name} · "
            f"{short_period(self.period.days)} · {self.period.price_label}"
        )

    @property
    def button_label(self) -> str:
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
        """«от N ₽» по подписи мини-приложения (не по сырому API)."""
        offers = self.offers()
        if not offers:
            return "от 49 ₽"
        return f"от {offers[0].period.price_label}"

    @property
    def max_devices(self) -> int:
        return max((t.device_limit for t in self.tariffs), default=0)

    def offers(self, limit: int = 9) -> list[Offer]:
        """По одному варианту на тариф — месячная цена «от» как в мини-приложении."""
        res: list[Offer] = []
        for tariff in self.tariffs:
            period = tariff.primary_period
            if period:
                res.append(Offer(tariff, period))
        res.sort(key=lambda o: (o.tariff.tier, o.period.price_kopeks or 10**9))
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


def _pick_primary_period(periods: tuple[Period, ...]) -> Period | None:
    if not periods:
        return None
    by_days = {p.days: p for p in periods}
    for days in PREFERRED_PERIOD_DAYS:
        if days in by_days:
            return by_days[days]
    return periods[0]


def _sort_periods(periods: tuple[Period, ...]) -> tuple[Period, ...]:
    order = {days: idx for idx, days in enumerate(PREFERRED_PERIOD_DAYS)}
    return tuple(sorted(periods, key=lambda p: (order.get(p.days, 99), p.days)))


def _apply_miniapp_prices(tariff: Tariff) -> Tariff:
    """Подставляет подписи цен мини-приложения; сумму списания берёт из API."""
    table = MINIAPP_PRICES_RUB.get(_norm_name(tariff.name))
    if not table:
        periods = tuple(p for p in tariff.periods if p.days >= MIN_PERIOD_DAYS)
        return replace(tariff, periods=_sort_periods(periods)) if periods else tariff

    by_days = {p.days: p for p in tariff.periods if p.days >= MIN_PERIOD_DAYS}
    rebuilt: list[Period] = []
    for days, rub in sorted(table.items()):
        display_kopeks = rub * 100
        api = by_days.get(days)
        label = api.label if api and api.label else _period_label(days)
        months = max(1, days // 30)
        rebuilt.append(
            Period(
                days=days,
                label=label,
                price_label=_format_price_label(display_kopeks),
                per_month_label=_format_price_label(display_kopeks // months),
                # На экране и в кнопках — цена мини-приложения.
                # purchase-tariff списывает по id+days из панели.
                price_kopeks=display_kopeks,
            )
        )
    if not rebuilt:
        return tariff
    return replace(tariff, periods=_sort_periods(tuple(rebuilt)))


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
    tariff = Tariff(
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
        periods=_sort_periods(periods),
    )
    return _apply_miniapp_prices(tariff)


def miniapp_fallback_catalog(
    *,
    min_topup_kopeks: int = 5000,
    quick_amounts_kopeks: tuple[int, ...] = (5000, 10000, 15000, 50000),
    referral_percent: int = 0,
) -> Catalog:
    """
    Каталог как в мини-приложении, если API недоступен.
    id=0 — покупка через кабинет (кнопка «В кабинет»).
    """
    specs = (
        ("Базовый", 1, 1, 100, False),
        ("Стандарт", 2, 3, 300, False),
        ("Премиум", 3, 5, 0, True),
    )
    tariffs: list[Tariff] = []
    for name, tier, devices, gb, unlimited in specs:
        table = MINIAPP_PRICES_RUB[_norm_name(name)]
        periods = tuple(
            Period(
                days=days,
                label=_period_label(days),
                price_label=_format_price_label(rub * 100),
                per_month_label=_format_price_label((rub * 100) // max(1, days // 30)),
                price_kopeks=rub * 100,
            )
            for days, rub in sorted(table.items())
        )
        tariffs.append(
            Tariff(
                id=tier,  # стабильный псевдо-id для кнопок
                name=name,
                description="",
                tier=tier,
                traffic_label="♾️ Бесконечность ГБ" if unlimited else f"{gb} ГБ",
                unlimited_traffic=unlimited,
                traffic_limit_gb=0 if unlimited else gb,
                device_limit=devices,
                extra_device_kopeks=5000,
                servers=("Белые списки RU",) if unlimited else (),
                periods=periods,
            )
        )
    return Catalog(
        tariffs=tuple(tariffs),
        min_topup_kopeks=min_topup_kopeks,
        quick_amounts_kopeks=quick_amounts_kopeks,
        referral_percent=referral_percent,
    )


async def fetch_catalog(settings: Settings, bedolaga_user_id: int) -> Catalog | None:
    """
    Тарифы из кабинета с ценами мини-приложения.

    Если API недоступен — отдаём fallback с теми же «от 49 ₽»,
    чтобы экран не пустел и цифры не расходились.
    """
    global _cache

    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    fallback = miniapp_fallback_catalog()

    if not settings.cabinet_jwt_secret:
        _cache = (now, fallback)
        return fallback

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
        logger.warning("не удалось получить тарифы: %s — fallback мини-приложения", exc)
        _cache = (now, fallback)
        return fallback

    if not tariffs:
        _cache = (now, fallback)
        return fallback

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
        quick_amounts_kopeks=quick or fallback.quick_amounts_kopeks,
        referral_percent=percent,
    )
    _cache = (now, catalog)
    return catalog


def reset_cache() -> None:
    global _cache
    _cache = None
