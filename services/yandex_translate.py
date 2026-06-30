"""Перевод RU→EN через неофициальный API Яндекс.Переводчика (iOS endpoint)."""

from __future__ import annotations

import logging
import uuid

import httpx

from services.price_utils import parse_price_hint  # re-export

__all__ = ["translate_ru_to_en", "parse_price_hint"]

logger = logging.getLogger("starvell.yandex_translate")

_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
_TRANSLATE_URL = "https://translate.yandex.net/api/v1/tr.json/translate"
_CHUNK_SIZE = 4500


def _split_text(text: str, limit: int = _CHUNK_SIZE) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 3:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 3:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return parts


async def translate_ru_to_en(text: str) -> str:
    """Переводит текст с русского на английский через Яндекс.Переводчик."""
    chunks = _split_text(text)
    if not chunks:
        return ""

    sid = uuid.uuid4().hex.upper()
    params = {
        "lang": "ru-en",
        "srv": "ios",
        "ucid": str(uuid.uuid4()).upper(),
        "sid": sid,
        "id": f"{sid}-0-0",
    }
    translated: list[str] = []

    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": _USER_AGENT}) as client:
        for chunk in chunks:
            resp = await client.post(
                _TRANSLATE_URL,
                params=params,
                data={"text": chunk},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 200:
                raise RuntimeError(data.get("message") or f"Yandex translate code {data.get('code')}")
            block = data.get("text")
            if isinstance(block, list) and block:
                translated.append(str(block[0]))
            elif isinstance(block, str):
                translated.append(block)
            else:
                translated.append(chunk)

    return "\n\n".join(translated).strip()