"""
Gemini API client, proxy validation, parsing, and rotation engine.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx

from database import Database, ProxyRecord

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_TEST_PROMPT = "ответь одним словом: ок"
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.5

PROXY_LINE_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks5)://)?"
    r"(?:(?P<user>[^:@]+):(?P<password>[^@]+)@)?"
    r"(?P<host>[\w.\-]+):(?P<port>\d+)$",
    re.IGNORECASE,
)

FREE_PROXY_SOURCES = (
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
)


@dataclass
class GeminiHealth:
    ok: bool
    model: str
    latency_ms: float
    message: str


@dataclass
class ProxyHealth:
    ok: bool
    proxy_id: Optional[int]
    proxy_url: Optional[str]
    latency_ms: Optional[float]
    message: str


class ProxyFormatError(ValueError):
    """Raised when proxy string cannot be parsed."""


def parse_proxy_string(raw: str) -> str:
    """
    Accept formats:
      - ip:port:user:pass
      - user:pass@ip:port
      - socks5://user:pass@ip:port
      - http://ip:port
    Returns normalized proxy URL for httpx.
    """
    text = raw.strip()
    if not text:
        raise ProxyFormatError("Пустая строка прокси")

    if "://" not in text and text.count(":") == 3:
        host, port, user, password = text.split(":", 3)
        return f"socks5://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"

    match = PROXY_LINE_RE.match(text)
    if not match:
        raise ProxyFormatError(
            "Неверный формат. Используй ip:port:user:pass или socks5://user:pass@ip:port"
        )

    scheme = (match.group("scheme") or "socks5").lower()
    host = match.group("host")
    port = match.group("port")
    user = match.group("user")
    password = match.group("password")

    if user and password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    else:
        auth = ""
    return f"{scheme}://{auth}{host}:{port}"


class ProxyParser:
    """Fetches and normalizes public SOCKS5 proxies."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def fetch_public_proxies(self, limit: int = 50) -> list[str]:
        proxies: list[str] = []
        seen: set[str] = set()

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for source in FREE_PROXY_SOURCES:
                try:
                    response = await client.get(source)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning("Failed to fetch proxy list from %s: %s", source, exc)
                    continue

                for line in response.text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        normalized = parse_proxy_string(line)
                    except ProxyFormatError:
                        try:
                            normalized = parse_proxy_string(f"socks5://{line}")
                        except ProxyFormatError:
                            continue
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    proxies.append(normalized)
                    if len(proxies) >= limit:
                        return proxies
        return proxies


class GeminiEngine:
    """Async Gemini client with proxy support, validation, and rotation."""

    def __init__(
        self,
        db: Database,
        *,
        model: str = DEFAULT_GEMINI_MODEL,
        request_timeout: float = 45.0,
    ) -> None:
        self.db = db
        self.model = model
        self.request_timeout = request_timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._current_proxy_url: Optional[str] = None
        self._rotation_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._proxy_parser = ProxyParser()
        self.last_gemini_health: Optional[GeminiHealth] = None
        self.last_proxy_health: Optional[ProxyHealth] = None

    async def start(self) -> None:
        await self._rebuild_client()
        self._stop_event.clear()
        if self._rotation_task is None or self._rotation_task.done():
            self._rotation_task = asyncio.create_task(
                self._rotation_loop(),
                name="proxy-rotation",
            )
        logger.info("GeminiEngine started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._rotation_task and not self._rotation_task.done():
            self._rotation_task.cancel()
            try:
                await self._rotation_task
            except asyncio.CancelledError:
                pass
        await self.close()
        logger.info("GeminiEngine stopped")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._current_proxy_url = None

    async def _rebuild_client(self, proxy_url: Optional[str] = None) -> None:
        if proxy_url is None:
            active = await self.db.get_active_proxy()
            proxy_url = active.proxy_url if active else None

        if self._client is not None and proxy_url == self._current_proxy_url:
            return

        if self._client is not None:
            await self._client.aclose()

        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.request_timeout),
            "follow_redirects": True,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url

        self._client = httpx.AsyncClient(**kwargs)
        self._current_proxy_url = proxy_url
        logger.debug("HTTP client rebuilt, proxy=%s", proxy_url or "direct")

    async def register_proxy(self, raw_proxy: str, *, source: str = "manual") -> ProxyRecord:
        proxy_url = parse_proxy_string(raw_proxy)
        proxy_id = await self.db.add_proxy(proxy_url, source=source)
        record = await self.db.get_proxy(proxy_id)
        if record is None:
            raise RuntimeError(f"Proxy {proxy_id} not found after insert")
        return record

    async def validate_and_activate(self, api_key: str, raw_proxy: str) -> tuple[GeminiHealth, ProxyHealth]:
        proxy = await self.register_proxy(raw_proxy, source="manual")
        health = await self._test_proxy_with_gemini(api_key, proxy)
        if not health.ok:
            await self.db.update_proxy_health(proxy.id, failed=True)
            raise RuntimeError(health.message)

        await self.db.set_proxy_active(proxy.id)
        await self.db.set_gemini_api_key(api_key)
        await self._rebuild_client(proxy.proxy_url)
        gemini_health = await self.test_gemini(api_key)
        if not gemini_health.ok:
            raise RuntimeError(gemini_health.message)
        return gemini_health, health

    async def test_gemini(self, api_key: Optional[str] = None) -> GeminiHealth:
        api_key = api_key or await self.db.get_gemini_api_key()
        if not api_key:
            health = GeminiHealth(False, self.model, 0.0, "API key не задан")
            self.last_gemini_health = health
            return health

        started = time.perf_counter()
        try:
            text = await self.generate_text(
                api_key=api_key,
                system_prompt="ты тестовый бот",
                messages=[{"role": "user", "content": GEMINI_TEST_PROMPT}],
                max_output_tokens=16,
            )
            latency = (time.perf_counter() - started) * 1000
            ok = bool(text.strip())
            health = GeminiHealth(ok, self.model, latency, text.strip() or "empty response")
        except Exception as exc:
            await self.db.log_error(exc, error_type="GeminiHealthCheck")
            latency = (time.perf_counter() - started) * 1000
            health = GeminiHealth(False, self.model, latency, str(exc))

        self.last_gemini_health = health
        return health

    async def _test_proxy_with_gemini(self, api_key: str, proxy: ProxyRecord) -> ProxyHealth:
        started = time.perf_counter()
        temp_client: Optional[httpx.AsyncClient] = None
        try:
            temp_client = httpx.AsyncClient(
                proxy=proxy.proxy_url,
                timeout=httpx.Timeout(self.request_timeout),
                follow_redirects=True,
            )
            url = f"{GEMINI_BASE_URL}/models/{self.model}:generateContent"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": GEMINI_TEST_PROMPT}]}],
                "generationConfig": {"maxOutputTokens": 8, "temperature": 0.2},
            }
            response = await temp_client.post(
                url,
                params={"key": api_key},
                json=payload,
            )
            latency = (time.perf_counter() - started) * 1000
            if response.status_code == 429:
                return ProxyHealth(False, proxy.id, proxy.proxy_url, latency, "Gemini rate limit (429)")
            if response.status_code >= 400:
                return ProxyHealth(
                    False,
                    proxy.id,
                    proxy.proxy_url,
                    latency,
                    f"HTTP {response.status_code}: {response.text[:200]}",
                )

            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return ProxyHealth(False, proxy.id, proxy.proxy_url, latency, "Gemini вернул пустой ответ")

            await self.db.update_proxy_health(proxy.id, latency_ms=latency, failed=False)
            health = ProxyHealth(True, proxy.id, proxy.proxy_url, latency, "ok")
            self.last_proxy_health = health
            return health
        except Exception as exc:
            latency = (time.perf_counter() - started) * 1000
            await self.db.log_error(
                exc,
                error_type="ProxyValidation",
                context={"proxy_id": proxy.id, "proxy_url": proxy.proxy_url},
            )
            health = ProxyHealth(False, proxy.id, proxy.proxy_url, latency, str(exc))
            self.last_proxy_health = health
            return health
        finally:
            if temp_client is not None:
                await temp_client.aclose()

    async def generate_reply(
        self,
        *,
        system_prompt: str,
        context_messages: list[dict[str, str]],
        user_message: str,
    ) -> str:
        api_key = await self.db.get_gemini_api_key()
        if not api_key:
            raise RuntimeError("Gemini API key не настроен")

        messages = list(context_messages)
        messages.append({"role": "user", "content": user_message, "author": "peer"})
        text = await self.generate_text(
            api_key=api_key,
            system_prompt=system_prompt,
            messages=messages,
        )
        return text.strip()

    async def generate_text(
        self,
        *,
        api_key: str,
        system_prompt: str,
        messages: list[dict[str, str]],
        max_output_tokens: int = 512,
        temperature: float = 0.95,
    ) -> str:
        await self._rebuild_client()
        if self._client is None:
            raise RuntimeError("HTTP client is not initialized")

        contents = []
        for item in messages:
            role = item.get("role", "user")
            author = item.get("author") or ("я" if role == "assistant" else "собеседник")
            text = item.get("content", "").strip()
            if not text:
                continue
            gemini_role = "model" if role == "assistant" else "user"
            prefix = f"{author}: " if gemini_role == "user" else ""
            contents.append(
                {
                    "role": gemini_role,
                    "parts": [{"text": f"{prefix}{text}"}],
                }
            )

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        }

        url = f"{GEMINI_BASE_URL}/models/{self.model}:generateContent"
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.post(
                    url,
                    params={"key": api_key},
                    json=payload,
                )
                if response.status_code == 429:
                    delay = BASE_BACKOFF_SECONDS * (2 ** attempt)
                    logger.warning("Gemini 429, backoff %.1fs (attempt %s)", delay, attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                data = response.json()
                return self._extract_text(data)
            except httpx.TimeoutException as exc:
                last_error = exc
                await self.db.log_error(exc, error_type="GeminiTimeout")
                await self._handle_proxy_failure()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                await self.db.log_error(exc, error_type="GeminiHTTPError")
                if exc.response.status_code in {401, 403}:
                    raise
                if exc.response.status_code >= 500:
                    delay = BASE_BACKOFF_SECONDS * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception as exc:
                last_error = exc
                await self.db.log_error(exc, error_type="GeminiRequest")
                delay = BASE_BACKOFF_SECONDS * (2 ** attempt)
                await asyncio.sleep(delay)

        raise RuntimeError(f"Gemini request failed after retries: {last_error}")

    async def parse_and_store_public_proxies(self, limit: int = 30) -> list[ProxyRecord]:
        raw_list = await self._proxy_parser.fetch_public_proxies(limit=limit)
        stored: list[ProxyRecord] = []
        for raw in raw_list:
            try:
                proxy_id = await self.db.add_proxy(raw, source="public")
                record = await self.db.get_proxy(proxy_id)
                if record:
                    stored.append(record)
            except Exception as exc:
                await self.db.log_error(exc, error_type="ProxyStore")
        return stored

    async def ensure_working_proxy(self, api_key: Optional[str] = None) -> Optional[ProxyRecord]:
        api_key = api_key or await self.db.get_gemini_api_key()
        if not api_key:
            return None

        active = await self.db.get_active_proxy()
        if active:
            health = await self._test_proxy_with_gemini(api_key, active)
            if health.ok:
                await self._rebuild_client(active.proxy_url)
                return active
            await self.db.deactivate_proxy(active.id)

        for proxy in await self.db.list_proxies():
            if proxy.is_active:
                continue
            health = await self._test_proxy_with_gemini(api_key, proxy)
            if health.ok:
                await self.db.set_proxy_active(proxy.id)
                await self._rebuild_client(proxy.proxy_url)
                return proxy

        parsed = await self.parse_and_store_public_proxies(limit=20)
        for proxy in parsed:
            health = await self._test_proxy_with_gemini(api_key, proxy)
            if health.ok:
                await self.db.set_proxy_active(proxy.id)
                await self._rebuild_client(proxy.proxy_url)
                return proxy

        return None

    async def _handle_proxy_failure(self) -> None:
        active = await self.db.get_active_proxy()
        if active:
            await self.db.update_proxy_health(active.id, failed=True)
            if active.fail_count >= 2:
                await self.db.deactivate_proxy(active.id)
        await self.ensure_working_proxy()

    async def _rotation_loop(self) -> None:
        logger.info("Proxy rotation loop started")
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=180)
                break
            except asyncio.TimeoutError:
                pass

            api_key = await self.db.get_gemini_api_key()
            if not api_key:
                continue

            try:
                await self.ensure_working_proxy(api_key)
                await self.test_gemini(api_key)
            except Exception as exc:
                await self.db.log_error(exc, error_type="ProxyRotation")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini response has no candidates")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        texts = [part.get("text", "") for part in parts if part.get("text")]
        if not texts:
            raise RuntimeError("Gemini response has no text parts")
        return "".join(texts)
