"""
receipt_amount.py — сумма на чеке должна совпасть с тарифом копейка в копейку.

Подпись к сообщению не считается: сумму читаем с PDF или с картинки (OCR).
Допуска 5% / 1 ₽ нет.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AmountCheck:
    ok: bool
    reason: str = ""
    flags: list[str] = field(default_factory=list)
    source: str = ""
    text_sample: str = ""


def prices_equal(left: float, right: float) -> bool:
    """Равенство до копейки. Без процентов и без «плюс-минус рубль»."""
    try:
        a = round(float(left), 2)
        b = round(float(right), 2)
    except (TypeError, ValueError):
        return False
    return abs(a - b) < 0.005


def format_price_exact(price: float) -> str:
    value = round(float(price) + 1e-9, 2)
    if abs(value - round(value)) < 0.005:
        return str(int(round(value)))
    return f"{value:.2f}".replace(".", ",")


def _int_and_cents(price: float) -> tuple[int, int]:
    value = round(float(price) + 1e-9, 2)
    int_part = int(value)
    cents = int(round((value - int_part) * 100))
    if cents == 100:
        int_part += 1
        cents = 0
    return int_part, cents


def price_text_variants(price: float) -> list[str]:
    int_part, cents = _int_and_cents(price)
    grouped = f"{int_part:,}".replace(",", " ")
    variants = set()
    if cents == 0:
        variants.update(
            {
                str(int_part),
                grouped,
                f"{int_part}.00",
                f"{int_part},00",
                f"{grouped}.00",
                f"{grouped},00",
            }
        )
    else:
        cc = f"{cents:02d}"
        variants.update(
            {
                f"{int_part}.{cc}",
                f"{int_part},{cc}",
                f"{grouped}.{cc}",
                f"{grouped},{cc}",
            }
        )
    return [v for v in variants if v]


def _normalize_text(text: str) -> str:
    raw = (text or "").replace("\u00a0", " ").replace("\u202f", " ")
    raw = raw.replace("₽", " руб ")
    raw = raw.replace("RUB", " руб ").replace("RUR", " руб ")
    return raw


def price_in_text(text: str, price: float) -> bool:
    if price <= 0 or not (text or "").strip():
        return False
    raw = _normalize_text(text)
    for form in price_text_variants(price):
        escaped = re.escape(form)
        escaped = escaped.replace(r"\ ", r"[\s\u00a0]*")
        pattern = r"(?<![\d.,])" + escaped + r"(?![\d])"
        if re.search(pattern, raw):
            return True
    return False


def extract_pdf_text(data: bytes) -> str:
    if not data or data[:5] != b"%PDF-":
        return ""
    chunks: list[str] = [data[:400_000].decode("latin-1", errors="ignore")]
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:8]:
            got = page.extract_text() or ""
            if got.strip():
                chunks.append(got)
    except Exception:
        logger.debug("pypdf extract failed", exc_info=True)
    return "\n".join(chunks)


def _prepare_ocr_image(data: bytes):
    from PIL import Image, ImageFilter, ImageOps

    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    max_side = max(w, h)
    if max_side < 1400:
        scale = 1400 / max(max_side, 1)
        img = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray


def extract_ocr_text(data: bytes) -> str:
    if shutil.which("tesseract") is None:
        logger.warning("tesseract binary not found")
        return ""
    try:
        import pytesseract
    except ImportError:
        logger.warning("pytesseract not installed")
        return ""
    try:
        gray = _prepare_ocr_image(data)
    except Exception:
        logger.debug("ocr image prepare failed", exc_info=True)
        return ""
    langs = "rus+eng"
    texts: list[str] = []
    for psm in (6, 4, 11):
        try:
            texts.append(
                pytesseract.image_to_string(
                    gray, lang=langs, config=f"--oem 3 --psm {psm}"
                )
            )
        except Exception:
            try:
                texts.append(
                    pytesseract.image_to_string(gray, config=f"--oem 3 --psm {psm}")
                )
            except Exception:
                logger.debug("tesseract psm %s failed", psm, exc_info=True)
    try:
        texts.append(
            pytesseract.image_to_string(
                gray,
                config="--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789., ",
            )
        )
    except Exception:
        pass
    return "\n".join(t for t in texts if t and t.strip())


def extract_receipt_text(
    data: bytes, *, mime: str = "", filename: str = ""
) -> tuple[str, str]:
    name = (filename or "").lower()
    kind = (mime or "").lower()
    is_pdf = data[:5] == b"%PDF-" or kind == "application/pdf" or name.endswith(".pdf")
    if is_pdf:
        text = extract_pdf_text(data)
        return text, "pdf"
    text = extract_ocr_text(data)
    return text, "ocr"


def mismatch_reason(price: float) -> str:
    shown = format_price_exact(price)
    return (
        f"на чеке должна быть сумма ровно {shown} ₽ — как цена тарифа в магазине"
    )


def check_receipt_amount(
    data: bytes,
    *,
    mime: str = "",
    filename: str = "",
    declared_price: float = 0.0,
) -> AmountCheck:
    price = float(declared_price or 0)
    if price <= 0:
        return AmountCheck(ok=False, reason="не задана цена тарифа")
    text, source = extract_receipt_text(data, mime=mime, filename=filename)
    flags = [f"amount-source:{source}"]
    sample = re.sub(r"\s+", " ", (text or ""))[:180]
    if price_in_text(text, price):
        flags.append("amount-exact")
        return AmountCheck(
            ok=True,
            flags=flags,
            source=source,
            text_sample=sample,
        )
    if not (text or "").strip():
        flags.append("amount-unreadable")
        return AmountCheck(
            ok=False,
            reason=mismatch_reason(price),
            flags=flags,
            source=source,
        )
    flags.append("amount-mismatch")
    return AmountCheck(
        ok=False,
        reason=mismatch_reason(price),
        flags=flags,
        source=source,
        text_sample=sample,
    )


def vision_amount_exact(amount: Optional[float], declared_price: float) -> bool:
    if amount is None or declared_price <= 0:
        return False
    return prices_equal(amount, declared_price)


def amount_is_confirmed(
    hit: AmountCheck,
    *,
    vision_amount: Optional[float] = None,
    declared_price: float = 0.0,
) -> bool:
    """Сумма подтверждена, только если она прочитана с чека или vision вернул ровно её."""
    if hit.ok:
        return True
    return vision_amount_exact(vision_amount, declared_price)
