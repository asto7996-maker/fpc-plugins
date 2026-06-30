"""
Проверка ключевых данных (Starvell session, Gemini API) перед сохранением.
"""

from __future__ import annotations

import httpx

from config import DEFAULT_AI_SYSTEM_PROMPT
from starvell_api import StarvellAPI
from utils.starvell_format import format_rub_balance

GEMINI_MODELS = ("gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro")


async def test_starvell_session(session_cookie: str) -> tuple[bool, str, dict]:
    """
    Проверяет cookie session на Starvell.
    Возвращает (успех, сообщение, данные пользователя).
    """
    cookie = (session_cookie or "").strip()
    if len(cookie) < 10:
        return False, "Cookie слишком короткий", {}

    api = StarvellAPI(session_cookie=cookie, delay_seconds=0.5)
    try:
        info = await api.fetch_homepage()
    except Exception as exc:
        return False, f"Ошибка подключения: {exc}", {}

    if not info.get("authorized"):
        return False, "Сессия недействительна или истекла", {}

    user = info.get("user") or {}
    username = user.get("username") or user.get("id") or "?"
    balance = format_rub_balance(user.get("balance"))
    return True, f"Авторизован: {username} | Баланс: {balance}", info


async def test_gemini_key(api_key: str, system_prompt: str = "") -> tuple[bool, str]:
    """Проверяет Gemini API ключ тестовым запросом."""
    key = (api_key or "").strip()
    if not key:
        return False, "Ключ пустой"

    prompt = system_prompt or DEFAULT_AI_SYSTEM_PROMPT
    payload_base = {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: работает"}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 20},
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        for model in GEMINI_MODELS:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={key}"
            )
            try:
                resp = await client.post(url, json=payload_base)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates") or []
                    if candidates:
                        return True, f"Gemini OK (модель {model})"
                if resp.status_code in (400, 403):
                    return False, f"Неверный ключ: {resp.text[:120]}"
            except Exception as exc:
                return False, f"Ошибка: {exc}"

    return False, "Не удалось подключиться к Gemini"
