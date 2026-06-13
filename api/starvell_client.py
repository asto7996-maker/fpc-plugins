"""
Асинхронный HTTP-клиент Starvell с retry, backoff 429 и прокси.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from starvell_api import StarvellAPI

logger = logging.getLogger("starvell.api.client")


class StarvellClient(StarvellAPI):
    """
    Расширенный клиент Starvell:
    - экспоненциальный backoff при 429 Too Many Requests
    - опциональный HTTP-прокси
    - до 4 повторов на запрос
    """

    def __init__(
        self,
        *args,
        proxy: str | None = None,
        max_retries: int = 4,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._proxy = proxy
        self._max_retries = max_retries

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

        backoff = ExponentialBackoff(base=1.5, factor=2.0, max_delay=45.0)
        last_exc: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                await self._throttle()
                async with httpx.AsyncClient(
                    cookies=self._cookies(),
                    headers=self._headers(
                        referer or f"https://starvell.com/",
                        json_request=not next_data and method != "GET",
                    ),
                    timeout=30.0,
                    follow_redirects=True,
                    proxy=self._proxy,
                ) as client:
                    if method == "GET":
                        resp = await client.get(url)
                    else:
                        resp = await client.post(url, json=json_body)

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
                await backoff.sleep()

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Starvell request failed after {self._max_retries} retries: {url}")
