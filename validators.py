"""
Проверка ключевых данных (Starvell session, Gemini API) перед сохранением.
"""

from __future__ import annotations

import httpx

from gemini_api import (
    GEMINI_MODELS,
    client_kwargs,
    extract_error_message,
    is_auth_error,
    is_quota_error,
    post_generate,
)
from starvell_api import StarvellAPI
from utils.starvell_format import format_rub_balance

DEFAULT_TEST_PROMPT = "Ты помощник. Отвечай кратко."


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


async def test_gemini_key(
    api_key: str,
    system_prompt: str = "",
    proxy: str = "",
) -> tuple[bool, str]:
    """Проверяет Gemini API ключ тестовым запросом (AIza… и AQ.…)."""
    key = (api_key or "").strip()
    if not key:
        return False, "Ключ пустой"

    prompt = system_prompt or DEFAULT_TEST_PROMPT
    payload_base = {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: работает"}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 20},
    }

    quota_model = ""
    last_error = ""

    async with httpx.AsyncClient(**client_kwargs(proxy, timeout=30.0)) as client:
        for model in GEMINI_MODELS:
            try:
                resp = await post_generate(client, key, model, payload_base)
            except Exception as exc:
                return False, f"Ошибка сети: {exc}"

            body = resp.text
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates") or []
                if candidates:
                    return True, f"Gemini OK (модель {model})"
                last_error = extract_error_message(body)
                continue

            if is_auth_error(resp.status_code, body):
                return False, f"Неверный ключ: {extract_error_message(body)}"

            if is_quota_error(resp.status_code, body):
                quota_model = model
                continue

            if resp.status_code == 404:
                continue

            last_error = extract_error_message(body)

    if quota_model:
        return True, (
            f"Ключ принят (модель {quota_model}: исчерпан бесплатный лимит, "
            "но ключ действителен — используйте gemini-2.5-flash-lite)"
        )

    if last_error:
        return False, f"Не удалось проверить ключ: {last_error}"

    return False, "Не удалось подключиться к Gemini (проверьте интернет или proxy)"
