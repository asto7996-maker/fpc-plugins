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
SERVICE_ID_LINE_RE = re.compile(
    r"^\s*(?:🆔\s*)?(?:ID|Service\s*ID|VexBoost\s*ID)\s*[:=\-\s#]+\s*\d+\s*$",
    re.IGNORECASE | re.MULTILINE,
)
QUAN_LINE_RE = re.compile(
    r"^\s*#?\s*Quan(?:tity)?\s*[:=\-\s#]+\s*\d+\s*$",
    re.IGNORECASE | re.MULTILINE,
)
INLINE_ID_RE = re.compile(
    r"(?:🆔\s*)?(?:ID|Service\s*ID|VexBoost\s*ID)\s*[:=\-\s#]+\s*\d+",
    re.IGNORECASE,
)
INLINE_QUAN_RE = re.compile(r"#?\s*Quan(?:tity)?\s*[:=\-\s#]+\s*\d+", re.IGNORECASE)

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

DEFAULT_EXECUTION_TIME = "Время доставки: от 10 минут до двух дней."

DEFAULT_AUTO_DELIVERY_DESC = (
    "🤖 Автоматическая доставка через SMM-бот VexBoost: "
    "после оплаты отправьте ссылку — робот выполнит заказ автоматически."
)

QUANTITY_LINE_RE = re.compile(
    r"^\s*(?:📦\s*)?(?:Мин\.?\s*заказ|Min\.?\s*order|Количество|Quantity)\s*[:.\-].*$",
    re.IGNORECASE | re.MULTILINE,
)
AUTO_DELIVERY_LINE_RE = re.compile(
    r"^\s*🤖\s*Автоматическая доставка.*$",
    re.IGNORECASE | re.MULTILINE,
)
EXECUTION_TIME_LINE_RE = re.compile(
    r"^\s*(?:⏱\s*)?(?:Время доставки|Срок выполнения).*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class ParsedLot:
    source_url: str
    offer_id: str
    title: str = ""
    brief_ru: str = ""
    full_ru: str = ""
    price_hint: str = ""
    funpay_node_id: int = 0
    funpay_category_title: str = ""
    funpay_category_kind: str = "lots"  # lots | chips
    funpay_service_type: str = ""
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
    """Удаляет строки и вхождения VexBoost ID / #Quan из текста."""
    if not text:
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if SERVICE_ID_LINE_RE.match(stripped) or SERVICE_ID_RE.match(stripped):
            continue
        if QUAN_LINE_RE.match(stripped):
            continue
        cleaned = INLINE_QUAN_RE.sub("", INLINE_ID_RE.sub("", line))
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if cleaned:
            lines.append(cleaned)
    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def _norm_compare(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def dedupe_brief_from_full(brief: str, full: str) -> str:
    """Убирает краткое описание из начала подробного (FunPay часто дублирует)."""
    brief = (brief or "").strip()
    full = (full or "").strip()
    if not full:
        return ""
    if not brief:
        return full
    if _norm_compare(full) == _norm_compare(brief):
        return ""
    if full.startswith(brief):
        rest = full[len(brief):].lstrip(" \n\r\t:—–-")
        return rest.strip()
    brief_first_line = brief.split("\n", 1)[0].strip()
    if brief_first_line and full.startswith(brief_first_line):
        rest = full[len(brief_first_line):].lstrip(" \n\r\t:—–-")
        if rest.strip() and _norm_compare(rest) != _norm_compare(brief):
            return rest.strip()
    return full


def format_quantity_limits(min_qty: int, max_qty: int) -> str:
    """Строка min/max для описания лота."""
    min_qty = max(1, int(min_qty))
    max_qty = max(min_qty, int(max_qty))
    return f"📦 Количество: от {min_qty} до {max_qty} шт."


def strip_smm_meta_lines(text: str) -> str:
    """Удаляет старые строки авто-доставки, сроков и min/max из описания."""
    if not text:
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if (
            QUANTITY_LINE_RE.match(stripped)
            or AUTO_DELIVERY_LINE_RE.match(stripped)
            or EXECUTION_TIME_LINE_RE.match(stripped)
        ):
            continue
        lines.append(line)
    result = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _append_unique_block(text: str, block: str) -> str:
    block = (block or "").strip()
    if not block or block in text:
        return text
    base = (text or "").rstrip()
    return f"{base}\n\n{block}".strip() if base else block


def apply_service_id(text: str, service_id: int) -> str:
    """Удаляет старые ID и добавляет актуальный VexBoost ID в конец."""
    cleaned = strip_service_id(text or "")
    id_line = f"ID: {service_id}"
    if cleaned and id_line in cleaned:
        return cleaned
    if cleaned:
        return f"{cleaned.rstrip()}\n\n{id_line}".strip()
    return id_line


def _truncate_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def compose_starvell_descriptions(
    lot: ParsedLot,
    *,
    is_smm: bool = False,
    service_id: int | None = None,
    min_qty: int | None = None,
    max_qty: int | None = None,
    include_auto_delivery: bool = True,
    brief_max: int = 100,
    full_max: int = 4800,
) -> dict[str, str]:
    """
    Краткое и подробное описание для Starvell:
    - краткое ≠ подробное (без дубля в начале full)
    - старый ID удаляется, новый service_id добавляется в full
    - SMM: авто-доставка (бот), min/max, срок доставки
    """
    raw_brief = strip_smm_meta_lines(strip_service_id((lot.brief_ru or lot.title or "").strip()))
    raw_full = strip_smm_meta_lines(strip_service_id((lot.full_ru or "").strip()))

    full_ru = dedupe_brief_from_full(raw_brief, raw_full)
    if not full_ru and raw_full and _norm_compare(raw_full) != _norm_compare(raw_brief):
        full_ru = raw_full

    brief_ru = _truncate_text(raw_brief, brief_max)
    full_ru = dedupe_brief_from_full(brief_ru, full_ru)

    if is_smm:
        if include_auto_delivery:
            full_ru = _append_unique_block(full_ru, DEFAULT_AUTO_DELIVERY_DESC)
        if min_qty and max_qty:
            full_ru = _append_unique_block(full_ru, format_quantity_limits(min_qty, max_qty))
        full_ru = _append_unique_block(full_ru, DEFAULT_EXECUTION_TIME)
        if service_id:
            full_ru = apply_service_id(full_ru, int(service_id))
        brief_ru = strip_service_id(strip_smm_meta_lines(brief_ru))
        if _norm_compare(full_ru) == _norm_compare(brief_ru):
            full_ru = "\n\n".join(filter(None, [
                DEFAULT_AUTO_DELIVERY_DESC if include_auto_delivery else "",
                format_quantity_limits(min_qty, max_qty) if min_qty and max_qty else "",
                DEFAULT_EXECUTION_TIME,
                f"ID: {service_id}" if service_id else "",
            ])).strip()

    return {
        "brief_ru": brief_ru,
        "full_ru": _truncate_text(full_ru, full_max),
    }


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
    desc = compose_starvell_descriptions(lot, is_smm=is_smm, service_id=service_id)
    sections: dict[str, str] = {
        "title": lot.title,
        "brief_ru": desc["brief_ru"],
        "full_ru": desc["full_ru"],
    }
    if is_smm:
        sections["after_payment"] = DEFAULT_SMM_AFTER_PAYMENT
        sections["execution_time"] = DEFAULT_EXECUTION_TIME
        sections["auto_delivery_note"] = DEFAULT_AUTO_DELIVERY_DESC
    elif auto_delivery:
        sections["auto_delivery_note"] = DEFAULT_AUTO_DELIVERY_DESC
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
    ]
    if sections.get("after_payment"):
        lines += ["", "💬 <b>Сообщение после оплаты:</b>", sections["after_payment"]]
    if sections.get("execution_time"):
        lines += ["", sections["execution_time"]]
    if sections.get("auto_delivery_note"):
        lines += ["", sections["auto_delivery_note"]]
    lines += ["", "<i>Скопируйте блоки в карточку лота на Starvell.</i>"]
    return "\n".join(lines)


def split_telegram_messages(text: str, limit: int = 4000) -> list[str]:
    """Разбивает длинный HTML-текст на части для Telegram."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks


async def send_copy_sections(message, sections: dict[str, str], *, reply_markup=None) -> None:
    """Отправляет результат парсера (несколько сообщений при необходимости)."""
    full = format_copy_message(sections)
    parts = split_telegram_messages(full)
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        await message.answer(
            part,
            parse_mode="HTML",
            reply_markup=reply_markup if is_last else None,
        )


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
    if lot.funpay_node_id:
        await _enrich_funpay_category(lot)
    if not lot.title:
        lot.errors.append("Не удалось извлечь название — проверьте ссылку")
    combined = f"{lot.brief_ru}\n{lot.full_ru}"
    lot.is_smm_guess = guess_smm(lot.title, combined)
    return lot


_CATEGORY_LINK_RE = re.compile(r"/(lots|chips)/(\d+)/?$", re.IGNORECASE)


def _parse_offer_fields(soup) -> tuple[str, str, str]:
    """Извлекает краткое/подробное описание и тип услуги из карточки FunPay."""
    lot_brief = ""
    lot_full = ""
    lot_service_type = ""
    for item in soup.select(".param-item"):
        heading = item.find("h5")
        if not heading:
            continue
        label = heading.get_text(strip=True).lower()
        body = item.find("div")
        if not body:
            continue
        text = body.get_text("\n", strip=True)
        if not text:
            continue
        if "краткое описание" in label:
            lot_brief = text
        elif "подробное описание" in label:
            lot_full = text
        elif label in ("тип услуги", "тип товара", "тип"):
            lot_service_type = text
    return lot_brief, lot_full, lot_service_type


def _parse_category_from_html(html: str, lot: ParsedLot) -> None:
    for href, title in re.findall(
        r'href="((?:https://funpay\.com)?/(?:lots|chips)/\d+/?)"[^>]*>([^<]+)</a>',
        html,
        re.IGNORECASE,
    ):
        path = href.replace("https://funpay.com", "")
        m = _CATEGORY_LINK_RE.search(path)
        if not m or "offer" in path.lower():
            continue
        kind, node_id = m.group(1).lower(), int(m.group(2))
        name = re.sub(r"\s+", " ", title).strip()
        if name and name.lower() not in ("funpay", "english", "войти"):
            lot.funpay_category_kind = kind
            lot.funpay_node_id = node_id
            lot.funpay_category_title = name
            return

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if "offer" in href:
                continue
            m = _CATEGORY_LINK_RE.search(href.replace("https://funpay.com", ""))
            if not m:
                continue
            name = a.get_text(" ", strip=True)
            if not name or len(name) < 2:
                continue
            lot.funpay_category_kind = m.group(1).lower()
            lot.funpay_node_id = int(m.group(2))
            lot.funpay_category_title = name
            return
        content = soup.select_one("#content")
        if content and not lot.funpay_category_title:
            h1 = content.find("h1")
            if h1:
                prev = h1.find_previous(string=True)
                if prev and isinstance(prev, str):
                    text = prev.strip()
                    if text and text != h1.get_text(strip=True):
                        lot.funpay_category_title = text
    except Exception as exc:
        logger.debug("category parse bs4: %s", exc)


async def _enrich_funpay_category(lot: ParsedLot) -> None:
    if not lot.funpay_node_id:
        return
    kind = lot.funpay_category_kind or "lots"
    url = f"https://funpay.com/{kind}/{lot.funpay_node_id}/"
    try:
        async with httpx.AsyncClient(
            headers=FUNPAY_HEADERS,
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.debug("funpay category page %s: %s", url, exc)
        return
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if m:
        lot.funpay_category_title = re.sub(r"\s+", " ", m.group(1)).strip()
    _parse_listing_price(html, lot)


def _parse_listing_price(html: str, lot: ParsedLot) -> None:
    """Цена лота из таблицы категории FunPay (атрибут data-s — полная точность)."""
    if not lot.offer_id:
        return
    oid = re.escape(str(lot.offer_id))
    pattern = rf'href="[^"]*offer\?id={oid}"[^>]*>.*?class="tc-price"[^>]*data-s="([\d.]+)"'
    m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if m:
        lot.price_hint = m.group(1)
        return
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select(f'a[href*="offer?id={lot.offer_id}"]'):
            price_el = a.select_one(".tc-price")
            if not price_el:
                continue
            raw = price_el.get("data-s")
            if raw:
                lot.price_hint = str(raw).strip()
                return
            text = price_el.get_text(" ", strip=True)
            if text:
                lot.price_hint = text
                return
    except Exception as exc:
        logger.debug("listing price bs4: %s", exc)


def _parse_html(html: str, lot: ParsedLot) -> None:
    _parse_category_from_html(html, lot)
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
        except Exception:
            continue

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        brief, full, service_type = _parse_offer_fields(soup)
        if brief:
            lot.brief_ru = brief
            if not lot.title or lot.title in ("Оформление заказа", "Offer", ""):
                lot.title = brief[:100]
        if full:
            lot.full_ru = full
        if service_type:
            lot.funpay_service_type = service_type
        if not lot.brief_ru and lot.full_ru:
            lot.brief_ru = lot.full_ru[:200] + ("…" if len(lot.full_ru) > 200 else "")

        h1 = soup.find("h1")
        if h1 and not brief:
            lot.title = h1.get_text(strip=True)
        if not lot.full_ru:
            desc = soup.select_one(".desc, #offer-desc, .offer-desc-text, .page-content")
            if desc:
                text = desc.get_text("\n", strip=True)
                lot.full_ru = text
                lot.brief_ru = text[:200] + ("…" if len(text) > 200 else "")
        price = soup.select_one(".price, .tc-price, .payment-summary")
        if price:
            lot.price_hint = price.get_text(strip=True)
    except ImportError:
        lot.errors.append("BeautifulSoup не установлен — парсинг упрощённый")
        _parse_regex(html, lot)
    except Exception as exc:
        logger.debug("bs4 parse: %s", exc)
        _parse_regex(html, lot)

    if not lot.brief_ru and lot.full_ru:
        lot.brief_ru = lot.full_ru[:200] + ("…" if len(lot.full_ru) > 200 else "")


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
