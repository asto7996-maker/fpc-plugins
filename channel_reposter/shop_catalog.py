"""
shop_catalog.py — актуальные тарифы и цены из @sweetshopxxx_bot.

Юзербот открывает «🍭Tariffs 🎀», кликает карточку resource/view,
затем каждый срок resource/tariff и читает «💲 Цена: … ₽».
Кнопку «Оплатить» не нажимает.

Официальный pyrogram 2.0.106 карточки не читает (MessageMediaUnsupported).
Нужен pyrofork (тот же import pyrogram).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import config

logger = logging.getLogger(__name__)

TARIFFS_BUTTON = "🍭Tariffs 🎀"
SKIP_BUTTONS = frozenset({"закрыть", "close", "назад", "back", "отмена"})
PAY_PREFIXES = ("pay/", "pay/main")

# Бессрочный VIP ALL-IN (кнопка INFINITY). > MAX_DURATION_DAYS свободного ввода.
INFINITY_DAYS = 3650

SLEEP_LIST = 2.2
SLEEP_CARD = 3.0
SLEEP_PRICE = 3.2

# Стабильные короткие подписи для кнопок (id из магазина)
SHORT_BY_ID = {
    "19063": "VIP ALL-IN",
    "19065": "MILF Exclusive",
    "19066": "OnlyFans Альтушки",
    "19070": "BDSM Archive",
    "19071": "Trash Archive",
    "19072": "Caramel Сливы",
    "19073": "Вписки Archive",
}

FALLBACK_PERIODS: list[tuple[int, str]] = [
    (1, "1 день"),
    (7, "7 дней"),
    (30, "1 месяц"),
    (90, "3 месяца"),
    (180, "6 месяцев"),
    (365, "1 год"),
]

_HAIR = "\u200a\u200b\u200c\u200d\u2060\u00a0\u3000\ufeff"


@dataclass
class Period:
    days: int
    price: float
    label: str


@dataclass
class Tariff:
    shop_id: str
    title: str
    short_name: str
    callback: str = ""
    periods: list[Period] = field(default_factory=list)

    def period_buttons(self) -> list[tuple[int, str, float]]:
        if self.periods:
            return [
                (p.days, period_button_text(p), p.price) for p in self.periods
            ]
        return [(d, label, 0.0) for d, label in FALLBACK_PERIODS]


def shop_title_plain(text: str) -> str:
    """Снять декоративный юникод с кнопок магазина."""
    raw = "".join(ch for ch in (text or "") if ch not in _HAIR)
    raw = unicodedata.normalize("NFKC", raw)
    raw = re.sub(r"[\u02d8-\u02ff˗ˏˋˎˊ]+", " ", raw)
    raw = re.sub(r"[❮❯〚〛⟦⟧〘〙⦗⦘﹛﹜〈〉《》]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def short_name_for(shop_id: str, title: str) -> str:
    if shop_id in SHORT_BY_ID:
        return SHORT_BY_ID[shop_id]
    plain = shop_title_plain(title)
    plain = re.sub(r"[^\w\s:+\-А-Яа-яЁё]", " ", plain, flags=re.UNICODE)
    plain = re.sub(r"\s+", " ", plain).strip()
    return (plain or title)[:40]


def shop_id_from_callback(data: str) -> str:
    raw = data or ""
    if "id=" in raw:
        qs = parse_qs(urlparse("http://x/?" + raw.split("?", 1)[-1]).query)
        if qs.get("r_id"):
            return str(qs["r_id"][0])
        if qs.get("id"):
            return str(qs["id"][0])
        m = re.search(r"(?:r_id|id)=(\d+)", raw)
        if m:
            return m.group(1)
    return raw


def format_rub(price: float) -> str:
    if price <= 0:
        return ""
    if abs(price - round(price)) < 1e-6:
        n = int(round(price))
        grouped = f"{n:,}".replace(",", " ")
        return f"{grouped} ₽"
    return f"{price:.2f} ₽"


def is_infinity_days(days: int, label: str = "") -> bool:
    if days >= INFINITY_DAYS:
        return True
    return "INFINITY" in (label or "").upper()


def format_duration_label(days: int, label: str = "") -> str:
    if is_infinity_days(days, label):
        return "INFINITY / бессрочно"
    raw = (label or "").strip()
    if raw and "₽" not in raw:
        return raw
    if days % 30 == 0 and days >= 30:
        months = days // 30
        if months == 1:
            return "1 мес"
        return f"{months} мес"
    return f"{days} д."


def period_button_text(period: Period) -> str:
    label = (period.label or "").strip() or f"{period.days} дн"
    if period.price > 0 and "₽" not in label:
        return f"{label} — {format_rub(period.price)}"
    return label


def nice_period_label(days: int, raw: str) -> str:
    if is_infinity_days(days, raw):
        return "INFINITY"
    plain = shop_title_plain(raw)
    m = re.search(r"(\d+)\s*месяц", plain, re.I)
    if m:
        n = int(m.group(1))
        return "1 мес" if n == 1 else f"{n} мес"
    m = re.search(r"(\d+)\s*(день|дня|дней|дн)\b", plain, re.I)
    if m:
        n = int(m.group(1))
        if n == 1:
            return "1 день"
        if n in {2, 3, 4}:
            return f"{n} дня"
        return f"{n} дней"
    m = re.search(r"(\d+)\s*год", plain, re.I)
    if m:
        n = int(m.group(1))
        return "1 год" if n == 1 else f"{n} г."
    if days == 7:
        return "7 дней"
    if days == 30:
        return "1 мес"
    if days == 360:
        return "12 мес"
    if days == 365:
        return "1 год"
    return f"{days} дн"


def parse_shop_price_text(text: str) -> Optional[float]:
    """Разобрать «💲 Цена: 1 490 ₽» со страницы оплаты магазина."""
    raw = (text or "").replace("\u00a0", " ").replace("\u202f", " ")
    m = re.search(
        r"Цена\s*[:\-–]?\s*(\d{1,3}(?:\s\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:₽|руб)",
        raw,
        re.I,
    )
    if not m:
        m = re.search(
            r"[💲💰]\s*(\d{1,3}(?:\s\d{3})+|\d+)\s*(?:₽|руб)",
            raw,
            re.I,
        )
    if not m:
        return None
    num = m.group(1).replace(" ", "").replace(",", ".")
    try:
        price = float(num)
    except ValueError:
        return None
    return price if price > 0 else None


def parse_period_label(text: str) -> Optional[Period]:
    """Разобрать кнопку вида «30 дней — 1 490₽» или «Тариф на 7 дней»."""
    plain = shop_title_plain(text)
    if not plain or plain.lower() in SKIP_BUTTONS:
        return None
    up = plain.upper()
    if "INFINITY" in up or "БЕССРОЧ" in up or "НАВСЕГДА" in up or "UNLIMITED" in up:
        price = _price_from_plain(plain)
        return Period(days=INFINITY_DAYS, price=price, label="INFINITY")
    days = 0
    m = re.search(
        r"(\d+)\s*(день|дня|дней|дн|д|недел[яиь]?|мес(?:яц(?:а|ев)?)?|год(?:а)?|лет)\b",
        plain,
        re.I,
    )
    if m:
        n = int(m.group(1))
        u = m.group(2).lower()
        if u.startswith("нед"):
            days = n * 7
        elif u.startswith("мес"):
            days = n * 30
        elif u.startswith("год") or u == "лет":
            days = n * 365
        else:
            days = n
    elif re.fullmatch(r"\d+", plain):
        days = int(plain)
    if days < 1:
        return None
    price = _price_from_plain(plain)
    return Period(days=days, price=price, label=nice_period_label(days, plain))


def parse_tariff_period_label(text: str) -> Optional[Period]:
    """Кнопка срока на карточке: «Тариф на 7 дней», «INFINITY»."""
    return parse_period_label(text)


def _price_from_plain(plain: str) -> float:
    pm = re.search(
        r"(\d{1,3}(?:\s\d{3})+|\d+)\s*(?:₽|руб\.?|rur|rub)",
        plain,
        re.I,
    )
    if not pm:
        return 0.0
    try:
        return float(pm.group(1).replace(" ", ""))
    except ValueError:
        return 0.0


def tariffs_from_inline_rows(rows: list[list[dict[str, Any]]]) -> list[Tariff]:
    out: list[Tariff] = []
    seen: set[str] = set()
    for row in rows:
        for btn in row:
            text = str(btn.get("text") or "")
            data = str(btn.get("data") or "")
            if not data.startswith("resource/view"):
                continue
            sid = shop_id_from_callback(data)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            title = shop_title_plain(text) or text
            out.append(
                Tariff(
                    shop_id=sid,
                    title=title,
                    short_name=short_name_for(sid, text),
                    callback=data,
                )
            )
    return out


def match_tariff(user_text: str, catalog: list[Tariff]) -> Optional[Tariff]:
    needle = shop_title_plain(user_text).lower()
    if not needle:
        return None
    for t in catalog:
        aliases = {t.short_name.lower(), t.title.lower(), t.shop_id}
        if needle in aliases:
            return t
        if needle == shop_title_plain(t.short_name).lower():
            return t
        if t.short_name.lower() in needle or needle in t.short_name.lower():
            return t
    return None


def match_period(
    user_text: str, tariff: Tariff
) -> Optional[tuple[int, float, str]]:
    needle = shop_title_plain(user_text)
    needle_l = needle.lower()
    for days, label, price in tariff.period_buttons():
        lab = shop_title_plain(label).lower()
        if lab == needle_l:
            return days, price, label
        lab_no_price = re.sub(r"\s*[—\-]\s*.*₽.*$", "", lab).strip()
        if lab_no_price and lab_no_price == needle_l:
            return days, price, label
        try:
            if __import__("lobby").parse_tariff_duration(needle) == days:
                return days, price, label
        except ValueError:
            continue
    return None


def catalog_from_db_rows(rows: list[dict[str, Any]]) -> list[Tariff]:
    out: list[Tariff] = []
    for row in rows:
        periods: list[Period] = []
        try:
            extra = json.loads(row.get("extra_json") or "{}")
        except json.JSONDecodeError:
            extra = {}
        for item in extra.get("periods") or []:
            try:
                periods.append(
                    Period(
                        days=int(item["days"]),
                        price=float(item.get("price") or 0),
                        label=str(item.get("label") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        out.append(
            Tariff(
                shop_id=str(row.get("shop_id") or ""),
                title=str(row.get("title") or ""),
                short_name=str(row.get("short_name") or ""),
                periods=periods,
            )
        )
    return out


def tariffs_to_db_rows(tariffs: list[Tariff]) -> list[dict[str, Any]]:
    rows = []
    for i, t in enumerate(tariffs):
        extra = {
            "periods": [
                {"days": p.days, "price": p.price, "label": p.label} for p in t.periods
            ],
            "callback": t.callback,
            "price_source": "shop_bot",
        }
        rows.append(
            {
                "shop_id": t.shop_id,
                "title": t.title,
                "short_name": t.short_name,
                "sort_order": i,
                "extra_json": json.dumps(extra, ensure_ascii=False),
            }
        )
    return rows


def catalog_prices_summary(tariffs: list[Tariff]) -> str:
    lines: list[str] = []
    for t in tariffs:
        if not t.periods:
            lines.append(f"• {t.short_name}: цены не получены")
            continue
        bits = []
        for p in t.periods:
            if p.price > 0:
                bits.append(f"{p.label} — {format_rub(p.price)}")
            else:
                bits.append(f"{p.label} — нет цены")
        lines.append(f"• {t.short_name}: " + "; ".join(bits))
    return "\n".join(lines)


def _inline_rows(message: Any) -> list[list[dict[str, Any]]]:
    rm = getattr(message, "reply_markup", None)
    ik = getattr(rm, "inline_keyboard", None) if rm else None
    if not ik:
        return []
    rows = []
    for row in ik:
        rows.append(
            [
                {
                    "text": getattr(b, "text", "") or "",
                    "data": getattr(b, "callback_data", None) or "",
                }
                for b in row
            ]
        )
    return rows


def _message_plain(message: Any) -> str:
    parts = [
        getattr(message, "text", None) or "",
        getattr(message, "caption", None) or "",
    ]
    return "\n".join(p for p in parts if p)


def _callback_data_list(message: Any) -> list[str]:
    out: list[str] = []
    for row in _inline_rows(message):
        for b in row:
            data = str(b.get("data") or "")
            if data:
                out.append(data)
    return out


def _is_pay_callback(data: str) -> bool:
    raw = (data or "").strip().lower()
    return raw.startswith(PAY_PREFIXES) or raw.startswith("pay")


def _tariff_period_buttons(message: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in _inline_rows(message):
        for b in row:
            data = str(b.get("data") or "")
            if not data.startswith("resource/tariff"):
                continue
            if _is_pay_callback(data):
                continue
            out.append({"text": str(b.get("text") or ""), "data": data})
    return out


async def _history(client: Any, chat: str, limit: int = 12) -> list[Any]:
    msgs: list[Any] = []
    async for m in client.get_chat_history(chat, limit=limit):
        msgs.append(m)
    return msgs


async def _find_tariff_list_message(client: Any, chat: str) -> Any:
    for m in await _history(client, chat, 15):
        if any(d.startswith("resource/view") for d in _callback_data_list(m)):
            return m
    return None


async def _find_resource_card(client: Any, chat: str) -> Any:
    for m in await _history(client, chat, 12):
        if _tariff_period_buttons(m):
            return m
    return None


async def _find_price_message(client: Any, chat: str) -> Any:
    for m in await _history(client, chat, 10):
        if parse_shop_price_text(_message_plain(m)):
            return m
    return None


async def _quiet_callback(client: Any, chat: str, msg_id: int, data: str) -> None:
    if _is_pay_callback(data):
        logger.error("отказ кликать оплату магазина: %s", data)
        return
    try:
        await asyncio.wait_for(
            client.request_callback_answer(chat, msg_id, data),
            timeout=1.2,
        )
    except Exception:
        logger.debug("shop callback %s skipped", data, exc_info=True)


async def _open_tariff_list(client: Any, chat: str) -> Any:
    await client.send_message(chat, TARIFFS_BUTTON)
    await asyncio.sleep(SLEEP_LIST)
    list_msg = await _find_tariff_list_message(client, chat)
    if list_msg is None:
        await client.send_message(chat, "/start")
        await asyncio.sleep(1.5)
        await client.send_message(chat, TARIFFS_BUTTON)
        await asyncio.sleep(SLEEP_LIST)
        list_msg = await _find_tariff_list_message(client, chat)
    return list_msg


async def _open_resource_card(client: Any, chat: str, view_data: str) -> Any:
    list_msg = await _open_tariff_list(client, chat)
    if list_msg is None:
        return None
    await _quiet_callback(client, chat, list_msg.id, view_data)
    await asyncio.sleep(SLEEP_CARD)
    return await _find_resource_card(client, chat)


async def _read_checkout_price(
    client: Any, chat: str, card: Any, tariff_data: str
) -> float:
    await _quiet_callback(client, chat, card.id, tariff_data)
    await asyncio.sleep(SLEEP_PRICE)
    price_msg = await _find_price_message(client, chat)
    if price_msg is None:
        return 0.0
    return parse_shop_price_text(_message_plain(price_msg)) or 0.0


async def _fill_prices_for_tariff(client: Any, chat: str, tariff: Tariff) -> None:
    if not tariff.callback:
        return
    card = await _open_resource_card(client, chat, tariff.callback)
    if card is None:
        logger.warning("нет карточки тарифа %s", tariff.short_name)
        return
    period_btns = _tariff_period_buttons(card)
    if not period_btns:
        logger.warning("у %s нет кнопок resource/tariff", tariff.short_name)
        return

    parsed: list[Period] = []
    for i, btn in enumerate(period_btns):
        if i > 0:
            card = await _open_resource_card(client, chat, tariff.callback)
            if card is None:
                logger.warning("не открылась карточка %s для следующего срока", tariff.short_name)
                break
            # После повторного открытия порядок кнопок тот же — берём i-ю
            fresh = _tariff_period_buttons(card)
            if i < len(fresh):
                btn = fresh[i]
        data = btn["data"]
        period = parse_tariff_period_label(btn["text"])
        if period is None:
            logger.warning(
                "не разобрали срок «%s» у %s", btn["text"], tariff.short_name
            )
            continue
        price = await _read_checkout_price(client, chat, card, data)
        period.price = price
        if price <= 0:
            logger.warning("нет цены у %s / %s", tariff.short_name, period.label)
        parsed.append(period)
    tariff.periods = parsed


async def fetch_shop_catalog(client: Any) -> list[Tariff]:
    """Сходить в магазин-бот и забрать тарифы вместе с ценами."""
    chat = (config.SHOP_BOT_USERNAME or "sweetshopxxx_bot").lstrip("@")
    list_msg = await _open_tariff_list(client, chat)
    if list_msg is None:
        raise RuntimeError("магазин не отдал список тарифов")

    tariffs = tariffs_from_inline_rows(_inline_rows(list_msg))
    if not tariffs:
        raise RuntimeError("в списке тарифов нет подписок")

    for t in tariffs:
        try:
            await _fill_prices_for_tariff(client, chat, t)
        except Exception:
            logger.exception("цены для %s", t.short_name)

    priced = sum(1 for t in tariffs for p in t.periods if p.price > 0)
    summary = catalog_prices_summary(tariffs)
    logger.info("Каталог магазина (%s тарифов, %s цен):\n%s", len(tariffs), priced, summary)
    if priced == 0:
        raise RuntimeError(
            "магазин не отдал цены тарифов — карточки пустые. "
            "Нужен pyrofork, не официальный pyrogram 2.0.106"
        )
    return tariffs
