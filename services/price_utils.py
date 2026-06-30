"""Нормализация и форматирование цен для парсера и Starvell API."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MIN_LOT_PRICE = Decimal("0.000001")
MAX_LOT_PRICE = Decimal("999999")
MAX_DECIMALS = 6

_PRICE_NUMBER_RE = re.compile(
    r"(\d+(?:[.,]\d{1,6})?|\.\d{1,6})"
)


def parse_price_hint(hint: str) -> str | None:
    """Извлекает цену из строки FunPay («0.039 ₽», «от 5.50», «0,001»)."""
    if not hint:
        return None
    text = hint.replace("\xa0", " ").replace("₽", " ").strip()
    m = _PRICE_NUMBER_RE.search(text)
    if not m:
        return None
    return normalize_price_input(m.group(1))


def normalize_price_input(raw: str) -> str | None:
    """Проверяет и нормализует введённую цену. None если некорректна."""
    text = (raw or "").strip().replace(",", ".")
    if not text:
        return None
    if text.startswith("."):
        text = "0" + text
    if not re.match(r"^\d+(?:\.\d{1,6})?$", text):
        return None
    try:
        val = Decimal(text)
    except InvalidOperation:
        return None
    if val < MIN_LOT_PRICE or val > MAX_LOT_PRICE:
        return None
    return format_starvell_price(val)


def format_starvell_price(price: str | float | Decimal) -> str:
    """Форматирует цену для Starvell API (без лишних нулей, до 6 знаков)."""
    try:
        val = Decimal(str(price).replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        val = Decimal("0.01")
    if val < MIN_LOT_PRICE:
        val = MIN_LOT_PRICE
    if val > MAX_LOT_PRICE:
        val = MAX_LOT_PRICE
    quantized = val.quantize(Decimal(f"0.{'0' * MAX_DECIMALS}")).normalize()
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0.001"


def format_price_display(price: str | float | Decimal) -> str:
    """Красивый вывод цены для Telegram."""
    return format_starvell_price(price)


def parse_smm_reply(text: str) -> tuple[int | None, str | None]:
    """
    Разбирает ответ вида «1634», «1634 0.001», «1634, 0.05 ₽».
    Возвращает (service_id, price_or_none).
    """
    raw = (text or "").strip()
    if not raw:
        return None, None
    numbers = _PRICE_NUMBER_RE.findall(raw.replace(",", "."))
    if not numbers:
        return None, None
    service_id: int | None = None
    price: str | None = None
    for num in numbers:
        normalized = normalize_price_input(num)
        if normalized is None:
            continue
        val = Decimal(normalized)
        if service_id is None and val == val.to_integral_value() and val >= 1:
            service_id = int(val)
            continue
        if price is None:
            price = normalized
    if service_id is None and numbers:
        first = normalize_price_input(numbers[0])
        if first and Decimal(first) == Decimal(first).to_integral_value() and Decimal(first) >= 1:
            service_id = int(Decimal(first))
    return service_id, price


def parse_parser_message(text: str) -> tuple[str, int | None, str | None]:
    """
    Разбирает сообщение парсера:
    - URL
    - URL + service_id + price (в одной или нескольких строках)
    """
    raw = (text or "").strip()
    if not raw:
        return "", None, None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    url = lines[0]
    rest = " ".join(lines[1:]) if len(lines) > 1 else ""
    if not rest and " " in url:
        parts = url.split()
        if parts[0].startswith("http"):
            url = parts[0]
            rest = " ".join(parts[1:])
    service_id, price = parse_smm_reply(rest) if rest else (None, None)
    return url, service_id, price
