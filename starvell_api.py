"""
Асинхронный клиент Starvell API.
Работает через cookie-сессию, как браузер. Встроен rate-limiter против флуда.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import string
import time
from typing import Any

import httpx

logger = logging.getLogger("starvell.api")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
BASE_URL = "https://starvell.com"


class StarvellAPIError(RuntimeError):
    """Ошибка Starvell API с телом ответа."""

    def __init__(self, status: int, message: str, body: dict | None = None) -> None:
        self.status = status
        self.body = body or {}
        super().__init__(message or f"HTTP {status}")


class RateLimiter:
    """Ограничитель частоты запросов к Starvell."""

    def __init__(self, max_per_minute: int = 40) -> None:
        self._min_interval = 60.0 / max(1, min(max_per_minute, 40))
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if self._next_allowed <= 0:
                self._next_allowed = now
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = loop.time()
            self._next_allowed = now + self._min_interval


class StarvellAPI:
    """Клиент для взаимодействия с маркетплейсом Starvell."""

    _build_id: str | None = None
    _build_id_at: float = 0.0
    _build_lock: asyncio.Lock | None = None
    _BUILD_TTL = 1800

    @classmethod
    def _get_build_lock(cls) -> asyncio.Lock:
        if cls._build_lock is None:
            cls._build_lock = asyncio.Lock()
        return cls._build_lock

    def __init__(
        self,
        session_cookie: str,
        sid_cookie: str = "",
        my_games_cookie: str = "",
        delay_seconds: float = 1.5,
        max_per_minute: int = 40,
        account_name: str = "default",
    ) -> None:
        self.session_cookie = session_cookie
        self.sid_cookie = sid_cookie
        self.my_games_cookie = my_games_cookie
        self.account_name = account_name
        self._limiter = RateLimiter(max_per_minute)
        self._extra_delay = max(0.0, delay_seconds)

    def _cookies(self) -> dict[str, str]:
        cookies = {
            "session": self.session_cookie,
            "starvell.theme": "dark",
            "starvell.time_zone": "Europe/Moscow",
        }
        if self.sid_cookie:
            cookies["sid"] = self.sid_cookie
        if self.my_games_cookie:
            cookies["starvell.my_games"] = self.my_games_cookie
        return cookies

    def _headers(self, referer: str = f"{BASE_URL}/", json_request: bool = False) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-language": "ru,en;q=0.9",
            "referer": referer,
            "user-agent": USER_AGENT,
        }
        if json_request:
            headers["content-type"] = "application/json"
            headers["origin"] = BASE_URL
        else:
            headers["x-nextjs-data"] = "1"
        return headers

    async def _throttle(self) -> None:
        await self._limiter.wait()
        if self._extra_delay > 0:
            await asyncio.sleep(self._extra_delay)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        referer: str | None = None,
        json_body: dict | None = None,
        next_data: bool = False,
    ) -> httpx.Response:
        await self._throttle()
        use_json = json_body is not None
        async with httpx.AsyncClient(
            cookies=self._cookies(),
            headers=self._headers(
                referer or f"{BASE_URL}/",
                json_request=use_json and not next_data and method != "GET",
            ),
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            if method == "GET":
                resp = await client.get(url)
            elif use_json:
                resp = await client.post(url, json=json_body)
            else:
                resp = await client.post(url)
            return resp

    async def get_build_id(self) -> str:
        """Получает buildId Next.js (кешируется на 30 минут)."""
        async with self._get_build_lock():
            now = time.time()
            if StarvellAPI._build_id and (now - StarvellAPI._build_id_at) < self._BUILD_TTL:
                return StarvellAPI._build_id

            await self._throttle()
            async with httpx.AsyncClient(
                cookies=self._cookies(),
                headers={"user-agent": USER_AGENT, "accept": "text/html"},
                timeout=30.0,
            ) as client:
                resp = await client.get(f"{BASE_URL}/")
                resp.raise_for_status()
                html = resp.text

            match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                html,
                re.DOTALL,
            )
            if not match:
                raise RuntimeError("Не удалось найти __NEXT_DATA__ на главной странице")
            data = json.loads(match.group(1))
            build_id = data.get("buildId")
            if not build_id:
                raise RuntimeError("buildId не найден")
            StarvellAPI._build_id = str(build_id)
            StarvellAPI._build_id_at = now
            return StarvellAPI._build_id

    @classmethod
    def reset_build_id(cls) -> None:
        cls._build_id = None
        cls._build_id_at = 0.0

    async def _next_data_get(self, path: str, referer: str) -> dict[str, Any]:
        """GET-запрос к Next.js data endpoint с авто-обновлением buildId."""
        for attempt in range(2):
            build_id = await self.get_build_id()
            url = f"{BASE_URL}/_next/data/{build_id}/{path}"
            resp = await self._request("GET", url, referer=referer, next_data=True)
            if resp.status_code == 404 and attempt == 0:
                self.reset_build_id()
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Не удалось загрузить {path}")

    # ── Авторизация и профиль ─────────────────────────────────────────────

    async def fetch_homepage(self) -> dict[str, Any]:
        """Проверяет авторизацию и возвращает данные пользователя."""
        data = await self._next_data_get("index.json", f"{BASE_URL}/")
        props = data.get("pageProps", {})
        user = props.get("user")
        sid = props.get("sid") or self.sid_cookie
        if sid:
            self.sid_cookie = str(sid)
        my_games = props.get("my_games") or self.my_games_cookie
        if my_games:
            self.my_games_cookie = str(my_games)
        return {
            "authorized": bool(user),
            "user": user,
            "sid": self.sid_cookie,
            "my_games": self.my_games_cookie,
            "balance": (user or {}).get("balance"),
        }

    async def get_balance(self) -> float | None:
        """Возвращает баланс аккаунта."""
        wallet = await self.fetch_wallet_balance()
        if wallet:
            raw = wallet.get("withdrawableRubBalance")
            if raw is None:
                raw = wallet.get("rubBalance")
            if raw is not None:
                try:
                    val = float(raw)
                    if val >= 100 and abs(val - round(val)) < 1e-9:
                        val = val / 100.0
                    return val
                except (TypeError, ValueError):
                    pass
        info = await self.fetch_homepage()
        user = info.get("user") or {}
        balance = user.get("balance")
        if balance is None:
            return None
        try:
            return float(balance)
        except (TypeError, ValueError):
            return None

    async def fetch_wallet_balance(self) -> dict[str, Any]:
        """GET /api/wallet/balance (как Lumus LSB)."""
        try:
            resp = await self._request(
                "GET",
                f"{BASE_URL}/api/wallet/balance",
                referer=f"{BASE_URL}/wallet",
            )
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("fetch_wallet_balance: %s", exc)
        return {}

    # ── Заказы ────────────────────────────────────────────────────────────

    async def fetch_orders(self, page: int | None = None) -> list[dict[str, Any]]:
        """Загружает список продаж (заказов продавца)."""
        path = "account/sells.json"
        if page and page > 1:
            path += f"?page={page}"
        data = await self._next_data_get(path, f"{BASE_URL}/account/sells")
        return (data.get("pageProps") or {}).get("orders") or []

    async def fetch_order(self, order_id: str) -> dict[str, Any]:
        """Полная карточка заказа (pageProps как в Lumus LSB)."""
        details = await self.fetch_order_details(order_id)
        if not details:
            return {}
        order = details.get("order") or details.get("orderDetails") or {}
        if isinstance(order, dict) and order:
            merged = {**details, **order}
            if details.get("offerDetails"):
                merged["offerDetails"] = details["offerDetails"]
            if details.get("offer"):
                merged["offer"] = details["offer"]
            return merged
        return details

    async def fetch_order_details(self, order_id: str) -> dict[str, Any]:
        """pageProps страницы заказа (Lumus: Account.fetch_order_details)."""
        oid = str(order_id or "").strip()
        if not oid:
            return {}
        for path, referer in (
            (f"order/{oid}.json", f"{BASE_URL}/order/{oid}"),
            (f"account/sells/{oid}.json", f"{BASE_URL}/account/sells"),
        ):
            try:
                data = await self._next_data_get(path, referer)
                props = data.get("pageProps") or {}
                if isinstance(props, dict) and props:
                    return props
            except Exception as exc:
                logger.debug("fetch_order_details path %s #%s: %s", path, oid[:12], exc)
        try:
            data = await self._next_data_get(f"order/{oid}.json", f"{BASE_URL}/order/{oid}")
            return data.get("pageProps") or {}
        except Exception as exc:
            logger.warning("fetch_order_details %s: %s", oid[:12], exc)
            return {}

    @staticmethod
    def offer_id_from_order(order: dict[str, Any]) -> str:
        """ID лота из объекта заказа (разные поля API)."""
        for key in ("offerId", "offer_id", "lotId", "lot_id"):
            val = order.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        for block_key in ("offerDetails", "offer"):
            block = order.get(block_key) or {}
            if not isinstance(block, dict):
                continue
            for key in ("id", "offerId", "offer_id"):
                val = block.get(key)
                if val is not None and str(val).strip():
                    return str(val).strip()
        return ""

    @staticmethod
    def _looks_like_offer(data: dict[str, Any]) -> bool:
        if not isinstance(data, dict) or not data or data.get("offerNotFound"):
            return False
        if not (data.get("id") or data.get("publicId")):
            return False
        return any(key in data for key in ("categoryId", "descriptions", "type", "price"))

    async def fetch_offer(self, offer_id: str) -> dict[str, Any]:
        """Описание лота — для поиска ID: / #Quan: в тексте."""
        oid = str(offer_id or "").strip()
        if not oid:
            return {}
        for path, referer in (
            (f"offers/{oid}.json", f"{BASE_URL}/offers/{oid}"),
            (f"offer/{oid}.json", f"{BASE_URL}/offer/{oid}"),
        ):
            try:
                data = await self._next_data_get(path, referer)
                props = data.get("pageProps") or {}
                for key in ("offer", "offerDetails", "lot"):
                    offer = props.get(key) or {}
                    if self._looks_like_offer(offer):
                        return offer
            except Exception as exc:
                logger.debug("fetch_offer path %s: %s", path, exc)
        return {}

    async def fetch_all_orders(self, max_pages: int = 20) -> list[dict[str, Any]]:
        """Загружает все страницы заказов."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            orders = await self.fetch_orders(page if page > 1 else None)
            if not orders:
                break
            for order in orders:
                oid = str((order or {}).get("id") or "")
                if oid and oid not in seen:
                    seen.add(oid)
                    items.append(order)
        return items

    async def refund_order(self, order_id: str) -> dict[str, Any]:
        """Возврат средств по заказу."""
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/orders/refund",
            referer=f"{BASE_URL}/order/{order_id}",
            json_body={"orderId": order_id},
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}

    # ── Лоты и бамп ───────────────────────────────────────────────────────

    async def fetch_my_offers(
        self,
        *,
        category_id: int | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """POST /api/offers/list-my — лоты текущего продавца (без _next/data)."""
        body: dict[str, Any] = {"limit": max(1, limit), "offset": max(0, offset)}
        if category_id is not None:
            body["categoryId"] = int(category_id)
        try:
            resp = await self._request(
                "POST",
                f"{BASE_URL}/api/offers/list-my",
                referer=f"{BASE_URL}/account/sells",
                json_body=body,
            )
            if resp.status_code >= 400:
                logger.debug("fetch_my_offers HTTP %s: %s", resp.status_code, resp.text[:300])
                return []
            data = resp.json()
        except Exception as exc:
            logger.debug("fetch_my_offers: %s", exc)
            return []

        items: list[Any] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("offers", "items", "data", "results"):
                val = data.get(key)
                if isinstance(val, list):
                    items = val
                    break

        lots: list[dict[str, Any]] = []
        for offer in items:
            if not isinstance(offer, dict):
                continue
            lots.append(self._normalize_seller_offer_row(offer))
        return [lot for lot in lots if lot.get("id") or lot.get("public_id")]

    @staticmethod
    def _normalize_seller_offer_row(offer: dict[str, Any]) -> dict[str, Any]:
        category = offer.get("category") if isinstance(offer.get("category"), dict) else {}
        game = offer.get("game") if isinstance(offer.get("game"), dict) else {}
        if not game and isinstance(category.get("game"), dict):
            game = category["game"]
        desc = (offer.get("descriptions") or {}).get("rus") or {}
        title = desc.get("briefDescription") or desc.get("description") or ""
        cat_id = offer.get("categoryId") or category.get("id")
        game_id = offer.get("gameId") or category.get("gameId") or game.get("id")
        game_slug = game.get("slug") or (category.get("game") or {}).get("slug")
        cat_slug = category.get("slug")
        return {
            "id": offer.get("id"),
            "public_id": offer.get("publicId"),
            "title": str(title).strip(),
            "price": offer.get("price"),
            "availability": offer.get("availability"),
            "is_active": offer.get("isActive", True),
            "category_id": cat_id,
            "game_id": game_id,
            "category_url": f"{BASE_URL}/{game_slug}/{cat_slug}/trade" if game_slug and cat_slug else None,
        }

    async def fetch_user_lots(self, user_id: int) -> list[dict[str, Any]]:
        """Получает лоты пользователя с категориями (fallback через _next/data)."""
        try:
            data = await self._next_data_get(
                f"users/{user_id}.json?user_id={user_id}",
                f"{BASE_URL}/users/{user_id}",
            )
        except Exception as exc:
            logger.warning("fetch_user_lots %s: %s", user_id, exc)
            return []

        props = data.get("pageProps", {})
        offers_block = props.get("userProfileOffers") or (props.get("bff") or {}).get("userProfileOffers")
        categories = offers_block or props.get("categoriesWithOffers") or []
        lots: list[dict[str, Any]] = []

        for category in categories:
            if not isinstance(category, dict):
                continue
            cat_id = category.get("id")
            game_id = category.get("gameId") or (category.get("game") or {}).get("id")
            game_slug = (category.get("game") or {}).get("slug")
            cat_slug = category.get("slug")
            for offer in category.get("offers") or []:
                if not isinstance(offer, dict):
                    continue
                row = self._normalize_seller_offer_row({
                    **offer,
                    "categoryId": cat_id,
                    "gameId": game_id,
                    "category": category,
                })
                if row.get("id"):
                    lots.append(row)
        return lots

    async def create_offer(
        self,
        payload: dict[str, Any],
        *,
        referer: str | None = None,
        category_id: int | None = None,
        game_slug: str = "",
        category_slug: str = "",
        finalize_mode: str = "frontend",
    ) -> dict[str, Any]:
        """POST /api/offers/create — создание нового лота."""
        from services.starvell_catalog import (
            finalize_create_payload,
            finalize_frontend_create_payload,
            finalize_unified_create_payload,
            payload_attribute_stats,
            strip_all_attributes,
        )

        mode = (finalize_mode or "frontend").lower()
        if mode == "frontend":
            clean_payload = finalize_frontend_create_payload(payload)
        elif mode == "unified":
            clean_payload = finalize_unified_create_payload(payload)
        elif mode == "basic":
            clean_payload = finalize_create_payload(payload)
        elif mode == "none":
            clean_payload = strip_all_attributes(finalize_create_payload(payload))
        else:
            clean_payload = finalize_frontend_create_payload(payload)

        logger.info("create_offer %s", payload_attribute_stats(clean_payload))
        logger.debug("create_offer body: %s", json.dumps(clean_payload, ensure_ascii=False)[:4000])
        if not referer and game_slug and category_slug:
            referer = f"{BASE_URL}/{game_slug}/{category_slug}/sell"
        elif not referer and category_id:
            referer = f"{BASE_URL}/"
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/offers/create",
            referer=referer or f"{BASE_URL}/",
            json_body=clean_payload,
        )
        result: dict[str, Any] = {"status": resp.status_code, "success": 200 <= resp.status_code < 300}
        body: dict[str, Any] = {}
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                body = parsed
            result["json"] = parsed
        except Exception:
            result["raw"] = resp.text[:2000]
        if resp.status_code >= 400:
            msg = ""
            if isinstance(body, dict):
                msg = str(body.get("message") or body.get("error") or body.get("detail") or "")
            if not msg:
                msg = resp.text[:500]
            logger.warning(
                "create_offer HTTP %s: %s | payload=%s",
                resp.status_code,
                msg,
                json.dumps(clean_payload, ensure_ascii=False)[:2000],
            )
            raise StarvellAPIError(resp.status_code, msg, body if isinstance(body, dict) else {"raw": resp.text[:500]})
        return result.get("json") or result

    async def fetch_offer_draft(
        self,
        category_id: int,
        *,
        sub_category_id: int | None = None,
        referer: str | None = None,
        game_slug: str = "",
        category_slug: str = "",
    ) -> dict[str, Any]:
        """GET /api/offers/draft — черновик формы продажи (может содержать bloated attrs)."""
        query = f"categoryId={int(category_id)}"
        if sub_category_id:
            query += f"&subCategoryId={int(sub_category_id)}"
        if not referer and game_slug and category_slug:
            referer = f"{BASE_URL}/{game_slug}/{category_slug}/trade"
        resp = await self._request(
            "GET",
            f"{BASE_URL}/api/offers/draft?{query}",
            referer=referer or f"{BASE_URL}/",
        )
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 400:
            logger.debug("fetch_offer_draft HTTP %s: %s", resp.status_code, resp.text[:300])
            return {}
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def partial_update_offer(
        self,
        offer_id: str,
        payload: dict[str, Any],
        *,
        referer: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/offers/{id}/partial-update — частичное обновление лота."""
        oid = str(offer_id or "").strip()
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/offers/{oid}/partial-update",
            referer=referer or f"{BASE_URL}/offers/{oid}",
            json_body=payload,
        )
        result: dict[str, Any] = {"status": resp.status_code, "success": 200 <= resp.status_code < 300}
        try:
            result["json"] = resp.json()
        except Exception:
            result["raw"] = resp.text[:2000]
        if resp.status_code >= 400:
            msg = resp.text[:500]
            if isinstance(result.get("json"), dict):
                msg = str(result["json"].get("message") or msg)
            logger.warning("partial_update_offer HTTP %s: %s", resp.status_code, msg)
            raise StarvellAPIError(resp.status_code, msg, result.get("json") if isinstance(result.get("json"), dict) else {})
        return result.get("json") or result

    async def update_offer(
        self,
        offer_id: str,
        payload: dict[str, Any],
        *,
        referer: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/offers/{id}/update — полное обновление лота."""
        oid = str(offer_id or "").strip()
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/offers/{oid}/update",
            referer=referer or f"{BASE_URL}/offers/{oid}",
            json_body=payload,
        )
        result: dict[str, Any] = {"status": resp.status_code, "success": 200 <= resp.status_code < 300}
        try:
            result["json"] = resp.json()
        except Exception:
            result["raw"] = resp.text[:2000]
        if resp.status_code >= 400:
            msg = resp.text[:500]
            if isinstance(result.get("json"), dict):
                msg = str(result["json"].get("message") or msg)
            logger.warning("update_offer HTTP %s: %s", resp.status_code, msg)
            raise StarvellAPIError(
                resp.status_code,
                msg,
                result.get("json") if isinstance(result.get("json"), dict) else {},
            )
        return result.get("json") or result

    @staticmethod
    def _build_offer_ref_body(
        offer_id: str | int | None = None,
        *,
        public_id: str | None = None,
    ) -> dict[str, Any]:
        pid = str(public_id or "").strip()
        oid = str(offer_id or "").strip()
        if pid:
            return {"publicId": pid}
        if oid.isdigit():
            return {"id": int(oid)}
        if oid and re.fullmatch(r"[0-9a-f-]{8,}", oid, re.IGNORECASE):
            return {"publicId": oid}
        if oid:
            return {"id": oid}
        raise ValueError("offer id required")

    @staticmethod
    def _offer_id_candidates(
        offer_id: str | int | None = None,
        *,
        public_id: str | None = None,
    ) -> list[str]:
        out: list[str] = []
        for candidate in (offer_id, public_id):
            text = str(candidate or "").strip()
            if text and text not in out:
                out.append(text)
        return out

    async def delete_offer(
        self,
        offer_id: str | int,
        *,
        public_id: str | None = None,
        referer: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/offers/{id}/delete — удаление лота."""
        last_exc: StarvellAPIError | None = None
        for oid in self._offer_id_candidates(offer_id, public_id=public_id):
            resp = await self._request(
                "POST",
                f"{BASE_URL}/api/offers/{oid}/delete",
                referer=referer or f"{BASE_URL}/offers/{oid}",
                json_body={},
            )
            if resp.status_code < 400:
                try:
                    return resp.json()
                except Exception:
                    return {"success": True}
            msg = resp.text[:500]
            body: dict[str, Any] = {}
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
                    msg = str(parsed.get("message") or msg)
            except Exception:
                pass
            last_exc = StarvellAPIError(resp.status_code, msg, body)
        if last_exc:
            raise last_exc
        raise StarvellAPIError(400, "offer id required", {})

    async def deactivate_offer(
        self,
        offer_id: str | int,
        *,
        public_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/offers/deactivate — отключить лот."""
        body = self._build_offer_ref_body(offer_id, public_id=public_id)
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/offers/deactivate",
            referer=f"{BASE_URL}/account/sells",
            json_body=body,
        )
        if resp.status_code >= 400:
            msg = resp.text[:500]
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    msg = str(parsed.get("message") or msg)
            except Exception:
                parsed = {}
            raise StarvellAPIError(resp.status_code, msg, parsed if isinstance(parsed, dict) else {})
        try:
            return resp.json()
        except Exception:
            return {"success": True}

    async def activate_offer(
        self,
        offer_id: str | int,
        *,
        public_id: str | None = None,
    ) -> dict[str, Any]:
        """Включить лот (partial-update isActive=true)."""
        last_exc: StarvellAPIError | None = None
        for oid in self._offer_id_candidates(offer_id, public_id=public_id):
            try:
                return await self.partial_update_offer(oid, {"isActive": True})
            except StarvellAPIError as exc:
                last_exc = exc
        if last_exc:
            raise last_exc
        raise StarvellAPIError(400, "offer id required", {})

    async def fetch_seller_categories(self, user_id: int | None = None) -> list[dict[str, Any]]:
        """
        Категории продавца для создания лота — из уже выставленных предложений.
        Возвращает список {category_id, title, offer_id, game_id, price}.
        """
        lots = await self.fetch_my_offers(limit=200)
        if not lots:
            if user_id is None:
                try:
                    info = await self.fetch_homepage()
                    user = info.get("user") or {}
                    user_id = user.get("id")
                except Exception as exc:
                    logger.debug("fetch_seller_categories homepage: %s", exc)
                    user_id = None
            if user_id:
                lots = await self.fetch_user_lots(int(user_id))

        seen: set[int] = set()
        categories: list[dict[str, Any]] = []
        for lot in lots:
            cat_id = lot.get("category_id")
            if cat_id is None:
                continue
            try:
                cid = int(cat_id)
            except (TypeError, ValueError):
                continue
            if cid in seen:
                continue
            seen.add(cid)
            categories.append({
                "category_id": cid,
                "title": str(lot.get("title") or f"Категория #{cid}")[:80],
                "offer_id": lot.get("id"),
                "game_id": lot.get("game_id"),
                "price": lot.get("price"),
            })
        return categories

    async def bump_offers(self, game_id: int, category_ids: list[int], referer: str | None = None) -> dict[str, Any]:
        """Поднимает лоты в указанных категориях."""
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/offers/bump",
            referer=referer or f"{BASE_URL}/",
            json_body={"gameId": game_id, "categoryIds": category_ids},
        )
        result: dict[str, Any] = {"status": resp.status_code, "success": 200 <= resp.status_code < 300}
        try:
            result["json"] = resp.json()
        except Exception:
            result["raw"] = resp.text[:2000]
        return result

    # ── Чаты и сообщения ──────────────────────────────────────────────────

    async def fetch_chats(self) -> list[dict[str, Any]]:
        """Список чатов."""
        data = await self._next_data_get("chat.json", f"{BASE_URL}/chat")
        return (data.get("pageProps") or {}).get("chats") or []

    async def fetch_messages(self, chat_id: str, limit: int = 50, interlocutor_id: int | None = None) -> list[dict]:
        """История сообщений чата."""
        if interlocutor_id is not None:
            resp = await self._request(
                "POST",
                f"{BASE_URL}/api/bff/chat-page",
                referer=f"{BASE_URL}/chat",
                json_body={
                    "interlocutorId": int(interlocutor_id),
                    "messagesListDto": {"chatId": chat_id, "limit": limit},
                },
            )
        else:
            resp = await self._request(
                "POST",
                f"{BASE_URL}/api/messages/list",
                referer=f"{BASE_URL}/chat",
                json_body={"chatId": chat_id, "limit": limit},
            )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = (data.get("messagesListResult") or {}).get("items")
            if isinstance(items, list):
                return items
        return []

    @staticmethod
    def _client_socket_id() -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(20))

    async def send_message(self, chat_id: str, content: str) -> dict[str, Any]:
        """Отправляет текстовое сообщение в чат Starvell (Lumus: clientSocketId обязателен)."""
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/messages/send",
            referer=f"{BASE_URL}/chat/{chat_id}",
            json_body={
                "chatId": chat_id,
                "clientSocketId": self._client_socket_id(),
                "content": content,
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code}

    async def mark_seller_completed(self, order_id: str) -> bool:
        """Отметить заказ выполненным продавцом (Lumus LSB)."""
        oid = str(order_id or "").strip()
        if not oid:
            return False
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/orders/{oid}/mark-seller-completed",
            referer=f"{BASE_URL}/order/{oid}",
            json_body={"id": oid},
        )
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("mark_seller_completed #%s: HTTP %s %s", oid, resp.status_code, resp.text[:200])
        return ok

    async def find_chat_by_buyer(self, buyer_id: int) -> str | None:
        """Находит chat_id по ID покупателя."""
        try:
            target = int(buyer_id)
        except (TypeError, ValueError):
            return None
        for chat in await self.fetch_chats():
            for participant in chat.get("participants") or []:
                pid = (participant or {}).get("id")
                try:
                    if pid is not None and int(pid) == target:
                        cid = chat.get("id")
                        return str(cid) if cid else None
                except (TypeError, ValueError):
                    continue
        return None

    async def send_review_reply(self, order_id: str, text: str) -> dict[str, Any]:
        """
        Отправляет ответ на отзыв покупателя (благодарность продавца).
        Использует BFF-эндпоинт Starvell.
        """
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/reviews/reply",
            referer=f"{BASE_URL}/order/{order_id}",
            json_body={"orderId": order_id, "content": text},
        )
        result = {"status": resp.status_code, "success": 200 <= resp.status_code < 300}
        try:
            result["json"] = resp.json()
        except Exception:
            result["raw"] = resp.text[:1000]
        return result

    def apply_watermark(self, text: str, enabled: bool, watermark: str) -> str:
        """Добавляет водяной знак к исходящему сообщению."""
        if enabled and watermark.strip():
            return f"{watermark.strip()}\n\n{text}"
        return text
