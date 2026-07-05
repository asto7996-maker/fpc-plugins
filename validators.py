"""
Проверка ключевых данных (Starvell session, Gemini API) перед сохранением.
"""

from __future__ import annotations

import re

import httpx

from config import DEFAULT_AI_SYSTEM_PROMPT
from starvell_api import StarvellAPI
from utils.starvell_format import format_rub_balance

GEMINI_MODELS = ("gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro")
_GEMINI_KEY_RE = re.compile(r"AIza[A-Za-z0-9_-]{35}")
_PROXY_SCHEME_RE = re.compile(r"^(https?|socks5h?|socks4)://", re.I)


def parse_gemini_api_key(raw: str) -> str:
    """Извлекает ключ Gemini из текста (ссылка, markdown, лишние символы)."""
    text = (raw or "").strip()
    if not text:
        return ""

    text = text.strip("`\"' \t\n\r")
    for chunk in re.split(r"[\s,;]+", text):
        chunk = chunk.strip("`\"'")
        match = _GEMINI_KEY_RE.search(chunk)
        if match:
            return match.group(0)
        if "key=" in chunk:
            for part in chunk.split("key="):
                match = _GEMINI_KEY_RE.search(part)
                if match:
                    return match.group(0)

    match = _GEMINI_KEY_RE.search(text)
    if match:
        return match.group(0)

    first = text.split()[0].strip("`\"'.,;")
    if first.startswith("AIza") and len(first) >= 39:
        return first[:39]
    return first


def parse_proxy_url(raw: str) -> str:
    """Нормализует URL прокси (http/socks5)."""
    text = (raw or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"-", "нет", "no", "skip", "none", "без прокси"}:
        return ""

    text = text.strip("`\"'")
    if _PROXY_SCHEME_RE.match(text):
        return text

    if "@" in text or text.count(":") >= 2:
        return f"http://{text}"
    return ""


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


async def test_proxy_url(proxy: str) -> tuple[bool, str]:
    """Проверяет доступность прокси."""
    url = parse_proxy_url(proxy)
    if not url:
        return False, "Прокси не указан или неверный формат"

    try:
        async with httpx.AsyncClient(proxy=url, timeout=20.0) as client:
            resp = await client.get("https://generativelanguage.googleapis.com/")
        return True, f"Прокси OK (HTTP {resp.status_code})"
    except Exception as exc:
        return False, f"Прокси недоступен: {exc}"


async def test_gemini_key(
    api_key: str,
    system_prompt: str = "",
    *,
    proxy: str | None = None,
) -> tuple[bool, str]:
    """Проверяет Gemini API ключ тестовым запросом."""
    key = parse_gemini_api_key(api_key)
    if not key:
        return False, "Ключ пустой или не распознан (ожидается AIza…)"
    if not _GEMINI_KEY_RE.fullmatch(key):
        return False, "Неверный формат ключа Gemini"

    prompt = system_prompt or DEFAULT_AI_SYSTEM_PROMPT
    payload_base = {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: работает"}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 20},
    }

    client_kwargs: dict = {"timeout": 30.0}
    proxy_url = parse_proxy_url(proxy or "")
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_kwargs) as client:
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
                    detail = resp.text[:160]
                    return False, f"Неверный ключ: {detail}"
            except Exception as exc:
                return False, f"Ошибка: {exc}"

    return False, "Не удалось подключиться к Gemini (проверьте прокси и ключ)"
