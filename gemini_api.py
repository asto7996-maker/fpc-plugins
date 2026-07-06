"""
Общие вызовы Google Gemini API (поддержка ключей AIza… и AQ.…).
"""

from __future__ import annotations

from typing import Any

import httpx

GEMINI_MODELS = (
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def gemini_url(model: str) -> str:
    return f"{GEMINI_BASE}/{model}:generateContent"


def client_kwargs(proxy: str = "", timeout: float = 45.0) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"timeout": timeout}
    proxy = (proxy or "").strip()
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def request_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key.strip(),
    }


def is_auth_error(status_code: int, body: str) -> bool:
    if status_code in (401, 403):
        return True
    if status_code != 400:
        return False
    low = body.lower()
    markers = (
        "api key not valid",
        "invalid api key",
        "invalid authentication",
        "expected oauth",
        "permission_denied",
        "unregistered callers",
    )
    return any(m in low for m in markers)


def is_quota_error(status_code: int, body: str) -> bool:
    if status_code == 429:
        return True
    low = body.lower()
    return "quota exceeded" in low or "rate limit" in low


def extract_error_message(body: str, limit: int = 160) -> str:
    import json

    try:
        data = json.loads(body)
        err = data.get("error") or {}
        msg = str(err.get("message") or body)
    except Exception:
        msg = body
    msg = " ".join(msg.split())
    return msg[:limit]


async def post_generate(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    payload: dict[str, Any],
) -> httpx.Response:
    return await client.post(
        gemini_url(model),
        json=payload,
        headers=request_headers(api_key),
    )
