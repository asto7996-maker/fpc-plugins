"""
Проверка ключевых данных (Starvell session, Gemini API) перед сохранением.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from config import DEFAULT_AI_SYSTEM_PROMPT
from starvell_api import StarvellAPI
from utils.starvell_format import format_rub_balance

GEMINI_MODELS = ("gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro")
_GEMINI_STANDARD_KEY_RE = re.compile(r"AIza[A-Za-z0-9_-]{35}")
_GEMINI_AUTH_KEY_RE = re.compile(r"AQ\.[A-Za-z0-9_.-]{20,}")
_PROXY_SCHEME_RE = re.compile(r"^(https?|socks5h?|socks4)://", re.I)
_SOCKS_PORTS = {1080, 8000, 9050, 4145, 5678, 1081}
_HTTP_PORTS = {80, 443, 8080, 3128, 8888}


def _build_proxy_url(
    scheme: str,
    host: str,
    port: str | int,
    user: str = "",
    password: str = "",
) -> str:
    host = (host or "").strip()
    port = str(port or "").strip()
    if not host or not port:
        return ""
    scheme = scheme.lower().rstrip(":/")
    if user:
        user_q = quote(unquote(user), safe="")
        pass_q = quote(unquote(password or ""), safe="")
        return f"{scheme}://{user_q}:{pass_q}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def _guess_proxy_scheme(host_port: str) -> str:
    match = re.search(r":(\d+)\s*$", host_port.strip())
    if not match:
        return "socks5"
    port = int(match.group(1))
    if port in _HTTP_PORTS:
        return "http"
    if port in _SOCKS_PORTS:
        return "socks5"
    return "socks5"


def _parse_telegram_socks_link(text: str) -> str:
    lowered = text.lower()
    if "t.me/socks" not in lowered and "tg://socks" not in lowered:
        return ""

    parsed = urlparse(text.strip())
    qs = parse_qs(parsed.query)
    server = (qs.get("server") or qs.get("host") or [""])[0].strip()
    port = (qs.get("port") or ["1080"])[0].strip()
    user = unquote((qs.get("user") or qs.get("username") or [""])[0])
    password = unquote((qs.get("pass") or qs.get("password") or [""])[0])
    return _build_proxy_url("socks5", server, port, user, password)


def parse_proxy_url(raw: str) -> str:
    """Нормализует URL прокси (http/socks5), в т.ч. ссылки t.me/socks."""
    text = (raw or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"-", "нет", "no", "skip", "none", "без прокси"}:
        return ""

    text = text.strip("`\"'")

    tg_proxy = _parse_telegram_socks_link(text)
    if tg_proxy:
        return tg_proxy

    if _PROXY_SCHEME_RE.match(text):
        return text

    # host:port:user:pass
    if "@" not in text and "://" not in text and text.count(":") == 3:
        host, port, user, password = text.split(":", 3)
        return _build_proxy_url("socks5", host, port, user, password)

    if "@" in text or text.count(":") >= 2:
        scheme = _guess_proxy_scheme(text.split("@")[-1])
        return f"{scheme}://{text}"

    return ""


def parse_gemini_api_key(raw: str) -> str:
    """Извлекает ключ Gemini (AIza… или AQ.… Auth key из AI Studio)."""
    text = (raw or "").strip()
    if not text:
        return ""

    text = text.strip("`\"' \t\n\r")
    for chunk in re.split(r"[\s,;]+", text):
        chunk = chunk.strip("`\"'")
        auth_match = _GEMINI_AUTH_KEY_RE.search(chunk)
        if auth_match:
            return auth_match.group(0)
        std_match = _GEMINI_STANDARD_KEY_RE.search(chunk)
        if std_match:
            return std_match.group(0)
        if "key=" in chunk:
            for part in chunk.split("key="):
                auth_match = _GEMINI_AUTH_KEY_RE.search(part)
                if auth_match:
                    return auth_match.group(0)
                std_match = _GEMINI_STANDARD_KEY_RE.search(part)
                if std_match:
                    return std_match.group(0)

    auth_match = _GEMINI_AUTH_KEY_RE.search(text)
    if auth_match:
        return auth_match.group(0)

    std_match = _GEMINI_STANDARD_KEY_RE.search(text)
    if std_match:
        return std_match.group(0)

    first = text.split()[0].strip("`\"'.,;")
    if first.startswith("AQ."):
        return first
    if first.startswith("AIza") and len(first) >= 39:
        return first[:39]
    return first


def is_valid_gemini_key_format(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    if _GEMINI_STANDARD_KEY_RE.fullmatch(key):
        return True
    if key.startswith("AQ.") and len(key) >= 25 and _GEMINI_AUTH_KEY_RE.fullmatch(key):
        return True
    return False


def gemini_generate_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def gemini_auth_headers(api_key: str) -> dict[str, str]:
    return {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }


def _mask_proxy_for_log(proxy_url: str) -> str:
    return re.sub(r":([^:@/]+)@", ":***@", proxy_url)


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
        return False, "Прокси не указан или неверный формат.\nПоддерживаются: t.me/socks ссылки, socks5://…, user:pass@host:port"

    try:
        async with httpx.AsyncClient(proxy=url, timeout=25.0) as client:
            resp = await client.get("https://generativelanguage.googleapis.com/")
        return True, f"Прокси OK ({_mask_proxy_for_log(url)}, HTTP {resp.status_code})"
    except ImportError:
        return False, "На сервере нет поддержки SOCKS. Переустановите бота (httpx[socks])."
    except Exception as exc:
        err = str(exc)
        if "socks" in err.lower() or "proxy" in err.lower():
            return False, f"Прокси недоступен: {err[:180]}"
        return False, f"Прокси недоступен: {err[:180]}"


async def test_gemini_key(
    api_key: str,
    system_prompt: str = "",
    *,
    proxy: str | None = None,
) -> tuple[bool, str]:
    """Проверяет Gemini API ключ тестовым запросом."""
    key = parse_gemini_api_key(api_key)
    if not key:
        return False, "Ключ пустой или не распознан (ожидается AIza… или AQ.…)"
    if not is_valid_gemini_key_format(key):
        return False, "Неверный формат ключа Gemini (AIza… или AQ.… из AI Studio)"

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

    headers = gemini_auth_headers(key)

    async with httpx.AsyncClient(**client_kwargs) as client:
        for model in GEMINI_MODELS:
            url = gemini_generate_url(model)
            try:
                resp = await client.post(url, json=payload_base, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates") or []
                    if candidates:
                        key_type = "Auth AQ" if key.startswith("AQ.") else "Standard"
                        return True, f"Gemini OK ({key_type}, модель {model})"
                if resp.status_code in (400, 401, 403):
                    detail = resp.text[:200]
                    return False, f"Ключ отклонён API: {detail}"
            except Exception as exc:
                return False, f"Ошибка: {exc}"

    return False, "Не удалось подключиться к Gemini (проверьте прокси и ключ)"
