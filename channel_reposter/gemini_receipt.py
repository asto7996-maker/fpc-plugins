"""
gemini_receipt.py — проверка чека через Gemini vision.

Локальный EXIF/ELA не отличает квитанцию от скрина чата.
Сюда уходит картинка: это чек? сумма сходится? есть монтаж/Photoshop?

Ключи AQ. (новый формат AI Studio) и старые AIza идут одним путём:
заголовок x-goog-api-key, сначала Interactions API, потом generateContent.
В ответах пользователю не пишем, чем именно проверяли.
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

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GENERATE_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
API_REVISION = "2026-05-20"
DEFAULT_MODELS = (
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
)
USER_CHECK_FAIL = "не удалось проверить чек"
USER_BAD_FILE = "не удалось подготовить файл чека"
USER_BAD_ANSWER = "не удалось разобрать ответ проверки"

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
В поле reason не упоминай нейросеть, Gemini, Google и AI Studio.
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


def normalize_api_key(raw: str) -> str:
    """AQ. и AIza ключи — обычные строки. Срезаем кавычки, BOM и пробелы."""
    key = (raw or "").replace("\ufeff", "").strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {'"', "'"}:
        key = key[1:-1].strip()
    return key.replace("\r", "").replace("\n", "").replace(" ", "")


def key_meta(raw: str) -> str:
    key = normalize_api_key(raw)
    if not key:
        return "empty"
    prefix = key[:4] if key.startswith("AQ.") else key[:4]
    return f"prefix={prefix} len={len(key)}"


def _headers(api_key: str, *, interactions: bool = False) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": normalize_api_key(api_key),
    }
    if interactions:
        headers["Api-Revision"] = API_REVISION
    return headers


def extract_model_text(parsed: Any) -> str:
    """Достать текст и из Interactions, и из generateContent."""
    if isinstance(parsed, str):
        return parsed.strip()
    if not isinstance(parsed, dict):
        return ""
    direct = parsed.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in parsed.get("outputs") or parsed.get("output") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text)
        for part in item.get("content") or []:
            if isinstance(part, dict):
                ptext = part.get("text")
                if isinstance(ptext, str) and ptext.strip():
                    chunks.append(ptext)
    if chunks:
        return "\n".join(chunks).strip()
    parts = (
        ((parsed.get("candidates") or [{}])[0].get("content") or {}).get("parts")
        or []
    )
    return "\n".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")
    ).strip()


def parse_verdict_json(text: str, declared_price: float) -> GeminiVerdict:
    """Разобрать ответ модели. Без сети — удобно тестировать."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I).strip()
        raw = raw.rstrip("`").strip()
    match = _JSON_RE.search(raw)
    if not match:
        return GeminiVerdict(ok=False, reason=USER_BAD_ANSWER, raw=text)
    try:
        data: dict[str, Any] = json.loads(match.group(0))
    except json.JSONDecodeError:
        return GeminiVerdict(ok=False, reason=USER_BAD_ANSWER, raw=text)

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


def _interactions_payload(model: str, prompt: str, mime_out: str, b64: str) -> dict[str, Any]:
    media_type = "document" if mime_out == "application/pdf" else "image"
    return {
        "model": model,
        "input": [
            {"type": "text", "text": prompt},
            {"type": media_type, "data": b64, "mime_type": mime_out},
        ],
    }


def _generate_payload(prompt: str, mime_out: str, b64: str) -> dict[str, Any]:
    return {
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


def _request_attempts(
    api_key: str, model: str, prompt: str, mime_out: str, b64: str
) -> list[tuple[str, str, dict[str, str], dict[str, Any]]]:
    """Сначала Interactions (актуальный путь для AQ.), потом generateContent."""
    key = normalize_api_key(api_key)
    ix_headers = _headers(key, interactions=True)
    gc_headers = _headers(key, interactions=False)
    gc_url = f"{GENERATE_BASE}/{model}:generateContent"
    ix_body = _interactions_payload(model, prompt, mime_out, b64)
    gc_body = _generate_payload(prompt, mime_out, b64)
    return [
        ("interactions", INTERACTIONS_URL, ix_headers, ix_body),
        ("generate", gc_url, gc_headers, gc_body),
        (
            "generate-query",
            f"{gc_url}?key={urllib.parse.quote(key)}",
            {"Content-Type": "application/json"},
            gc_body,
        ),
    ]


def _verdict_from_body(
    body: str, declared_price: float, model: str, surface: str
) -> Optional[GeminiVerdict]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed and isinstance(parsed[0], dict) else {}
    if isinstance(parsed, dict) and parsed.get("error"):
        return None
    text = extract_model_text(parsed)
    if not text:
        return None
    verdict = parse_verdict_json(text, declared_price)
    verdict.model = model
    verdict.flags.append(f"check:{surface}:{model}")
    if verdict.is_receipt:
        verdict.flags.append("receipt")
    if verdict.edited:
        verdict.flags.append("edited")
    return verdict


def inspect_receipt_gemini_sync(
    data: bytes,
    *,
    mime: str = "",
    filename: str = "",
    declared_price: float = 0.0,
    api_key: str = "",
    model: str = "",
) -> GeminiVerdict:
    key = normalize_api_key(api_key or getattr(config, "GEMINI_API_KEY", "") or "")
    if not key:
        return GeminiVerdict(ok=False, reason=USER_CHECK_FAIL, flags=["no-key"])
    try:
        mime_out, b64 = _prepare_inline(data, mime, filename)
    except ValueError as e:
        return GeminiVerdict(ok=False, reason=str(e))
    except Exception:
        logger.exception("prepare receipt for check")
        return GeminiVerdict(ok=False, reason=USER_BAD_FILE)

    prompt = PROMPT.format(price=float(declared_price or 0))
    preferred = (model or getattr(config, "GEMINI_MODEL", "") or DEFAULT_MODELS[0]).strip()
    models = [preferred] + [m for m in DEFAULT_MODELS if m != preferred]
    last_err = ""
    logger.info("receipt check key %s models=%s", key_meta(key), ",".join(models[:3]))
    for try_model in models:
        auth_fail = 0
        for surface, url, headers, payload in _request_attempts(
            key, try_model, prompt, mime_out, b64
        ):
            status, body = _post(url, headers, payload, timeout=55)
            if status in (401, 403):
                last_err = f"{surface} {try_model} HTTP {status}: {body[:160]}"
                logger.warning("receipt check auth %s %s", key_meta(key), last_err)
                auth_fail += 1
                continue
            if status in (404, 429):
                last_err = f"{surface} {try_model} HTTP {status}"
                logger.warning("receipt check %s", last_err)
                break
            if status != 200:
                last_err = f"{surface} {try_model} HTTP {status}: {body[:160]}"
                logger.warning("receipt check %s", last_err)
                continue
            verdict = _verdict_from_body(body, declared_price, try_model, surface)
            if verdict is None:
                last_err = f"{surface} {try_model} empty/bad body"
                continue
            return verdict
        if auth_fail >= 3:
            # Этот ключ Google не принимает ни на одном транспорте — дальше те же 401.
            break
    logger.error("receipt check failed %s last=%s", key_meta(key), last_err[:180])
    return GeminiVerdict(ok=False, reason=USER_CHECK_FAIL, flags=["check-error"])


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
