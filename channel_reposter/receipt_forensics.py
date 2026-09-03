"""
receipt_forensics.py — проверка чека юзерботом.

Ищем следы Photoshop / GIMP / Photopea и грубый монтаж (ELA).
Скриншот из банка без EXIF — норма; явный редактор в метаданных — отказ.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

EDITOR_MARKERS = (
    b"adobe photoshop",
    b"adobe photoshop",
    b"photoshop",
    b"image ready",
    b"gimp",
    b"photopea",
    b"picsart",
    b"snapseed",
    b"lightroom",
    b"pixelmator",
    b"affinity photo",
    b"corel",
    b"paint.net",
    b"pixlr",
    b"fotomashup",
)

# EXIF / XMP текстовые маркеры
EDITOR_WORDS = (
    "photoshop",
    "adobe",
    "gimp",
    "photopea",
    "picsart",
    "snapseed",
    "lightroom",
    "pixelmator",
    "affinity",
    "corel",
    "paint.net",
    "pixlr",
)


@dataclass
class ForensicResult:
    ok: bool
    reason: str = ""
    flags: list[str] = field(default_factory=list)
    score: int = 0

    def reject(self, reason: str, score: int = 50) -> "ForensicResult":
        self.ok = False
        self.reason = reason
        self.score = max(self.score, score)
        return self


def _lower_bytes(data: bytes) -> bytes:
    return data[: 2_000_000].lower()


def scan_magic(data: bytes) -> list[str]:
    blob = _lower_bytes(data)
    hits = []
    for marker in EDITOR_MARKERS:
        if marker in blob:
            hits.append(marker.decode("ascii", errors="ignore"))
    return hits


def _exif_software(img) -> str:
    try:
        exif = img.getexif()
    except Exception:
        return ""
    if not exif:
        return ""
    # 305 Software, 11 ProcessingSoftware
    parts = []
    for tag in (305, 11, 315, 270):
        val = exif.get(tag)
        if val:
            parts.append(str(val))
    # sometimes in IFD
    try:
        extra = getattr(img, "info", {}) or {}
        for key in ("software", "Software", "xml", "xmp"):
            if extra.get(key):
                parts.append(str(extra.get(key)))
    except Exception:
        pass
    return " ".join(parts).lower()


def ela_score(img) -> float:
    """Грубая Error Level Analysis: 0 чисто, выше — возможный монтаж."""
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        return 0.0
    try:
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=90)
        buf.seek(0)
        recompressed = Image.open(buf).convert("RGB")
        diff = ImageChops.difference(rgb, recompressed)
        stat = ImageStat.Stat(diff)
        # среднее по каналам
        mean = sum(stat.mean) / max(len(stat.mean), 1)
        extrema = diff.getextrema()
        peak = max(ch[1] for ch in extrema) if extrema else 0
        return float(mean) + float(peak) / 25.0
    except Exception:
        logger.debug("ELA failed", exc_info=True)
        return 0.0


def inspect_receipt_bytes(
    data: bytes,
    *,
    filename: str = "",
    mime: str = "",
    declared_price: float = 0.0,
    caption: str = "",
) -> ForensicResult:
    result = ForensicResult(ok=True)
    if not data or len(data) < 8_000:
        return result.reject("файл слишком маленький — нужен полный чек, не обрезок")

    magic_hits = scan_magic(data)
    if magic_hits:
        result.flags.extend(magic_hits)
        return result.reject(
            "в файле есть следы графического редактора (%s)" % ", ".join(magic_hits[:3]),
            score=80,
        )

    name = (filename or "").lower()
    if any(w in name for w in ("photoshop", "edited", "ps_", "gimp")):
        return result.reject("имя файла похоже на экспорт из редактора", score=60)

    is_pdf = data[:5] == b"%PDF-" or "pdf" in (mime or "").lower() or name.endswith(".pdf")
    if is_pdf:
        head = data[:80_000].lower()
        if b"photoshop" in head or b"photopea" in head or b"gimp" in head:
            return result.reject("PDF сохранён из графического редактора", score=80)
        return result

    try:
        from PIL import Image
    except ImportError:
        result.flags.append("no-pillow")
        return result

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return result.reject("не удалось открыть изображение чека")

    software = _exif_software(img)
    if software:
        result.flags.append("exif:" + software[:80])
        if any(w in software for w in EDITOR_WORDS if w != "adobe"):
            return result.reject(
                "в метаданных чека указан графический редактор",
                score=80,
            )
        if "photoshop" in software:
            return result.reject("EXIF: Adobe Photoshop — чек отредактирован", score=90)

    # PNG текстовые чанки
    try:
        png_txt = " ".join(
            str(v).lower() for v in (getattr(img, "text", None) or {}).values()
        )
        if png_txt and any(w in png_txt for w in EDITOR_WORDS):
            return result.reject("PNG содержит подпись графического редактора", score=80)
    except Exception:
        pass

    score_ela = ela_score(img)
    if score_ela:
        result.flags.append(f"ela:{score_ela:.1f}")
    # Скриншоты UI дают средний ELA; пик монтажа обычно заметно выше
    if score_ela >= 28.0:
        result.score = max(result.score, 45)
        result.flags.append("ela-high")
        return result.reject(
            "по сжатию картинка похожа на монтаж (Photoshop и подобное). "
            "Пришлите исходный файл чека без обработки, как документ",
            score=55,
        )

    w, h = img.size
    if w < 240 or h < 240:
        return result.reject("картинка слишком маленькая — нужен полный скриншот чека")

    return result
