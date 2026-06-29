"""
Асинхронный клиент Starvell API.
Работает через cookie-сессию, как браузер. Встроен rate-limiter против флуда.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("starvell.api")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
BASE_URL = "https://starvell.com"


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
        async with httpx.AsyncClient(
            cookies=self._cookies(),
            headers=self._headers(referer or f"{BASE_URL}/", json_request=not next_data and method != "GET"),
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            if method == "GET":
                resp = await client.get(url)
            else:
                resp = await client.post(url, json=json_body)
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
        info = await self.fetch_homepage()
        user = info.get("user") or {}
        balance = user.get("balance")
        if balance is None:
            return None
        try:
            return float(balance)
        except (TypeError, ValueError):
            return None

    # ── Заказы ────────────────────────────────────────────────────────────

    async def fetch_orders(self, page: int | None = None) -> list[dict[str, Any]]:
        """Загружает список продаж (заказов продавца)."""
        path = "account/sells.json"
        if page and page > 1:
            path += f"?page={page}"
        data = await self._next_data_get(path, f"{BASE_URL}/account/sells")
        return (data.get("pageProps") or {}).get("orders") or []

    async def fetch_order(self, order_id: str) -> dict[str, Any]:
        """Полная карточка заказа (описание лота с ID: / #Quan:)."""
        oid = str(order_id or "").strip()
        if not oid:
            return {}
        try:
            data = await self._next_data_get(f"order/{oid}.json", f"{BASE_URL}/order/{oid}")
            props = data.get("pageProps") or {}
            order = props.get("order") or props.get("orderDetails") or {}
            if isinstance(order, dict) and order:
                return order
            return props if isinstance(props, dict) else {}
        except Exception as exc:
            logger.warning("fetch_order %s: %s", oid[:12], exc)
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

    async def fetch_user_lots(self, user_id: int) -> list[dict[str, Any]]:
        """Получает лоты пользователя с категориями для бампа."""
        data = await self._next_data_get(
            f"users/{user_id}.json?user_id={user_id}",
            f"{BASE_URL}/users/{user_id}",
        )
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
                desc = (offer.get("descriptions") or {}).get("rus") or {}
                title = desc.get("briefDescription") or desc.get("description") or ""
                lots.append({
                    "id": offer.get("id"),
                    "title": str(title).strip(),
                    "price": offer.get("price"),
                    "availability": offer.get("availability"),
                    "category_id": cat_id,
                    "game_id": game_id,
                    "category_url": f"{BASE_URL}/{game_slug}/{cat_slug}/trade" if game_slug and cat_slug else None,
                })
        return lots

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

    async def send_message(self, chat_id: str, content: str) -> dict[str, Any]:
        """Отправляет текстовое сообщение в чат Starvell."""
        resp = await self._request(
            "POST",
            f"{BASE_URL}/api/messages/send",
            referer=f"{BASE_URL}/chat/{chat_id}",
            json_body={"chatId": chat_id, "content": content},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        return resp.json()

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
