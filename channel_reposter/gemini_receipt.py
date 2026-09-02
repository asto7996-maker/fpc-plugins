"""
gemini_receipt.py — проверка чека нейросетью Gemini (vision).

Локальный EXIF/ELA не отличает квитанцию от скрина чата.
Сюда уходит картинка: это чек? сумма сходится? есть монтаж/Photoshop?
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
)

_JSON_RE = re.compile(r"\{.*\}", re.S)

PROMPT = """Ты проверяешь файл на компенсацию покупки. Это ДОЛЖЕН быть чек/квитанция оплаты.

Прими ТОЛЬКО если на изображении видно платёжный документ:
- квитанция банка / СБП / карты;
- чек операции с суммой, датой и получателем или номером операции.

Отклони, если это:
- скриншот чата Telegram / переписки / группы;
- мем, фото комнаты, случайная картинка;
- обрезок без суммы;
- отредактированное изображение (Photoshop, Photopea, нарисованный текст, слои, штамп).

Ожидаемая сумма оплаты: {price:.2f} RUB (допуск 5% или 1 рубль).

Ответь ТОЛЬКО JSON без markdown:
{{
  "is_receipt": true/false,
  "edited": true/false,
  "amount": число или null,
  "amount_matches": true/false,
  "verdict": "ok" или "reject",
  "reason": "кратко по-русски, одна фраза"
}}
verdict=ok только если is_receipt=true, edited=false и (amount_matches=true ИЛИ сумма на чеке совпадает с ожидаемой).
"""


@dataclass
class GeminiVerdict:
    ok: bool
    reason: str
    is_receipt: bool = False
    edited: bool = False
    amount: Optional[float] = None
    amount_matches: bool = False
    raw: str = ""
    model: str = ""
    flags: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> "GeminiVerdict":
        self.ok = False
        self.reason = reason
        return self


def _headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "x-goog-api-key": api_key.strip()}


def _gemini_url(model: str) -> str:
    return f"{GEMINI_BASE}/{model}:generateContent"


def parse_verdict_json(text: str, declared_price: float) -> GeminiVerdict:
    """Разобрать ответ модели. Без сети — удобно тестировать."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I).strip()
        raw = raw.rstrip("`").strip()
    match = _JSON_RE.search(raw)
    if not match:
        return GeminiVerdict(ok=False, reason="нейросеть вернула непонятный ответ", raw=text)
    try:
        data: dict[str, Any] = json.loads(match.group(0))
    except json.JSONDecodeError:
        return GeminiVerdict(ok=False, reason="нейросеть вернула битый JSON", raw=text)

    is_receipt = bool(data.get("is_receipt"))
    edited = bool(data.get("edited"))
    amount = data.get("amount")
    try:
        amount_f = float(amount) if amount is not None and amount != "" else None
    except (TypeError, ValueError):
        amount_f = None
    amount_matches = bool(data.get("amount_matches"))
    if amount_f is not None and declared_price > 0:
        if abs(amount_f - declared_price) <= max(1.0, declared_price * 0.05):
            amount_matches = True
        else:
            amount_matches = False
    verdict = str(data.get("verdict") or "").strip().lower()
    reason = str(data.get("reason") or "").strip() or "отклонено"

    ok = (
        verdict == "ok"
        and is_receipt
        and not edited
        and amount_matches
    )
    if not is_receipt:
        reason = reason or "это не чек оплаты"
        ok = False
    elif edited:
        reason = reason or "есть следы Photoshop / монтажа"
        ok = False
    elif not amount_matches:
        reason = reason or "сумма на чеке не совпадает с тарифом"
        ok = False
    return GeminiVerdict(
        ok=ok,
        reason=reason,
        is_receipt=is_receipt,
        edited=edited,
        amount=amount_f,
        amount_matches=amount_matches,
        raw=text,
    )


def _prepare_inline(data: bytes, mime: str, filename: str) -> tuple[str, str]:
    """Сжать картинку при необходимости. PDF оставляем как есть, если влезает."""
    name = (filename or "").lower()
    kind = (mime or "").lower()
    is_pdf = kind == "application/pdf" or name.endswith(".pdf") or data[:5] == b"%PDF-"
    if is_pdf:
        if len(data) > 4_000_000:
            raise ValueError("PDF слишком большой — пришлите скриншот чека")
        return "application/pdf", base64.b64encode(data).decode("ascii")

    try:
        from PIL import Image
    except ImportError:
        if len(data) > 3_500_000:
            raise ValueError("файл слишком большой")
        return (kind or "image/jpeg"), base64.b64encode(data).decode("ascii")

    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    w, h = img.size
    max_side = 1600
    if max(w, h) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True)
    out = buf.getvalue()
    if len(out) > 3_800_000:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=65, optimize=True)
        out = buf.getvalue()
    return "image/jpeg", base64.b64encode(out).decode("ascii")


def _post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace") if e.fp else str(e)
        return int(e.code), err


def _auth_attempts(api_key: str, model: str) -> list[tuple[str, dict[str, str]]]:
    """AQ-ключи часто принимают и заголовок, и ?key= — пробуем оба."""
    url = _gemini_url(model)
    key = api_key.strip()
    header = _headers(key)
    return [
        (url, header),
        (f"{url}?key={urllib.parse.quote(key)}", {"Content-Type": "application/json"}),
        (f"{url}?key={urllib.parse.quote(key)}", header),
    ]


def inspect_receipt_gemini_sync(
    data: bytes,
    *,
    mime: str = "",
    filename: str = "",
    declared_price: float = 0.0,
    api_key: str = "",
    model: str = "",
) -> GeminiVerdict:
    key = (api_key or getattr(config, "GEMINI_API_KEY", "") or "").strip()
    if not key:
        return GeminiVerdict(ok=False, reason="не настроен ключ Gemini — чек не проверен")
    try:
        mime_out, b64 = _prepare_inline(data, mime, filename)
    except ValueError as e:
        return GeminiVerdict(ok=False, reason=str(e))
    except Exception:
        logger.exception("prepare receipt for gemini")
        return GeminiVerdict(ok=False, reason="не удалось подготовить файл чека")

    prompt = PROMPT.format(price=float(declared_price or 0))
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_out, "data": b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 400,
            "responseMimeType": "application/json",
        },
    }
    preferred = (model or getattr(config, "GEMINI_MODEL", "") or DEFAULT_MODELS[0]).strip()
    models = [preferred] + [m for m in DEFAULT_MODELS if m != preferred]
    last_err = ""
    for try_model in models:
        auth_fail = 0
        for url, headers in _auth_attempts(key, try_model):
            status, body = _post(url, headers, payload, timeout=55)
            if status in (401, 403):
                last_err = f"{try_model} HTTP {status}: {body[:180]}"
                logger.warning("Gemini %s", last_err)
                auth_fail += 1
                continue
            if status in (404, 429):
                last_err = f"{try_model} HTTP {status}"
                break
            if status != 200:
                last_err = f"{try_model} HTTP {status}: {body[:180]}"
                logger.warning("Gemini %s", last_err)
                continue
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                last_err = "не JSON от API"
                continue
            parts = (
                ((parsed.get("candidates") or [{}])[0].get("content") or {}).get("parts")
                or []
            )
            text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
            if not text:
                last_err = "пустой ответ Gemini"
                continue
            verdict = parse_verdict_json(text, declared_price)
            verdict.model = try_model
            verdict.flags.append(f"gemini:{try_model}")
            if verdict.is_receipt:
                verdict.flags.append("receipt")
            if verdict.edited:
                verdict.flags.append("edited")
            return verdict
        if auth_fail >= 3:
            break
    return GeminiVerdict(
        ok=False,
        reason=f"нейросеть не ответила ({last_err or 'нет модели'})",
        flags=["gemini-error"],
    )


async def inspect_receipt_gemini(
    data: bytes,
    *,
    mime: str = "",
    filename: str = "",
    declared_price: float = 0.0,
) -> GeminiVerdict:
    return await asyncio.to_thread(
        inspect_receipt_gemini_sync,
        data,
        mime=mime,
        filename=filename,
        declared_price=declared_price,
    )
