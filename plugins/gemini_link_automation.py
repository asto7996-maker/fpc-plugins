from __future__ import annotations

# === ОБЯЗАТЕЛЬНЫЕ ПОЛЯ FunPay Cardinal (НЕ УДАЛЯТЬ) ===
NAME = "Gemini Link Auto"
VERSION = "2.0.1"
DESCRIPTION = "Автовыдача Gemini 18m через Reseller API + очередь + автозакупка при рестоке"
CREDITS = "@xei1y"
UUID = "e8a3f1c2-9b4d-4d7e-a816-5f2c9b0e3d41"
SETTINGS_PAGE = True
BIND_TO_DELETE = None
# === КОНЕЦ ОБЯЗАТЕЛЬНЫХ ПОЛЕЙ ===

import html
import json
import logging
import os
import re
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from FunPayAPI.common.enums import OrderStatuses
from FunPayAPI.common.utils import RegularExpressions
from FunPayAPI.types import MessageTypes
from FunPayAPI.updater.events import (
    LastChatMessageChangedEvent,
    NewMessageEvent,
    NewOrderEvent,
    OrderStatusChangedEvent,
)
from cardinal import Cardinal
from tg_bot import CBT

logger = logging.getLogger("FPC.GeminiLink")
_P = "GeminiLink"

STORAGE_DIR = f"storage/plugins/{UUID}"
SETTINGS_FILE = f"{STORAGE_DIR}/settings.json"
PROCESSED_FILE = f"{STORAGE_DIR}/processed_orders.json"
WAIT_QUEUE_FILE = f"{STORAGE_DIR}/wait_queue.json"
INVENTORY_FILE = f"{STORAGE_DIR}/inventory.json"
RESTOCK_SUBS_FILE = f"{STORAGE_DIR}/restock_subscribers.json"
ATTENTION_FILE = f"{STORAGE_DIR}/manual_attention.json"
STATE_FILE = f"{STORAGE_DIR}/state.json"

_file_lock = threading.Lock()
_disabled_chats: Dict[str, float] = {}
_plugin: Optional["Plugin"] = None
_tg_bot_instance_id: Optional[int] = None
_restock_thread_started = False

DEFAULT_SETTINGS: Dict[str, Any] = {
    "bot_api_url": "https://worker-production-53ca.up.railway.app",
    "bot_api_key": "",
    "product_keywords": ["gemini", "18 мес", "gemini link", "gemini pro", "активация ссылкой"],
    "bot_product_id": "1",
    "product_search_keywords": ["gemini"],
    "order_process_delay_sec": 2,
    "order_process_retries": 3,
    "api_path_balance": "/api/me",
    "api_path_stock": "/api/products",
    "api_path_purchase": "/api/buy",
    "api_auth_header": "X-API-Key",
    "api_retry_count": 3,
    "api_retry_delay": 5,
    "wait_hours": 12,
    "restock_check_interval_sec": 300,
    "restock_info_hours": 12,
    "auto_buy_on_restock": True,
    "auto_buy_quantity": 5,
    "notify_seller": True,
    "processing_message": (
        "⏳ Заказ #{order_id} принят!\n"
        "Формируем ссылку Gemini 18 мес. — подождите 1–2 минуты..."
    ),
    "delivery_message": (
        "🎉 Ваша подписка Gemini готова!\n\n"
        "🔗 Ссылка для активации (18 месяцев):\n"
        "{link}\n\n"
        "📌 Активируйте сразу после получения!\n"
        "📋 Заказ: #{order_id}\n\n"
        "Спасибо за покупку! ⭐"
    ),
    "out_of_stock_message": (
        "⏳ Ссылки Gemini 18 мес. сейчас закончились.\n\n"
        "📋 Заказ: #{order_id}\n\n"
        "Выберите действие:\n"
        "1️⃣ Напишите «1» — ждать ссылку до {wait_hours} ч. (выдадим автоматически при рестоке)\n"
        "2️⃣ Напишите «2» — позвать продавца и обсудить возврат\n\n"
        "💡 Ресток поставщика — примерно каждые {restock_hours} ч."
    ),
    "wait_confirmed_message": (
        "✅ Вы в очереди ожидания!\n\n"
        "Мы выдадим ссылку автоматически, как только появится ресток "
        "(до {wait_hours} ч.).\n"
        "📋 Заказ: #{order_id}"
    ),
    "refund_requested_message": (
        "🆘 Продавец уведомлён о вашем запросе.\n\n"
        "Ожидайте ответа для обсуждения возврата средств.\n"
        "📋 Заказ: #{order_id}"
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Хранилище
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_storage() -> None:
    os.makedirs(STORAGE_DIR, exist_ok=True)


def _load_json(path: str, default: Any) -> Any:
    with _file_lock:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("%s: ошибка чтения %s — %s", _P, path, exc)
            return default


def _save_json(path: str, data: Any) -> None:
    with _file_lock:
        _ensure_storage()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def load_settings() -> Dict[str, Any]:
    data = _load_json(SETTINGS_FILE, {})
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    if "/api/v1/" in str(merged.get("api_path_balance", "")):
        for k in ("api_path_balance", "api_path_stock", "api_path_purchase", "api_auth_header"):
            merged[k] = DEFAULT_SETTINGS[k]
    if merged.get("product_keywords") == ["Gemini link"]:
        merged["product_keywords"] = list(DEFAULT_SETTINGS["product_keywords"])
    return merged


def save_settings(data: Dict[str, Any]) -> None:
    _save_json(SETTINGS_FILE, data)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_action(action: str, **meta: Any) -> None:
    logger.info("%s [%s] %s %s", _P, _ts(), action, meta or "")


def _mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return "—"
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def _is_configured(settings: Optional[Dict[str, Any]] = None) -> bool:
    s = settings or load_settings()
    return bool(s.get("bot_api_url", "").strip() and s.get("bot_api_key", "").strip())


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def _extract_order_text(order: Any) -> str:
    parts: List[str] = []
    for attr in (
        "full_description", "short_description", "title", "description",
        "lot_name", "name", "html", "subcategory_name", "category_name",
    ):
        val = getattr(order, attr, None)
        if val:
            parts.append(str(val))
    subcategory = getattr(order, "subcategory", None)
    if subcategory:
        parts.append(str(getattr(subcategory, "name", "") or ""))
    if hasattr(order, "lot_params") and order.lot_params:
        parts.append(str(order.lot_params))
    return _normalize_text("\n".join(parts))


def _extract_order_id(text: str) -> Optional[str]:
    matches = RegularExpressions().ORDER_ID.findall(text or "")
    if not matches:
        return None
    return matches[0].lstrip("#").upper()


def _order_quantity(order: Any) -> int:
    amount = getattr(order, "amount", None)
    if amount:
        try:
            return max(1, int(amount))
        except (TypeError, ValueError):
            pass
    for source in (
        getattr(order, "full_description", None),
        getattr(order, "description", None),
        getattr(order, "short_description", None),
    ):
        if not source:
            continue
        found = RegularExpressions().PRODUCTS_AMOUNT.findall(str(source))
        if found:
            try:
                return max(1, int(found[0].split()[0]))
            except (TypeError, ValueError, IndexError):
                pass
    return 1


def _resolve_chat_id(cardinal: Cardinal, buyer: str, hint: Any = None) -> Any:
    if hint:
        return hint
    try:
        chat = cardinal.account.get_chat_by_name(buyer)
        if chat:
            return chat.id
    except Exception as exc:
        logger.debug("%s: get_chat_by_name(%s): %s", _P, buyer, exc)
    return None


def _matches_keywords(text: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    s = settings or load_settings()
    normalized = _normalize_text(text)
    keywords = s.get("product_keywords") or DEFAULT_SETTINGS["product_keywords"]
    return any(_normalize_text(kw) in normalized for kw in keywords)


def send_fp(c: Cardinal, chat_id: Any, text: str) -> None:
    if not chat_id:
        logger.warning("%s: send_fp без chat_id", _P)
        return
    c.send_message(chat_id, text)


def _format_template(key: str, **kwargs: Any) -> str:
    settings = load_settings()
    template = settings.get(key, DEFAULT_SETTINGS.get(key, ""))
    defaults = {
        "wait_hours": settings.get("wait_hours", 12),
        "restock_hours": settings.get("restock_info_hours", 12),
    }
    defaults.update(kwargs)
    try:
        return template.format(**defaults)
    except (KeyError, ValueError):
        result = str(template)
        for k, v in defaults.items():
            result = result.replace("{" + k + "}", str(v))
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Очередь, инвентарь, подписчики
# ─────────────────────────────────────────────────────────────────────────────

class OrderQueue:
    @staticmethod
    def load_wait() -> List[Dict[str, Any]]:
        return _load_json(WAIT_QUEUE_FILE, [])

    @staticmethod
    def save_wait(items: List[Dict[str, Any]]) -> None:
        _save_json(WAIT_QUEUE_FILE, items)

    @staticmethod
    def load_inventory() -> List[Dict[str, Any]]:
        return _load_json(INVENTORY_FILE, [])

    @staticmethod
    def save_inventory(items: List[Dict[str, Any]]) -> None:
        _save_json(INVENTORY_FILE, items)

    @staticmethod
    def pop_link() -> Optional[str]:
        items = OrderQueue.load_inventory()
        if not items:
            return None
        item = items.pop(0)
        OrderQueue.save_inventory(items)
        return item.get("link")

    @staticmethod
    def push_links(links: List[str], product_id: int = 0) -> None:
        items = OrderQueue.load_inventory()
        for link in links:
            items.append({"link": link, "at": _ts(), "product_id": product_id})
        OrderQueue.save_inventory(items)

    @staticmethod
    def add_waiting(order_id: str, chat_id: Any, buyer: str, quantity: int = 1) -> None:
        items = OrderQueue.load_wait()
        if any(x.get("order_id") == order_id for x in items):
            return
        settings = load_settings()
        wait_h = int(settings.get("wait_hours", 12))
        items.append({
            "order_id": order_id,
            "chat_id": chat_id,
            "buyer": buyer,
            "quantity": quantity,
            "status": "awaiting_choice",
            "created_at": _ts(),
            "wait_until_ts": time.time() + wait_h * 3600,
        })
        OrderQueue.save_wait(items)

    @staticmethod
    def find_by_chat(chat_id: Any, statuses: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        cid = str(chat_id)
        for item in reversed(OrderQueue.load_wait()):
            if str(item.get("chat_id")) != cid:
                continue
            if statuses and item.get("status") not in statuses:
                continue
            return item
        return None

    @staticmethod
    def update_entry(order_id: str, **fields: Any) -> None:
        items = OrderQueue.load_wait()
        for item in items:
            if item.get("order_id") == order_id:
                item.update(fields)
                break
        OrderQueue.save_wait(items)

    @staticmethod
    def pending_delivery() -> List[Dict[str, Any]]:
        now = time.time()
        result = []
        for item in OrderQueue.load_wait():
            if item.get("status") not in ("waiting_link", "preorder_paid"):
                continue
            if item.get("wait_until_ts", now + 1) < now:
                item["status"] = "expired"
                continue
            result.append(item)
        OrderQueue.save_wait(OrderQueue.load_wait())
        return sorted(result, key=lambda x: x.get("created_at", ""))

    @staticmethod
    def remove(order_id: str) -> None:
        items = [x for x in OrderQueue.load_wait() if x.get("order_id") != order_id]
        OrderQueue.save_wait(items)

    @staticmethod
    def subscribe_restock(chat_id: Any) -> None:
        subs = _load_json(RESTOCK_SUBS_FILE, [])
        cid = str(chat_id)
        if cid not in subs:
            subs.append(cid)
            _save_json(RESTOCK_SUBS_FILE, subs)

    @staticmethod
    def pop_restock_subscribers() -> List[str]:
        subs = _load_json(RESTOCK_SUBS_FILE, [])
        _save_json(RESTOCK_SUBS_FILE, [])
        return subs

    @staticmethod
    def load_state() -> Dict[str, Any]:
        return _load_json(STATE_FILE, {"last_stock": 0, "last_restock_at": 0})

    @staticmethod
    def save_state(state: Dict[str, Any]) -> None:
        _save_json(STATE_FILE, state)


def load_processed() -> List[str]:
    return _load_json(PROCESSED_FILE, [])


def save_processed(items: List[str]) -> None:
    _save_json(PROCESSED_FILE, items)


def load_attention() -> List[Dict[str, Any]]:
    return _load_json(ATTENTION_FILE, [])


def save_attention(items: List[Dict[str, Any]]) -> None:
    _save_json(ATTENTION_FILE, items)


# ─────────────────────────────────────────────────────────────────────────────
# Reseller API
# ─────────────────────────────────────────────────────────────────────────────

class SupplierBotAPI:
    HEADERS = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"FunPayCardinal/{VERSION}",
    }

    @classmethod
    def _settings(cls) -> Dict[str, Any]:
        return load_settings()

    @classmethod
    def _base_url(cls) -> str:
        return cls._settings()["bot_api_url"].rstrip("/")

    @classmethod
    def _auth_headers(cls) -> Dict[str, str]:
        s = cls._settings()
        headers = dict(cls.HEADERS)
        key = s.get("bot_api_key", "").strip()
        if key:
            headers[s.get("api_auth_header", "X-API-Key")] = key
        return headers

    @classmethod
    def _retry_params(cls) -> Tuple[int, int]:
        s = cls._settings()
        return int(s.get("api_retry_count", 3)), int(s.get("api_retry_delay", 5))

    @classmethod
    def _check_ok(cls, data: Dict[str, Any]) -> None:
        if data.get("ok") is False:
            raise RuntimeError(data.get("error") or data.get("message") or "API ok=false")

    @classmethod
    def _request(cls, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        retries, delay = cls._retry_params()
        url = f"{cls._base_url()}{path}"
        last_error = "unknown"
        for attempt in range(1, retries + 1):
            try:
                resp = requests.request(method, url, headers=cls._auth_headers(), json=json_body, timeout=30)
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                data = resp.json() if resp.content else {}
                if not resp.ok:
                    raise requests.HTTPError(str(data.get("error") or data.get("message") or resp.status_code))
                if isinstance(data, dict):
                    cls._check_ok(data)
                return data if isinstance(data, dict) else {"data": data}
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = str(exc)
                logger.warning("%s: API %s/%s — %s", _P, attempt, retries, exc)
                if attempt < retries:
                    time.sleep(delay)
        raise RuntimeError(f"API недоступен: {last_error}")

    @classmethod
    def _product_names(cls, product: Dict[str, Any]) -> str:
        return _normalize_text(" ".join(str(product.get(k) or "") for k in ("name_en", "name_ru", "name")))

    @classmethod
    def _resolve_product(cls, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        s = cls._settings()
        pid = s.get("bot_product_id")
        if pid not in (None, "", 0, "0"):
            try:
                tid = int(pid)
                for p in products:
                    if int(p.get("id", -1)) == tid:
                        return p
            except ValueError:
                pass
        keywords = s.get("product_search_keywords") or ["gemini", "18m"]
        matched = [p for p in products if all(_normalize_text(k) in cls._product_names(p) for k in keywords)]
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            # предпочитаем товар с 18m в названии
            for p in matched:
                if "18m" in cls._product_names(p):
                    return p
            return matched[0]
        raise RuntimeError("Gemini 18m не найден в /api/products — укажите Product ID")

    @classmethod
    def get_products(cls) -> List[Dict[str, Any]]:
        data = cls._request("GET", cls._settings()["api_path_stock"])
        products = data.get("products") or []
        return products if isinstance(products, list) else []

    @classmethod
    def _extract_links(cls, data: Dict[str, Any]) -> List[str]:
        links: List[str] = []

        def add_link(value: Any) -> None:
            if not isinstance(value, str):
                return
            link = value.strip()
            if link and link not in links:
                links.append(link)

        items = data.get("items") or data.get("links") or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str):
                    add_link(item)
                elif isinstance(item, dict):
                    for key in ("link", "url", "value", "item", "activation_link", "text", "content", "data"):
                        add_link(item.get(key))

        for key in ("activation_link", "link", "url", "item"):
            add_link(data.get(key))

        nested = data.get("data") or data.get("result") or data.get("purchase")
        if isinstance(nested, dict) and not links:
            links.extend(cls._extract_links(nested))

        return [link for link in links if link.startswith("http")]

    @classmethod
    def get_balance(cls) -> Dict[str, Any]:
        data = cls._request("GET", cls._settings()["api_path_balance"])
        user = data.get("user") or data
        balance = float(user.get("balance", data.get("balance", 0)))
        return {"balance": balance, "currency": "USD"}

    @classmethod
    def get_stock(cls) -> Dict[str, Any]:
        products = cls.get_products()
        product = cls._resolve_product(products)
        available = int(product.get("stock_count", product.get("stock", 0)))
        return {
            "available": available,
            "price": float(product.get("price", 0)),
            "status": "out_of_stock" if available <= 0 else "ok",
            "product_id": int(product.get("id", 0)),
            "product_name": product.get("name_en") or product.get("name") or "?",
        }

    @classmethod
    def purchase(cls, quantity: int = 1) -> Dict[str, Any]:
        products = cls.get_products()
        product = cls._resolve_product(products)
        product_id = int(product.get("id", 0))
        stock = int(product.get("stock_count", product.get("stock", 0)))
        qty = max(1, min(quantity, stock))
        if stock <= 0:
            return {"items": [], "status": "out_of_stock", "product_id": product_id}
        data = cls._request("POST", cls._settings()["api_path_purchase"], {"product_id": product_id, "quantity": qty})
        links = cls._extract_links(data)
        if not links:
            return {"items": [], "status": "out_of_stock", "product_id": product_id}
        return {
            "items": links,
            "status": "ok",
            "product_id": product_id,
            "transaction_id": data.get("transaction_id"),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Основной класс
# ─────────────────────────────────────────────────────────────────────────────

class Plugin:
    def __init__(self, cardinal: Cardinal) -> None:
        self.cardinal = cardinal
        self._lock = threading.Lock()
        _ensure_storage()
        if not os.path.exists(SETTINGS_FILE):
            save_settings(dict(DEFAULT_SETTINGS))

    def notify_seller(self, text: str) -> None:
        settings = load_settings()
        if not settings.get("notify_seller") or not self.cardinal.telegram:
            logger.warning("%s: %s", _P, text)
            return
        try:
            chat_id = self.cardinal.telegram.authorized_users[0]
            self.cardinal.telegram.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as exc:
            logger.error("%s: notify_seller: %s", _P, exc)

    def _mark_processed(self, order_id: str) -> None:
        items = load_processed()
        if order_id not in items:
            items.append(order_id)
            save_processed(items)

    def _deliver(self, order_id: str, chat_id: Any, link: str, buyer: str = "") -> None:
        if chat_id:
            send_fp(self.cardinal, chat_id, _format_template("delivery_message", order_id=order_id, link=link))
        self._mark_processed(order_id)
        OrderQueue.remove(order_id)
        self.notify_seller(f"✅ <b>{NAME}</b>\n\nЗаказ <code>#{order_id}</code> выдан.\n👤 {html.escape(buyer or '—')}")

    def _try_fulfill_with_link(self, entry: Dict[str, Any], link: str) -> None:
        self._deliver(entry["order_id"], entry.get("chat_id"), link, entry.get("buyer", ""))

    def _buy_and_store(self, quantity: int) -> List[str]:
        result = SupplierBotAPI.purchase(quantity)
        if result["status"] == "out_of_stock":
            return []
        links = result.get("items") or []
        if links:
            OrderQueue.push_links(links, result.get("product_id", 0))
            _log_action("Куплено в инвентарь", qty=len(links))
        return links

    def fulfill_queue(self) -> None:
        if not _is_configured():
            return
        with self._lock:
            try:
                stock = SupplierBotAPI.get_stock()
            except Exception as exc:
                logger.debug("%s: fulfill_queue stock: %s", _P, exc)
                return

            settings = load_settings()
            state = OrderQueue.load_state()
            prev_stock = int(state.get("last_stock", 0))
            cur_stock = stock["available"]
            state["last_stock"] = cur_stock

            # ресток обнаружен
            if cur_stock > 0 and prev_stock == 0:
                state["last_restock_at"] = time.time()
                _log_action("Ресток обнаружен", stock=cur_stock)
                if settings.get("auto_buy_on_restock"):
                    buy_qty = min(int(settings.get("auto_buy_quantity", 5)), cur_stock)
                    self._buy_and_store(buy_qty)
                # уведомить подписчиков
                for cid in OrderQueue.pop_restock_subscribers():
                    try:
                        send_fp(self.cardinal, cid,
                                f"🔔 Ресток Gemini 18m!\nВ наличии: {cur_stock} шт.\nМожете оформить заказ.")
                    except Exception:
                        pass
                self.notify_seller(f"🔔 <b>{NAME}</b> Ресток! В наличии: {cur_stock} шт.")

            OrderQueue.save_state(state)

            # выдача очереди
            for entry in OrderQueue.pending_delivery():
                order_id = entry["order_id"]
                qty = int(entry.get("quantity", 1))
                link = OrderQueue.pop_link()
                if not link:
                    try:
                        purchase = SupplierBotAPI.purchase(qty)
                        if purchase["status"] == "out_of_stock":
                            continue
                        links = purchase.get("items") or []
                        link = links[0] if links else None
                        extra = links[1:] if len(links) > 1 else []
                        if extra:
                            OrderQueue.push_links(extra)
                    except Exception as exc:
                        logger.error("%s: fulfill #%s: %s", _P, order_id, exc)
                        continue
                if link:
                    self._try_fulfill_with_link(entry, link)

            # просроченные
            now = time.time()
            for item in OrderQueue.load_wait():
                if item.get("status") in ("waiting_link", "preorder_paid", "awaiting_choice"):
                    if item.get("wait_until_ts", now + 1) < now and item.get("status") != "expired":
                        item["status"] = "expired"
                        self.notify_seller(
                            f"⏰ <b>{NAME}</b> Заказ <code>#{item.get('order_id')}</code> — "
                            f"истекло время ожидания ({settings.get('wait_hours')}ч)"
                        )
            OrderQueue.save_wait(OrderQueue.load_wait())

    def schedule_process(
        self,
        order_id: str,
        force: bool = False,
        chat_id_hint: Any = None,
        delay: Optional[float] = None,
        retry: int = 0,
    ) -> None:
        settings = load_settings()
        wait = delay if delay is not None else float(settings.get("order_process_delay_sec", 2))

        def worker() -> None:
            if wait > 0:
                time.sleep(wait)
            self.process_order(order_id, force=force, chat_id_hint=chat_id_hint, retry=retry)

        threading.Thread(target=worker, daemon=True, name=f"GeminiLink-{order_id}").start()

    def process_order(
        self,
        order_id: str,
        force: bool = False,
        chat_id_hint: Any = None,
        retry: int = 0,
    ) -> None:
        order_id = order_id.strip().lstrip("#").upper()
        if not force and order_id in load_processed():
            return
        if not _is_configured():
            self.notify_seller(f"⚠️ #{order_id} — API не настроен")
            return

        try:
            full_order = self.cardinal.account.get_order(order_id)
        except Exception as exc:
            logger.error("%s: get_order #%s: %s", _P, order_id, exc)
            settings = load_settings()
            max_retries = int(settings.get("order_process_retries", 3))
            if retry < max_retries:
                logger.error("%s: get_order #%s — повтор %s/%s", _P, order_id, retry + 1, max_retries)
                self.schedule_process(order_id, force=force, chat_id_hint=chat_id_hint, delay=3, retry=retry + 1)
            return

        if not force and getattr(full_order, "status", None) == OrderStatuses.REFUNDED:
            return

        order_text = _extract_order_text(full_order)
        if not force and not _matches_keywords(order_text):
            settings = load_settings()
            max_retries = int(settings.get("order_process_retries", 3))
            if retry < max_retries:
                logger.info(
                    "%s: #%s keywords не совпали, повтор %s/%s (%.120s…)",
                    _P, order_id, retry + 1, max_retries, order_text[:120],
                )
                self.schedule_process(order_id, chat_id_hint=chat_id_hint, delay=3, retry=retry + 1)
                return
            logger.info(
                "%s: #%s пропущен — keywords %s (текст: %.120s…)",
                _P, order_id, settings.get("product_keywords"), order_text[:120],
            )
            return

        buyer = full_order.buyer_username
        chat_id = _resolve_chat_id(self.cardinal, buyer, chat_id_hint)
        quantity = _order_quantity(full_order)

        _log_action("Новый заказ", order_id=order_id, buyer=buyer, qty=quantity, chat_id=chat_id)
        if chat_id:
            send_fp(self.cardinal, chat_id, _format_template("processing_message", order_id=order_id))

        # 1) инвентарь
        links_from_inv: List[str] = []
        for _ in range(quantity):
            link = OrderQueue.pop_link()
            if link:
                links_from_inv.append(link)
            else:
                break
        if len(links_from_inv) == quantity:
            self._deliver(order_id, chat_id, "\n".join(links_from_inv), buyer)
            return
        for link in links_from_inv:
            OrderQueue.push_links([link])

        # 2) покупка в API
        try:
            stock = SupplierBotAPI.get_stock()
            if stock["available"] <= 0:
                self._enqueue_out_of_stock(order_id, chat_id, buyer, quantity)
                return
            balance = SupplierBotAPI.get_balance()
            if balance["balance"] < stock["price"] * quantity:
                self._notify_attention(order_id, chat_id, "insufficient_balance")
                if chat_id:
                    send_fp(
                        self.cardinal, chat_id,
                        f"⏳ Заказ #{order_id} принят.\n"
                        "Сейчас пополняем баланс поставщика — выдадим ссылку в ближайшее время.",
                    )
                return
            purchase = SupplierBotAPI.purchase(quantity)
            if purchase["status"] == "out_of_stock":
                self._enqueue_out_of_stock(order_id, chat_id, buyer, quantity)
                return
            items = purchase.get("items") or []
            if items:
                self._deliver(order_id, chat_id, "\n".join(items), buyer)
            else:
                self._enqueue_out_of_stock(order_id, chat_id, buyer, quantity)
        except Exception as exc:
            logger.error("%s: process_order #%s: %s", _P, order_id, exc)
            self._notify_attention(order_id, chat_id, "api_failure")
            if chat_id:
                send_fp(self.cardinal, chat_id, _format_template("out_of_stock_message", order_id=order_id))

    def _enqueue_out_of_stock(self, order_id: str, chat_id: Any, buyer: str, quantity: int) -> None:
        OrderQueue.add_waiting(order_id, chat_id, buyer, quantity)
        if chat_id:
            send_fp(self.cardinal, chat_id, _format_template("out_of_stock_message", order_id=order_id))

    def _notify_attention(self, order_id: str, chat_id: Any, reason: str) -> None:
        items = load_attention()
        if not any(x.get("order_id") == order_id for x in items):
            items.append({"order_id": order_id, "chat_id": chat_id, "reason": reason, "at": _ts()})
            save_attention(items)
        self.notify_seller(f"⚠️ <b>{NAME}</b> #{order_id} — {html.escape(reason)}")

    # ── Сообщения покупателя ─────────────────────────────────────────────────

    def handle_buyer_message(self, text: str, chat_id: Any) -> bool:
        """Возвращает True если сообщение обработано."""
        if str(chat_id) in _disabled_chats and time.time() < _disabled_chats[str(chat_id)]:
            return False

        raw = (text or "").strip()
        lower = _normalize_text(raw)

        # Команда 1 — ждать ссылку
        if lower in ("1", "+", "1️⃣", "ждать", "жду", "подождать", "wait", "ожидать"):
            entry = OrderQueue.find_by_chat(chat_id, ["awaiting_choice", "waiting_link"])
            if entry:
                settings = load_settings()
                wait_h = int(settings.get("wait_hours", 12))
                OrderQueue.update_entry(
                    entry["order_id"],
                    status="waiting_link",
                    wait_until_ts=time.time() + wait_h * 3600,
                )
                send_fp(self.cardinal, chat_id, _format_template(
                    "wait_confirmed_message", order_id=entry["order_id"]))
                threading.Thread(target=self.fulfill_queue, daemon=True).start()
                return True

        # Команда 2 — возврат / продавец
        if lower in ("2", "-", "2️⃣", "возврат", "refund", "продавец", "help"):
            entry = OrderQueue.find_by_chat(chat_id, ["awaiting_choice", "waiting_link", "preorder_paid"])
            if entry:
                OrderQueue.update_entry(entry["order_id"], status="refund_requested")
                _disabled_chats[str(chat_id)] = time.time() + 3600
                send_fp(self.cardinal, chat_id, _format_template(
                    "refund_requested_message", order_id=entry["order_id"]))
                self.notify_seller(
                    f"🆘 <b>{NAME}</b> Возврат/помощь\n"
                    f"Заказ <code>#{entry['order_id']}</code>\n"
                    f"💬 chat: <code>{chat_id}</code>")
                return True

        if re.match(r"^/gemini\b", raw, re.I):
            self.handle_gemini_command(raw, chat_id)
            return True

        return False

    def handle_gemini_command(self, text: str, chat_id: Any) -> None:
        parts = text.strip().split()
        sub = parts[1].lower() if len(parts) > 1 else ""

        if not sub:
            send_fp(self.cardinal, chat_id, self._main_menu())
            return

        if sub in ("check", "stock", "наличие"):
            self._cmd_check(chat_id)
        elif sub in ("restock", "ресток", "when"):
            self._cmd_restock_info(chat_id)
        elif sub in ("preorder", "предзаказ"):
            self._cmd_preorder(chat_id)
        elif sub in ("help", "помощь"):
            self._cmd_help(chat_id)
        else:
            send_fp(self.cardinal, chat_id, self._main_menu())

    def _main_menu(self) -> str:
        s = load_settings()
        return (
            "🌟 Gemini 18m — Меню\n\n"
            "📦 /Gemini check — Наличие\n"
            "🕐 /Gemini restock — Когда ресток\n"
            "📝 /Gemini preorder — Предзаказ\n"
            "🆘 /Gemini help — Позвать продавца\n\n"
            f"💡 Ресток поставщика ~ каждые {s.get('restock_info_hours', 12)} ч.\n"
            "После оплаты ссылка выдаётся автоматически."
        )

    def _cmd_check(self, chat_id: Any) -> None:
        try:
            stock = SupplierBotAPI.get_stock()
            inv = len(OrderQueue.load_inventory())
            msg = (
                f"📦 {stock['product_name']}\n\n"
                f"🛒 У поставщика: {stock['available']} шт.\n"
                f"📦 Наш резерв: {inv} шт.\n"
                f"💵 Цена: ${stock['price']:.2f}\n\n"
                + ("✅ Можно покупать!" if stock["available"] > 0 or inv > 0
                   else f"⏳ Нет в наличии. Ресток ~ каждые {load_settings().get('restock_info_hours', 12)} ч.\n"
                        "Напишите /Gemini restock для уведомления")
            )
            send_fp(self.cardinal, chat_id, msg)
        except Exception as exc:
            send_fp(self.cardinal, chat_id, f"⚠️ Ошибка: {exc}")

    def _cmd_restock_info(self, chat_id: Any) -> None:
        s = load_settings()
        state = OrderQueue.load_state()
        last = state.get("last_restock_at", 0)
        if last:
            ago = int((time.time() - last) / 3600)
            eta = max(0, int(s.get("restock_info_hours", 12)) - ago)
            info = f"Последний ресток: {ago} ч. назад.\nОжидаем следующий через ~{eta} ч."
        else:
            info = f"Ресток поставщика примерно каждые {s.get('restock_info_hours', 12)} ч."
        OrderQueue.subscribe_restock(chat_id)
        send_fp(
            self.cardinal, chat_id,
            f"🕐 Информация о рестоке\n\n{info}\n\n"
            "🔔 Вы подписаны на уведомление о рестоке!\n"
            "📝 /Gemini preorder — инструкция по предзаказу",
        )

    def _cmd_preorder(self, chat_id: Any) -> None:
        OrderQueue.subscribe_restock(chat_id)
        send_fp(
            self.cardinal, chat_id,
            "📝 Предзаказ Gemini 18m\n\n"
            "1. Оформите и оплатите заказ на FunPay\n"
            "2. Если ссылка есть — выдадим сразу\n"
            "3. Если нет — напишите «1» и ждите до 12 ч.\n"
            "4. При рестоке — автоматическая выдача\n\n"
            "🕐 /Gemini restock — уведомление о рестоке",
        )

    def _cmd_help(self, chat_id: Any) -> None:
        _disabled_chats[str(chat_id)] = time.time() + 1800
        self.notify_seller(f"🆘 <b>{NAME}</b> Помощь в чате <code>{chat_id}</code>")
        send_fp(self.cardinal, chat_id, "🆘 Продавец уведомлён. Ожидайте ответа.")


# ─────────────────────────────────────────────────────────────────────────────
# Фоновый монитор рестока
# ─────────────────────────────────────────────────────────────────────────────

def _restock_loop() -> None:
    while True:
        try:
            if _plugin and _is_configured():
                _plugin.fulfill_queue()
        except Exception as exc:
            logger.error("%s: restock_loop: %s", _P, exc)
        interval = int(load_settings().get("restock_check_interval_sec", 300))
        time.sleep(max(60, interval))


def start_restock_monitor() -> None:
    global _restock_thread_started
    if _restock_thread_started:
        return
    _restock_thread_started = True
    threading.Thread(target=_restock_loop, daemon=True, name="GeminiLinkRestock").start()
    logger.info("%s: монитор рестока запущен", _P)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram UI (продавец) — компактная версия
# ─────────────────────────────────────────────────────────────────────────────

def _settings_summary() -> str:
    s = load_settings()
    inv = len(OrderQueue.load_inventory())
    wait = len(OrderQueue.load_wait())
    return (
        f"⚙️ <b>{NAME}</b> v{VERSION}\n\n"
        f"API: {'🟢' if _is_configured(s) else '🔴'}\n"
        f"🌐 <code>{html.escape(s.get('bot_api_url', '—'))}</code>\n"
        f"🔑 <code>{html.escape(_mask_key(s.get('bot_api_key', '')))}</code>\n"
        f"🆔 Product ID: <code>{html.escape(str(s.get('bot_product_id') or 'auto'))}</code>\n"
        f"📦 Резерв: {inv} | Очередь: {wait}\n"
        f"🔄 Автозакупка: {s.get('auto_buy_quantity', 5)} шт. при рестоке\n\n"
        f"/gemini_link /gl_balance /gl_stock /gl_process ID"
    )


def _main_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🌐 URL", callback_data="gla:set_url"),
        InlineKeyboardButton("🔑 Key", callback_data="gla:set_key"),
    )
    kb.add(
        InlineKeyboardButton("🆔 Product ID", callback_data="gla:set_product"),
        InlineKeyboardButton("📃 Товары", callback_data="gla:products"),
    )
    kb.add(
        InlineKeyboardButton("🔢 Автозакупка", callback_data="gla:set_autobuy"),
        InlineKeyboardButton("💰 Баланс", callback_data="gla:balance"),
    )
    kb.add(
        InlineKeyboardButton("📦 Наличие", callback_data="gla:stock"),
        InlineKeyboardButton("📋 Очередь", callback_data="gla:queue"),
    )
    kb.add(InlineKeyboardButton("🔄 Обновить", callback_data="gla:main"))
    return kb


def _queue_summary() -> str:
    wait = OrderQueue.load_wait()
    inv = OrderQueue.load_inventory()
    lines = [f"📋 <b>Очередь</b> ({len(wait)}) | Резерв: {len(inv)}\n"]
    for item in wait[-8:]:
        lines.append(
            f"• #{item.get('order_id')} — {item.get('status')} — {item.get('buyer', '?')}"
        )
    return "\n".join(lines) if len(lines) > 1 else lines[0] + "\nПусто"


def _edit_panel(bot, chat_id: int, msg_id: Optional[int] = None) -> None:
    text, markup = _settings_summary(), _main_keyboard()
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


def setup_telegram(cardinal: Cardinal) -> None:
    global _tg_bot_instance_id
    if not cardinal.telegram:
        return
    tg, bot = cardinal.telegram, cardinal.telegram.bot
    if _tg_bot_instance_id == id(bot):
        return
    _tg_bot_instance_id = id(bot)

    def _answer(call, text: str = "", alert: bool = False) -> None:
        try:
            bot.answer_callback_query(call.id, text[:200] if text else None, show_alert=alert)
        except Exception:
            pass

    def on_callback(call):
        action = (call.data or "").split(":", 1)[-1]
        cid, mid = call.message.chat.id, call.message.message_id
        try:
            if action == "main":
                _edit_panel(bot, cid, mid)
                _answer(call)
            elif action == "set_url":
                _answer(call)
                r = bot.send_message(cid, "🌐 BOT_API_URL:\n/cancel")
                tg.set_state(cid, r.message_id, call.from_user.id, state="gla_url")
            elif action == "set_key":
                _answer(call)
                r = bot.send_message(cid, "🔑 BOT_API_KEY:\n/cancel")
                tg.set_state(cid, r.message_id, call.from_user.id, state="gla_key")
            elif action == "set_product":
                _answer(call)
                r = bot.send_message(cid, "🆔 Product ID (1 = Gemini 18m) или auto:\n/cancel")
                tg.set_state(cid, r.message_id, call.from_user.id, state="gla_product")
            elif action == "set_autobuy":
                _answer(call)
                r = bot.send_message(cid, "🔢 Кол-во автозакупки при рестоке (число):\n/cancel")
                tg.set_state(cid, r.message_id, call.from_user.id, state="gla_autobuy")
            elif action == "products":
                if not _is_configured():
                    _answer(call, "Настройте API", alert=True)
                    return
                lines = ["📃 Товары:\n"]
                for p in SupplierBotAPI.get_products()[:15]:
                    lines.append(f"#{p.get('id')} {p.get('name_en')} — ${p.get('price')} × {p.get('stock_count')}")
                bot.send_message(cid, "\n".join(lines), parse_mode="HTML")
                _answer(call)
            elif action == "balance":
                bal = SupplierBotAPI.get_balance()
                _answer(call, f"💰 {bal['balance']:.2f} USD", alert=True)
            elif action == "stock":
                st = SupplierBotAPI.get_stock()
                inv = len(OrderQueue.load_inventory())
                _answer(call, f"📦 API:{st['available']} + резерв:{inv}", alert=True)
            elif action == "queue":
                bot.edit_message_text(_queue_summary(), cid, mid, parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup().add(
                                          InlineKeyboardButton("◀️", callback_data="gla:main")))
                _answer(call)
            else:
                _answer(call)
        except Exception as exc:
            _answer(call, str(exc)[:200], alert=True)

    def on_text(msg):
        st_data = tg.get_state(msg.chat.id, msg.from_user.id)
        if not st_data or "state" not in st_data:
            return
        state = st_data["state"]
        text = (msg.text or "").strip()
        if text.lower() in ("/cancel", "отмена"):
            tg.clear_state(msg.chat.id, msg.from_user.id, True)
            bot.reply_to(msg, "❌ Отменено")
            return
        settings = load_settings()
        if state == "gla_url":
            settings["bot_api_url"] = text.rstrip("/")
        elif state == "gla_key":
            settings["bot_api_key"] = text
            try:
                bot.delete_message(msg.chat.id, msg.message_id)
            except Exception:
                pass
        elif state == "gla_product":
            settings["bot_product_id"] = "" if text.lower() in ("auto", "авто") else str(int(text))
        elif state == "gla_autobuy":
            settings["auto_buy_quantity"] = max(1, int(text))
        else:
            return
        save_settings(settings)
        tg.clear_state(msg.chat.id, msg.from_user.id, True)
        bot.reply_to(msg, "✅ Сохранено")
        _edit_panel(bot, msg.chat.id)

    def cmd_process(msg):
        parts = (msg.text or "").split()
        if len(parts) < 2:
            bot.reply_to(msg, "/gl_process ORDER_ID")
            return
        oid = parts[1].strip().lstrip("#").upper()
        bot.reply_to(msg, f"⏳ #{oid}...")
        threading.Thread(target=_plugin.process_order, args=(oid, True), daemon=True).start()

    tg.msg_handler(lambda m: _edit_panel(bot, m.chat.id), func=lambda m: m.text and m.text.split()[0].lower() in ("/gemini_link", "/gemini"))
    def cmd_balance(msg):
        if not _is_configured():
            bot.reply_to(msg, "⚠️ API не настроен")
            return
        try:
            bal = SupplierBotAPI.get_balance()
            bot.reply_to(msg, f"💰 {bal['balance']:.2f} USD")
        except Exception as exc:
            bot.reply_to(msg, f"⚠️ {exc}")

    def cmd_stock(msg):
        if not _is_configured():
            bot.reply_to(msg, "⚠️ API не настроен")
            return
        try:
            st = SupplierBotAPI.get_stock()
            inv = len(OrderQueue.load_inventory())
            wait = len(OrderQueue.load_wait())
            bot.reply_to(
                msg,
                f"📦 {st['product_name']}\n"
                f"API: {st['available']} | Резерв: {inv} | Очередь: {wait}\n"
                f"💵 ${st['price']:.2f}",
            )
        except Exception as exc:
            bot.reply_to(msg, f"⚠️ {exc}")

    tg.msg_handler(cmd_balance, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_balance")
    tg.msg_handler(cmd_stock, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_stock")
    tg.msg_handler(cmd_process, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_process")
    tg.cbq_handler(on_callback, func=lambda c: (c.data or "").startswith("gla:"))
    tg.cbq_handler(lambda c: (_edit_panel(bot, c.message.chat.id, c.message.message_id), _answer(c)),
                   func=lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))
    def _in_gla_state(msg) -> bool:
        state = (tg.get_state(msg.chat.id, msg.from_user.id) or {}).get("state", "")
        return state in ("gla_url", "gla_key", "gla_product", "gla_autobuy")

    tg.msg_handler(on_text, func=_in_gla_state)
    try:
        cardinal.add_telegram_commands(UUID, [
            ("gemini_link", "панель Gemini", True),
            ("gl_process", "выдать заказ", True),
            ("gl_stock", "наличие Gemini", True),
            ("gl_balance", "баланс API", True),
        ])
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# События FunPay
# ─────────────────────────────────────────────────────────────────────────────

def init_plugin(cardinal: Cardinal, *_args) -> None:
    global _plugin
    _plugin = Plugin(cardinal)
    setup_telegram(cardinal)
    start_restock_monitor()
    logger.info("%s v%s загружен", _P, VERSION)


def on_new_order(cardinal: Cardinal, event: NewOrderEvent) -> None:
    if not _plugin:
        return
    order = event.order
    hint = _extract_order_text(order)
    if hint and not _matches_keywords(hint):
        return
    _plugin.schedule_process(str(order.id))


def on_order_status_changed(cardinal: Cardinal, event: OrderStatusChangedEvent) -> None:
    if not _plugin:
        return
    if event.order.status != OrderStatuses.PAID:
        return
    _plugin.schedule_process(str(event.order.id))


def _handle_order_paid(cardinal: Cardinal, text: str, chat_id: Any) -> None:
    order_id = _extract_order_id(text)
    if order_id and _plugin:
        _log_action("Оплата заказа", order_id=order_id, chat_id=chat_id)
        _plugin.schedule_process(order_id, chat_id_hint=chat_id)


def _handle_fp_message(cardinal: Cardinal, text: str, chat_id: Any, msg_type: Any = None) -> None:
    if msg_type == MessageTypes.ORDER_PURCHASED:
        _handle_order_paid(cardinal, text, chat_id)
        return
    if _plugin and text:
        _plugin.handle_buyer_message(text, chat_id)


def on_new_message(cardinal: Cardinal, event: NewMessageEvent) -> None:
    msg = event.message
    if msg.type == MessageTypes.ORDER_PURCHASED:
        _handle_order_paid(cardinal, (getattr(msg, "text", None) or str(msg)).strip(), msg.chat_id)
        return
    if msg.type != MessageTypes.NON_SYSTEM or msg.author_id == cardinal.account.id:
        return
    _handle_fp_message(cardinal, (getattr(msg, "text", None) or "").strip(), msg.chat_id, msg.type)


def on_last_chat(cardinal: Cardinal, event: LastChatMessageChangedEvent) -> None:
    if not cardinal.old_mode_enabled or not event.chat.unread:
        return
    chat = event.chat
    text = str(chat).strip()
    if chat.last_message_type == MessageTypes.ORDER_PURCHASED:
        _handle_order_paid(cardinal, text, chat.id)
        return
    _handle_fp_message(cardinal, text, chat.id)


def safe_handler(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.error("%s: %s: %s", _P, func.__name__, exc)
            logger.debug(traceback.format_exc())
    wrapper.__name__ = func.__name__
    return wrapper


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_NEW_ORDER = [safe_handler(on_new_order)]
BIND_TO_ORDER_STATUS_CHANGED = [safe_handler(on_order_status_changed)]
BIND_TO_NEW_MESSAGE = [safe_handler(on_new_message)]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [safe_handler(on_last_chat)]

logger.info("$MAGENTA%s v%s загружен.$RESET", _P, VERSION)
