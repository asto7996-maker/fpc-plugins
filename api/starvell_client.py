"""
Асинхронный HTTP-клиент Starvell с retry, backoff 429 и прокси.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from starvell_api import StarvellAPI

logger = logging.getLogger("starvell.api.client")


class StarvellClient(StarvellAPI):
    """
    Расширенный клиент Starvell:
    - экспоненциальный backoff при 429 Too Many Requests
    - опциональный HTTP-прокси
    - до 3 повторов на запрос
    """

    def __init__(
        self,
        *args,
        proxy: str | None = None,
        max_retries: int = 3,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._proxy = proxy
        self._max_retries = max_retries
        if proxy:
            self._proxy = proxy

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            kw: dict = {
                "cookies": self._cookies(),
                "timeout": 15.0,
                "follow_redirects": True,
                "limits": httpx.Limits(max_connections=24, max_keepalive_connections=12),
            }
            if self._proxy:
                kw["proxy"] = self._proxy
            self._http = httpx.AsyncClient(**kw)
        else:
            self._http.cookies.update(self._cookies())
        return self._http

    async def _request(
        self,
        method: str,
        url: str,
        *,
        referer: str | None = None,
        json_body: dict | None = None,
        next_data: bool = False,
    ) -> httpx.Response:
        from api.rate_limiter import ExponentialBackoff

        backoff = ExponentialBackoff(base=0.8, factor=2.0, max_delay=20.0)
        last_exc: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                resp = await super()._request(
                    method, url,
                    referer=referer,
                    json_body=json_body,
                    next_data=next_data,
                )
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", backoff.next_delay()))
                    logger.warning("[%s] 429 rate limit, sleep %.1fs", self.account_name, retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code >= 500 and attempt < self._max_retries - 1:
                    await backoff.sleep()
                    continue
                backoff.reset()
                return resp
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                logger.warning("[%s] network error attempt %d: %s", self.account_name, attempt + 1, exc)
                if attempt < self._max_retries - 1:
                    await backoff.sleep()

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Starvell request failed after {self._max_retries} retries: {url}")
