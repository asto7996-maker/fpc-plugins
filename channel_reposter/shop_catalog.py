"""
shop_catalog.py — актуальные тарифы из @sweetshopxxx_bot.

Юзербот открывает «🍭Tariffs 🎀» и забирает список подписок.
Цены/сроки — из кнопок карточки тарифа, если магазин их отдаёт.
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
                (
                    p.days,
                    f"{p.days} д. — {p.price:g} ₽" if p.price else p.label,
                    p.price,
                )
                for p in self.periods
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
        if qs.get("id"):
            return str(qs["id"][0])
        m = re.search(r"id=(\d+)", raw)
        if m:
            return m.group(1)
    return raw


def parse_period_label(text: str) -> Optional[Period]:
    """Разобрать кнопку вида «30 дней — 1 490₽»."""
    plain = shop_title_plain(text)
    if not plain or plain.lower() in SKIP_BUTTONS:
        return None
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
    price = 0.0
    pm = re.search(
        r"(\d{1,3}(?:\s\d{3})+|\d+)\s*(?:₽|руб\.?|rur|rub)",
        plain,
        re.I,
    )
    if pm:
        try:
            price = float(pm.group(1).replace(" ", ""))
        except ValueError:
            price = 0.0
    return Period(days=days, price=price, label=plain)


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
    for days, label, price in tariff.period_buttons():
        if shop_title_plain(label).lower() == needle.lower():
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


async def _find_tariff_list_message(client: Any, chat: str) -> Any:
    async for m in client.get_chat_history(chat, limit=15):
        rows = _inline_rows(m)
        if any(
            str(b.get("data") or "").startswith("resource/view")
            for row in rows
            for b in row
        ):
            return m
    return None


async def _quiet_callback(client: Any, chat: str, msg_id: int, data: str) -> None:
    try:
        await asyncio.wait_for(
            client.request_callback_answer(chat, msg_id, data),
            timeout=1.2,
        )
    except Exception:
        logger.debug("shop callback %s skipped", data, exc_info=True)


async def fetch_shop_catalog(client: Any) -> list[Tariff]:
    """Сходить в магазин-бот и забрать список тарифов."""
    chat = (config.SHOP_BOT_USERNAME or "sweetshopxxx_bot").lstrip("@")
    await client.send_message(chat, TARIFFS_BUTTON)
    await asyncio.sleep(2.5)
    list_msg = await _find_tariff_list_message(client, chat)
    if list_msg is None:
        await client.send_message(chat, "/start")
        await asyncio.sleep(2)
        await client.send_message(chat, TARIFFS_BUTTON)
        await asyncio.sleep(2.5)
        list_msg = await _find_tariff_list_message(client, chat)
    if list_msg is None:
        raise RuntimeError("магазин не отдал список тарифов")

    tariffs = tariffs_from_inline_rows(_inline_rows(list_msg))
    if not tariffs:
        raise RuntimeError("в списке тарифов нет подписок")

    # Попробовать открыть карточки и снять сроки/цены
    for t in tariffs:
        if not t.callback:
            continue
        await _quiet_callback(client, chat, list_msg.id, t.callback)
        await asyncio.sleep(2.0)
        card = await client.get_messages(chat, list_msg.id)
        period_rows = _inline_rows(card)
        parsed: list[Period] = []
        for row in period_rows:
            for b in row:
                data = str(b.get("data") or "")
                if data.startswith("resource/view"):
                    continue
                period = parse_period_label(str(b.get("text") or ""))
                if period:
                    parsed.append(period)
        if parsed:
            t.periods = parsed
        await client.send_message(chat, TARIFFS_BUTTON)
        await asyncio.sleep(2.0)
        list_msg = await _find_tariff_list_message(client, chat) or list_msg

    logger.info("Каталог магазина: %s тарифов", len(tariffs))
    return tariffs
