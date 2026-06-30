"""Парсинг лотов FunPay для копирования на Starvell."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger("starvell.funpay_parser")

FUNPAY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml",
}

OFFER_ID_RE = re.compile(
    r"(?:funpay\.com/(?:lots|chips)/(?:offer|\d+)(?:\?[^#\s]*id=(\d+)|/(\d+)))",
    re.IGNORECASE,
)
SERVICE_ID_RE = re.compile(r"^\s*ID\s*[:=\-]\s*\d+\s*$", re.IGNORECASE | re.MULTILINE)

SMM_KEYWORDS = (
    "накрут", "подписчик", "лайк", "просмотр", "реакци", "smm", "boost",
    "followers", "views", "subscriber", "teletype", "продвижен", "охват",
    "instagram", "tiktok", "telegram", "youtube", "vk.com",
)

DEFAULT_SMM_AFTER_PAYMENT = (
    "✅ Заказ принят в работу!\n\n"
    "(Даже если я офлайн, автом. бот уже активен. Спасибо за ожидание!)\n\n"
    "🚀 Для запуска отправьте ссылку:\n"
    "Пришлите URL-адрес формата https://... на открытый и активный объект.\n"
    "Пример: https://teletype.in/@alibyways/lZc68lnw5SE\n\n"
    "⚠️ Важные условия:\n"
    "Не меняйте ссылку и не закрывайте профиль до финала.\n"
    "Не заказывайте продвижение в других сервисах одновременно.\n"
    "Сроки: от 0 до 48 часов (зависит от нагрузки).\n\n"
    "🤖 После отправки корректной ссылки робот начнет выполнение автоматически. "
    "Ожидайте уведомления!"
)

DEFAULT_EXECUTION_TIME = "⏱ Срок выполнения: от 5 минут до 2 суток."


@dataclass
class ParsedLot:
    source_url: str
    offer_id: str
    title: str = ""
    brief_ru: str = ""
    full_ru: str = ""
    brief_en: str = ""
    full_en: str = ""
    price_hint: str = ""
    is_smm_guess: bool = False
    raw_html_len: int = 0
    errors: list[str] = field(default_factory=list)


def extract_offer_id(url: str) -> str | None:
    text = (url or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if "funpay.com" not in (parsed.netloc or "").lower():
        return None
    qs = parse_qs(parsed.query)
    if qs.get("id"):
        return str(qs["id"][0]).strip()
    m = OFFER_ID_RE.search(text)
    if m:
        return m.group(1) or m.group(2)
    parts = [p for p in parsed.path.split("/") if p.isdigit()]
    if parts:
        return parts[-1]
    return None


def strip_service_id(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        if SERVICE_ID_RE.match(line.strip()):
            continue
        if re.search(r"\bID\s*[:=\-]\s*\d+", line, re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def guess_smm(title: str, description: str) -> bool:
    blob = f"{title}\n{description}".lower()
    return any(k in blob for k in SMM_KEYWORDS)


def build_starvell_package(
    lot: ParsedLot,
    *,
    is_smm: bool,
    service_id: int | None = None,
    auto_delivery: bool = False,
) -> dict[str, str]:
    brief = strip_service_id(lot.brief_ru or lot.title)
    full = strip_service_id(lot.full_ru or brief)
    if is_smm and service_id:
        id_line = f"ID: {service_id}"
        full = f"{full.rstrip()}\n\n{id_line}".strip()
    sections: dict[str, str] = {
        "title": lot.title,
        "brief_ru": brief,
        "full_ru": full,
        "brief_en": lot.brief_en or brief,
        "full_en": lot.full_en or full,
    }
    if is_smm:
        sections["after_payment"] = DEFAULT_SMM_AFTER_PAYMENT
        sections["execution_time"] = DEFAULT_EXECUTION_TIME
    if auto_delivery:
        sections["auto_delivery_note"] = (
            "✅ Включите «Автоматическая выдача» в настройках лота на Starvell."
        )
    return sections


def format_copy_message(sections: dict[str, str]) -> str:
    lines = [
        "📋 <b>Лот готов к публикации на Starvell</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📌 <b>Название:</b>\n{sections.get('title', '—')}",
        "",
        "🇷🇺 <b>Краткое описание:</b>",
        f"<code>{_esc(sections.get('brief_ru', ''))}</code>",
        "",
        "🇷🇺 <b>Подробное описание:</b>",
        f"<code>{_esc(sections.get('full_ru', ''))}</code>",
        "",
        "🇬🇧 <b>Краткое (EN):</b>",
        f"<code>{_esc(sections.get('brief_en', ''))}</code>",
        "",
        "🇬🇧 <b>Подробное (EN):</b>",
        f"<code>{_esc(sections.get('full_en', ''))}</code>",
    ]
    if sections.get("after_payment"):
        lines += ["", "💬 <b>Сообщение после оплаты:</b>", sections["after_payment"]]
    if sections.get("execution_time"):
        lines += ["", sections["execution_time"]]
    if sections.get("auto_delivery_note"):
        lines += ["", sections["auto_delivery_note"]]
    lines += ["", "<i>Скопируйте блоки в карточку лота на Starvell.</i>"]
    return "\n".join(lines)


def _esc(text: str) -> str:
    return (text or "").replace("<", "&lt;").replace(">", "&gt;")


async def fetch_funpay_lot(url: str) -> ParsedLot:
    offer_id = extract_offer_id(url)
    if not offer_id:
        return ParsedLot(source_url=url, offer_id="", errors=["Не удалось определить ID лота FunPay"])

    fetch_url = f"https://funpay.com/lots/offer?id={offer_id}"
    lot = ParsedLot(source_url=url, offer_id=offer_id)

    try:
        async with httpx.AsyncClient(
            headers=FUNPAY_HEADERS,
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            resp = await client.get(fetch_url)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        lot.errors.append(f"Ошибка загрузки FunPay: {exc}")
        return lot

    lot.raw_html_len = len(html)
    _parse_html(html, lot)
    if not lot.title:
        lot.errors.append("Не удалось извлечь название — проверьте ссылку")
    combined = f"{lot.brief_ru}\n{lot.full_ru}"
    lot.is_smm_guess = guess_smm(lot.title, combined)
    return lot


def _parse_html(html: str, lot: ParsedLot) -> None:
    # JSON bootstrap
    for pattern in (
        r"data-app-data=\"([^\"]+)\"",
        r"window\.__APP_DATA__\s*=\s*(\{.*?\});",
        r"JSON\.parse\(\"(.+?)\"\)",
    ):
        m = re.search(pattern, html, re.DOTALL)
        if not m:
            continue
        raw = m.group(1)
        try:
            raw = raw.encode().decode("unicode_escape") if "\\u" in raw else raw
            data = json.loads(raw.replace("&quot;", '"'))
            _fill_from_json(data, lot)
            if lot.title:
                return
        except Exception:
            continue

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if h1:
            lot.title = h1.get_text(strip=True)
        desc = soup.select_one(".desc, #offer-desc, .offer-desc-text, .page-content")
        if desc:
            text = desc.get_text("\n", strip=True)
            lot.full_ru = text
            lot.brief_ru = text[:200] + ("…" if len(text) > 200 else "")
        price = soup.select_one(".price, .tc-price")
        if price:
            lot.price_hint = price.get_text(strip=True)
    except ImportError:
        lot.errors.append("BeautifulSoup не установлен — парсинг упрощённый")
        _parse_regex(html, lot)
    except Exception as exc:
        logger.debug("bs4 parse: %s", exc)
        _parse_regex(html, lot)


def _parse_regex(html: str, lot: ParsedLot) -> None:
    if not lot.title:
        m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
        if m:
            lot.title = re.sub(r"\s+", " ", m.group(1)).strip()
    if not lot.full_ru:
        m = re.search(r'class="desc[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
        if m:
            text = re.sub(r"<[^>]+>", "\n", m.group(1))
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            lot.full_ru = text
            lot.brief_ru = text[:200]


def _fill_from_json(data: Any, lot: ParsedLot, depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(data, dict):
        for key in ("title", "name", "offerTitle", "lotTitle"):
            if key in data and isinstance(data[key], str) and not lot.title:
                lot.title = data[key].strip()
        for key in ("description", "desc", "fullDescription", "text"):
            if key in data and isinstance(data[key], str):
                val = data[key].strip()
                if len(val) > len(lot.full_ru):
                    lot.full_ru = val
        if "briefDescription" in data and isinstance(data["briefDescription"], str):
            lot.brief_ru = data["briefDescription"].strip()
        for val in data.values():
            _fill_from_json(val, lot, depth + 1)
    elif isinstance(data, list):
        for item in data[:30]:
            _fill_from_json(item, lot, depth + 1)

    if lot.full_ru and not lot.brief_ru:
        lot.brief_ru = lot.full_ru[:200] + ("…" if len(lot.full_ru) > 200 else "")
