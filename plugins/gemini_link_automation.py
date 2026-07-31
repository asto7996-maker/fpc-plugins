from __future__ import annotations

# === ОБЯЗАТЕЛЬНЫЕ ПОЛЯ FunPay Cardinal (НЕ УДАЛЯТЬ) ===
NAME = "Gemini Link Auto"
VERSION = "2.1.2"
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

from FunPayAPI.common import exceptions as fp_exceptions
from FunPayAPI.common.enums import OrderStatuses
from FunPayAPI.common.utils import RegularExpressions
from FunPayAPI.types import MessageTypes
from FunPayAPI.updater.events import (
    LastChatMessageChangedEvent,
    NewMessageEvent,
    NewOrderEvent,
)
from cardinal import Cardinal
from tg_bot import CBT

logger = logging.getLogger("FPC.GeminiLink")
_P = "GeminiLink"
_URL_RE = re.compile(r"https?://[^\s<>\"\'{}|\\^`\[\]]+", re.I)

STORAGE_DIR = f"storage/plugins/{UUID}"
SETTINGS_FILE = f"{STORAGE_DIR}/settings.json"
PROCESSED_FILE = f"{STORAGE_DIR}/processed_orders.json"
WAIT_QUEUE_FILE = f"{STORAGE_DIR}/wait_queue.json"
INVENTORY_FILE = f"{STORAGE_DIR}/inventory.json"
RESTOCK_SUBS_FILE = f"{STORAGE_DIR}/restock_subscribers.json"
ATTENTION_FILE = f"{STORAGE_DIR}/manual_attention.json"
PENDING_DELIVERY_FILE = f"{STORAGE_DIR}/pending_delivery.json"
TEST_LINKS_FILE = f"{STORAGE_DIR}/test_links.json"
STATE_FILE = f"{STORAGE_DIR}/state.json"

_file_lock = threading.Lock()
_order_lock = threading.Lock()
_disabled_chats: Dict[str, float] = {}
_inflight_orders: Dict[str, float] = {}
_processing_msg_sent: Dict[str, float] = {}
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
    "test_mode": False,
    "link_parts_count": 3,
    "funpay_max_message_len": 200,
    "delivery_split_sleep_sec": 3.0,
    "delivery_send_attempts": 4,
    "delivery_use_paste_fallback": False,
    "processing_message": (
        "⏳ Заказ #{order_id} принят!\n"
        "Формируем ссылку Gemini 18 мес. — подождите 1–2 минуты..."
    ),
    "delivery_header_message": (
        "🎉 Ваша подписка Gemini готова!\n\n"
        "📋 Заказ: #{order_id}\n"
        "🔗 Ссылка(и) для активации — в следующем сообщении(ях).\n"
        "📌 Активируйте сразу после получения!\n\n"
        "Спасибо за покупку! ⭐"
    ),
    "delivery_message": (
        "🎉 Ваша подписка Gemini готова!\n\n"
        "🔗 Ссылка для активации (18 месяцев):\n"
        "{link}\n\n"
        "📌 Активируйте сразу после получения!\n"
        "📋 Заказ: #{order_id}\n\n"
        "Спасибо за покупку! ⭐"
    ),
    "delivery_link_message": "🔗 {label}\n{link}",
    "delivery_paste_message": (
        "🎉 Ваша подписка Gemini готова!\n\n"
        "📋 Заказ: #{order_id}\n"
        "📎 Ссылок: {count} шт.\n\n"
        "Все ссылки здесь (скопируйте и активируйте):\n"
        "{paste_url}\n\n"
        "📌 Ссылка действует ограниченное время — активируйте сразу!"
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


def _is_paid_notification(text: str) -> bool:
    res = RegularExpressions()
    return bool(res.ORDER_PURCHASED.search(text or "") and res.ORDER_PURCHASED2.search(text or ""))


def send_fp(
    c: Cardinal,
    chat_id: Any,
    text: str,
    buyer: Optional[str] = None,
    buyer_id: Optional[int] = None,
    watermark: Optional[bool] = None,
) -> bool:
    if not chat_id:
        logger.warning("%s: send_fp без chat_id", _P)
        return False
    kwargs: Dict[str, Any] = {}
    if buyer:
        kwargs["chat_name"] = buyer
    if buyer_id:
        kwargs["interlocutor_id"] = buyer_id
    if watermark is not None:
        kwargs["watermark"] = watermark
    try:
        result = c.send_message(chat_id, text, **kwargs)
        if result:
            return True
    except Exception as exc:
        logger.debug("%s: cardinal.send_message: %s", _P, exc)
    return _send_fp_raw(c, chat_id, text, buyer)


def _normalize_chat_id(chat_id: Any) -> Any:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return chat_id


def _send_fp_raw(c: Cardinal, chat_id: Any, text: str, buyer: Optional[str] = None) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    cid = _normalize_chat_id(chat_id)
    settings = load_settings()
    attempts = int(settings.get("delivery_send_attempts", 4))
    for attempt in range(1, attempts + 1):
        try:
            c.account.send_message(cid, text, buyer)
            _log_action("FunPay отправлено", chat_id=cid, len=len(text), attempt=attempt)
            return True
        except fp_exceptions.MessageNotDeliveredError as exc:
            logger.error(
                "%s: FunPay отклонил (%s симв.): %s",
                _P, len(text), exc.error_message or exc.short_str(),
            )
        except Exception as exc:
            logger.error("%s: send_message chat=%s: %s", _P, cid, exc)
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    return False


def _send_text_adaptive(
    c: Cardinal,
    chat_id: Any,
    text: str,
    buyer: Optional[str],
    max_len: int,
    sleep_sec: float,
) -> bool:
    text = text.strip()
    if not text:
        return True
    if len(text) <= max_len:
        return _send_fp_raw(c, chat_id, text, buyer)

    chunks = _chunk_text(text, max_len)
    for idx, chunk in enumerate(chunks):
        label = f"({idx + 1}/{len(chunks)}) "
        piece = label + chunk if len(label + chunk) <= max_len else chunk
        if len(piece) > max_len:
            sub_chunks = _chunk_text(chunk, max(80, max_len - len(label)))
            for sub_idx, sub in enumerate(sub_chunks):
                sub_piece = f"({idx + 1}.{sub_idx + 1}) {sub}"
                if len(sub_piece) > max_len:
                    sub_piece = sub
                if not _send_fp_raw(c, chat_id, sub_piece, buyer):
                    return False
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
            continue
        if not _send_fp_raw(c, chat_id, piece, buyer):
            return False
        if sleep_sec > 0 and idx < len(chunks) - 1:
            time.sleep(sleep_sec)
    return True


def _cleanup_stale(keys: Dict[str, float], ttl: float) -> None:
    now = time.time()
    stale = [k for k, ts in keys.items() if now - ts > ttl]
    for k in stale:
        keys.pop(k, None)


def _try_begin_order(order_id: str, force: bool = False) -> bool:
    now = time.time()
    with _order_lock:
        _cleanup_stale(_inflight_orders, 600)
        if force:
            _inflight_orders.pop(order_id, None)
        if order_id in _inflight_orders and not force:
            logger.debug("%s: #%s уже обрабатывается — пропуск", _P, order_id)
            return False
        _inflight_orders[order_id] = now
        return True


def _end_order(order_id: str) -> None:
    with _order_lock:
        _inflight_orders.pop(order_id, None)


def _mark_processing_sent(order_id: str) -> bool:
    """True если можно отправить «формируем ссылку» (ещё не отправляли)."""
    now = time.time()
    with _order_lock:
        _cleanup_stale(_processing_msg_sent, 86400)
        if order_id in _processing_msg_sent:
            return False
        _processing_msg_sent[order_id] = now
        return True


def _normalize_links(link_data: Any) -> List[str]:
    links: List[str] = []

    def add(raw: str) -> None:
        for url in _URL_RE.findall(raw or ""):
            u = url.rstrip(".,;)")
            if u not in links:
                links.append(u)

    if isinstance(link_data, list):
        for item in link_data:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(json.dumps(item, ensure_ascii=False))
            else:
                add(str(item))
    else:
        add(str(link_data or ""))
    return links


def _extract_urls_from_json(data: Any) -> List[str]:
    links: List[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for val in obj:
                walk(val)
        elif isinstance(obj, str):
            for url in _URL_RE.findall(obj):
                u = url.rstrip(".,;)")
                if u not in links:
                    links.append(u)

    walk(data)
    return links


def _chunk_text(text: str, max_len: int) -> List[str]:
    if len(text) <= max_len:
        return [text]
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]


def _upload_paste(content: str) -> Optional[str]:
    settings = load_settings()
    if not settings.get("delivery_use_paste_fallback"):
        return None
    try:
        resp = requests.post(
            "https://dpaste.com/api/v2/",
            data={"content": content, "expiry_days": 7},
            timeout=20,
        )
        if resp.ok:
            url = resp.text.strip()
            if url.startswith("http"):
                return url
    except Exception as exc:
        logger.warning("%s: paste upload failed: %s", _P, exc)
    return None


def _split_link_parts(link: str, parts_count: int = 3, max_part_len: int = 90) -> List[str]:
    """Делит ссылку на N частей; при необходимости увеличивает N, чтобы часть ≤ max_part_len."""
    link = (link or "").strip()
    if not link:
        return []
    n = max(1, int(parts_count))
    max_len = max(40, int(max_part_len))
    while len(link) // n + (1 if len(link) % n else 0) > max_len:
        n += 1
    if len(link) <= n:
        return [link]
    size = len(link) // n
    rem = len(link) % n
    parts: List[str] = []
    pos = 0
    for i in range(n):
        chunk = size + (1 if i < rem else 0)
        parts.append(link[pos:pos + chunk])
        pos += chunk
    return parts


def send_fp_delivery(
    c: Cardinal,
    chat_id: Any,
    order_id: str,
    links: List[str],
    buyer: str = "",
    buyer_id: Optional[int] = None,
    is_test: bool = False,
) -> bool:
    if not chat_id or not links:
        return False
    settings = load_settings()
    parts_count = int(settings.get("link_parts_count", 3))
    max_part = int(settings.get("funpay_max_message_len", 200)) // 2
    sleep_sec = float(settings.get("delivery_split_sleep_sec", 3.0))

    tag = "🧪 ТЕСТ — " if is_test else ""
    header = (
        f"{tag}Gemini 18m #{order_id}\n"
        f"Ссылка будет в нескольких сообщениях.\n"
        f"Склейте части по порядку (1, 2, 3…) в адресной строке."
    )
    if not _send_fp_raw(c, chat_id, header, buyer):
        logger.error("%s: заголовок не отправлен #%s", _P, order_id)
        return False

    time.sleep(sleep_sec)

    for link_num, link in enumerate(links, 1):
        if len(links) > 1:
            if not _send_fp_raw(c, chat_id, f"--- ссылка {link_num}/{len(links)} ---", buyer):
                return False
            time.sleep(sleep_sec)

        parts = _split_link_parts(link, parts_count, max_part)
        if not parts:
            return False
        total = len(parts)

        for idx, part in enumerate(parts, 1):
            # Только ASCII-префикс + часть URL (без emoji в теле ссылки)
            msg = f"[{idx}/{total}] {part}"
            if not _send_fp_raw(c, chat_id, msg, buyer):
                # повтор без префикса
                if not _send_fp_raw(c, chat_id, part, buyer):
                    logger.error(
                        "%s: часть %s/%s не отправлена (#%s, len=%s)",
                        _P, idx, total, order_id, len(part),
                    )
                    return False
            if idx < len(parts):
                time.sleep(sleep_sec)

    time.sleep(sleep_sec)
    _send_fp_raw(
        c, chat_id,
        "Готово! Склейте части подряд и откройте в браузере. Спасибо!",
        buyer,
    )
    return True


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


def load_pending_delivery() -> Dict[str, Any]:
    return _load_json(PENDING_DELIVERY_FILE, {})


def save_pending_delivery(data: Dict[str, Any]) -> None:
    _save_json(PENDING_DELIVERY_FILE, data)


def get_pending_links(order_id: str) -> Optional[List[str]]:
    entry = load_pending_delivery().get(order_id)
    if not entry:
        return None
    links = entry.get("links") or []
    return links if links else None


def set_pending_links(order_id: str, links: List[str], chat_id: Any, buyer: str) -> None:
    data = load_pending_delivery()
    data[order_id] = {
        "links": links,
        "chat_id": chat_id,
        "buyer": buyer,
        "at": _ts(),
    }
    save_pending_delivery(data)


def clear_pending_links(order_id: str) -> None:
    data = load_pending_delivery()
    if order_id in data:
        del data[order_id]
        save_pending_delivery(data)


def load_test_links() -> List[str]:
    raw = _load_json(TEST_LINKS_FILE, [])
    links: List[str] = []
    for item in raw if isinstance(raw, list) else []:
        for url in _normalize_links(item):
            if url not in links:
                links.append(url)
    return links


def save_test_links(links: List[str]) -> None:
    _save_json(TEST_LINKS_FILE, links[-30:])


def archive_test_link(link: str) -> None:
    links = load_test_links()
    for url in _normalize_links(link):
        if url not in links:
            links.append(url)
    save_test_links(links)


def get_test_link() -> Optional[str]:
    """Берёт тестовую ссылку без удаления (переиспользование)."""
    links = load_test_links()
    if links:
        idx = int(time.time() // 120) % len(links)
        return links[idx]
    for entry in load_pending_delivery().values():
        pending = entry.get("links") or []
        if pending:
            return pending[0]
    inv = OrderQueue.load_inventory()
    if inv:
        return inv[0].get("link")
    return None


def import_pending_to_test_links() -> int:
    added = 0
    links = load_test_links()
    for entry in load_pending_delivery().values():
        for url in _normalize_links(entry.get("links") or []):
            if url not in links:
                links.append(url)
                added += 1
    save_test_links(links)
    return added


def _is_test_mode() -> bool:
    return bool(load_settings().get("test_mode"))


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
        links = _extract_urls_from_json(data)
        if links:
            return links

        def add_link(value: Any) -> None:
            if not isinstance(value, str):
                return
            for url in _URL_RE.findall(value.strip()):
                u = url.rstrip(".,;)")
                if u not in links:
                    links.append(u)

        for key in ("items", "links", "keys", "accounts", "deliveries", "purchased"):
            items = data.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str):
                        add_link(item)
                    elif isinstance(item, dict):
                        for k in (
                            "link", "url", "value", "item", "activation_link",
                            "text", "content", "data", "key", "account", "delivery",
                        ):
                            add_link(item.get(k))

        for key in ("activation_link", "link", "url", "item", "content", "text"):
            add_link(data.get(key))

        nested = data.get("data") or data.get("result") or data.get("purchase") or data.get("order")
        if isinstance(nested, dict):
            for u in cls._extract_links(nested):
                if u not in links:
                    links.append(u)

        return links

    @classmethod
    def purchase(cls, quantity: int = 1) -> Dict[str, Any]:
        products = cls.get_products()
        product = cls._resolve_product(products)
        product_id = int(product.get("id", 0))
        stock = int(product.get("stock_count", product.get("stock", 0)))
        qty = max(1, min(quantity, stock))
        if stock <= 0:
            _log_action("API: нет stock", product_id=product_id, stock=stock)
            return {"items": [], "status": "out_of_stock", "product_id": product_id}

        _log_action("API: покупка", product_id=product_id, qty=qty)
        data = cls._request("POST", cls._settings()["api_path_purchase"], {"product_id": product_id, "quantity": qty})
        links = cls._extract_links(data)
        if not links:
            logger.error("%s: /api/buy без ссылок, ответ: %s", _P, json.dumps(data, ensure_ascii=False)[:500])
            return {
                "items": [],
                "status": "no_links",
                "product_id": product_id,
                "raw": data,
                "transaction_id": data.get("transaction_id"),
            }
        _log_action("API: куплено", qty=len(links), product_id=product_id)
        return {
            "items": links,
            "status": "ok",
            "product_id": product_id,
            "transaction_id": data.get("transaction_id"),
        }

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

    def _deliver(
        self,
        order_id: str,
        chat_id: Any,
        links: Any,
        buyer: str = "",
        buyer_id: Optional[int] = None,
        is_test: bool = False,
    ) -> bool:
        link_list = _normalize_links(links)
        if not link_list:
            logger.error("%s: #%s — пустые ссылки для выдачи", _P, order_id)
            self.notify_seller(f"⚠️ <b>{NAME}</b> #{order_id} — API не вернул ссылку")
            return False

        set_pending_links(order_id, link_list, chat_id, buyer)

        if chat_id:
            if not send_fp_delivery(
                self.cardinal, chat_id, order_id, link_list, buyer, buyer_id, is_test=is_test,
            ):
                self.notify_seller(
                    f"⚠️ <b>{NAME}</b> #{order_id} — не удалось отправить в FunPay\n"
                    f"Ссылка сохранена. Повтор: <code>/gl_process {order_id}</code>\n"
                    f"👤 {html.escape(buyer or '—')}"
                )
                return False

        clear_pending_links(order_id)
        self._mark_processed(order_id)
        OrderQueue.remove(order_id)
        self.notify_seller(
            f"✅ <b>{NAME}</b>\n\nЗаказ <code>#{order_id}</code> выдан "
            f"({len(link_list)} ссыл.).\n👤 {html.escape(buyer or '—')}"
        )
        return True

    def _purchase_links(self, quantity: int) -> List[str]:
        purchase = SupplierBotAPI.purchase(quantity)
        status = purchase.get("status")
        if status == "ok":
            items = purchase.get("items") or []
            for link in items:
                archive_test_link(link)
            return items
        if status == "no_links":
            self.notify_seller(
                f"⚠️ <b>{NAME}</b> Покупка прошла, но ссылка не распознана.\n"
                f"Проверьте логи /api/buy"
            )
        return []

    def _get_test_links_for_order(self, quantity: int) -> List[str]:
        pool = load_test_links()
        if not pool:
            return []
        result: List[str] = []
        for i in range(quantity):
            result.append(pool[i % len(pool)])
        return result

    def _try_fulfill_with_link(self, entry: Dict[str, Any], link: str) -> None:
        self._deliver(
            entry["order_id"], entry.get("chat_id"), link,
            entry.get("buyer", ""), entry.get("buyer_id"),
        )

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
        if _is_test_mode():
            return
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
        settings = load_settings()
        use_test = bool(settings.get("test_mode"))
        if not use_test and not _is_configured(settings):
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

        if not _try_begin_order(order_id, force=force):
            return

        buyer = full_order.buyer_username
        buyer_id = getattr(full_order, "buyer_id", None)
        chat_id = _resolve_chat_id(self.cardinal, buyer, chat_id_hint)
        try:
            quantity = _order_quantity(full_order)

            _log_action("Обработка заказа", order_id=order_id, buyer=buyer, qty=quantity, chat_id=chat_id)
            if chat_id and _mark_processing_sent(order_id):
                send_fp(
                    self.cardinal, chat_id,
                    _format_template("processing_message", order_id=order_id),
                    buyer=buyer, buyer_id=buyer_id, watermark=False,
                )

            pending = get_pending_links(order_id)
            if pending:
                _log_action("Повторная выдача сохранённой ссылки", order_id=order_id)
                if self._deliver(order_id, chat_id, pending, buyer, buyer_id):
                    return

            if use_test:
                test_links = self._get_test_links_for_order(quantity)
                if not test_links:
                    self.notify_seller(
                        f"🧪 <b>{NAME}</b> Тестовый режим: нет ссылок в пуле.\n"
                        f"Добавьте: <code>/gl_add_test_link URL</code>\n"
                        f"Или импорт: <code>/gl_import_test_links</code>"
                    )
                    if chat_id:
                        send_fp(
                            self.cardinal, chat_id,
                            f"🧪 Тест: ссылка для #{order_id} не настроена. Продавец уведомлён.",
                            buyer=buyer, buyer_id=buyer_id,
                        )
                    return
                _log_action("Тестовая выдача (без покупки)", order_id=order_id, links=len(test_links))
                if self._deliver(order_id, chat_id, test_links, buyer, buyer_id, is_test=True):
                    return
                self._notify_attention(order_id, chat_id, "test_delivery_failed")
                return

            links_from_inv: List[str] = []
            for _ in range(quantity):
                link = OrderQueue.pop_link()
                if link:
                    links_from_inv.append(link)
                else:
                    break
            if len(links_from_inv) == quantity:
                if self._deliver(order_id, chat_id, links_from_inv, buyer, buyer_id):
                    return
            for link in links_from_inv:
                OrderQueue.push_links([link])

            stock = SupplierBotAPI.get_stock()
            _log_action("Stock check", order_id=order_id, available=stock["available"], price=stock["price"])
            if stock["available"] <= 0:
                self._enqueue_out_of_stock(order_id, chat_id, buyer, quantity)
                return

            balance = SupplierBotAPI.get_balance()
            _log_action("Balance check", order_id=order_id, balance=balance["balance"])
            if balance["balance"] < stock["price"] * quantity:
                self._notify_attention(order_id, chat_id, "insufficient_balance")
                if chat_id:
                    send_fp(
                        self.cardinal, chat_id,
                        f"⏳ Заказ #{order_id} принят.\n"
                        "Сейчас пополняем баланс поставщика — выдадим ссылку в ближайшее время.",
                        buyer=buyer, buyer_id=buyer_id,
                    )
                return

            items = self._purchase_links(quantity)
            if items:
                if self._deliver(order_id, chat_id, items, buyer, buyer_id):
                    return
                self._notify_attention(order_id, chat_id, "delivery_failed")
                return

            if stock["available"] > 0:
                self._notify_attention(order_id, chat_id, "purchase_no_links")
            self._enqueue_out_of_stock(order_id, chat_id, buyer, quantity)
        except Exception as exc:
            logger.error("%s: process_order #%s: %s", _P, order_id, exc)
            logger.debug(traceback.format_exc())
            self._notify_attention(order_id, chat_id, "api_failure")
            if chat_id:
                send_fp(
                    self.cardinal, chat_id,
                    _format_template("out_of_stock_message", order_id=order_id),
                    buyer=buyer, buyer_id=buyer_id,
                )
        finally:
            _end_order(order_id)

    def _enqueue_out_of_stock(self, order_id: str, chat_id: Any, buyer: str, quantity: int) -> None:
        OrderQueue.add_waiting(order_id, chat_id, buyer, quantity)
        if chat_id:
            send_fp(self.cardinal, chat_id, _format_template("out_of_stock_message", order_id=order_id), buyer=buyer)

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
    test_links = len(load_test_links())
    test_on = bool(s.get("test_mode"))
    return (
        f"⚙️ <b>{NAME}</b> v{VERSION}\n\n"
        f"🧪 Тест: {'🟢 ВКЛ (все заказы — тест-ссылки)' if test_on else '⚪ выкл'} | Пул: {test_links} ссыл.\n"
        f"API: {'🟢' if _is_configured(s) else '🔴'}\n"
        f"🌐 <code>{html.escape(s.get('bot_api_url', '—'))}</code>\n"
        f"🔑 <code>{html.escape(_mask_key(s.get('bot_api_key', '')))}</code>\n"
        f"🆔 Product ID: <code>{html.escape(str(s.get('bot_product_id') or 'auto'))}</code>\n"
        f"📦 Резерв: {inv} | Очередь: {wait}\n"
        f"🔗 Выдача: {s.get('link_parts_count', 3)} части | пауза {s.get('delivery_split_sleep_sec', 3)}с\n"
        f"🔄 Автозакупка: {s.get('auto_buy_quantity', 5)} шт. при рестоке\n\n"
        f"/gl_test_mode on|off · /gl_add_test_link URL · /gl_import_test_links\n"
        f"/gemini_link /gl_process /gl_resend /gl_stock /gl_balance"
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
    test_on = bool(load_settings().get("test_mode"))
    kb.add(InlineKeyboardButton(
        f"🧪 Тест: {'ВКЛ' if test_on else 'ВЫКЛ'}",
        callback_data="gla:toggle_test",
    ))
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
            elif action == "toggle_test":
                settings = load_settings()
                settings["test_mode"] = not bool(settings.get("test_mode"))
                save_settings(settings)
                state = "ВКЛ — тест-ссылки, покупка отключена" if settings["test_mode"] else "ВЫКЛ"
                _edit_panel(bot, cid, mid)
                _answer(call, f"🧪 Тест {state}", alert=True)
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
        mode = "🧪 тестовая выдача" if _is_test_mode() else "покупка и выдача"
        bot.reply_to(msg, f"⏳ {mode} #{oid}...")
        _plugin.schedule_process(oid, force=True, delay=0)

    def cmd_test_mode(msg):
        parts = (msg.text or "").split()
        settings = load_settings()
        if len(parts) >= 2:
            arg = parts[1].lower()
            if arg in ("on", "1", "вкл", "enable", "true"):
                settings["test_mode"] = True
            elif arg in ("off", "0", "выкл", "disable", "false"):
                settings["test_mode"] = False
            else:
                bot.reply_to(msg, "Использование: /gl_test_mode on|off")
                return
            save_settings(settings)
        state = "ВКЛ (все заказы — тест-ссылки, API не тратится)" if settings.get("test_mode") else "ВЫКЛ"
        pool = len(load_test_links())
        bot.reply_to(
            msg,
            f"🧪 Тестовый режим: {state}\n"
            f"Пул ссылок: {pool}\n\n"
            f"При ВКЛ: покупка с любого аккаунта → тест-ссылка из пула.\n"
            f"Не забудьте /gl_test_mode off перед боевой работой!\n\n"
            f"Добавить: /gl_add_test_link URL\n"
            f"Импорт из pending: /gl_import_test_links",
        )

    def cmd_add_test_link(msg):
        text = (msg.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(msg, "Использование: /gl_add_test_link https://...")
            return
        urls = _normalize_links(parts[1])
        if not urls:
            bot.reply_to(msg, "❌ URL не найден в сообщении")
            return
        for url in urls:
            archive_test_link(url)
        bot.reply_to(msg, f"✅ Добавлено {len(urls)} ссыл. в тест-пул (всего {len(load_test_links())})")

    def cmd_import_test_links(msg):
        added = import_pending_to_test_links()
        bot.reply_to(
            msg,
            f"✅ Импортировано {added} ссыл. из pending\n"
            f"Всего в тест-пуле: {len(load_test_links())}",
        )

    def cmd_test_buy(msg):
        if not _is_configured():
            bot.reply_to(msg, "⚠️ API не настроен")
            return
        bot.reply_to(msg, "⏳ Тестовая покупка 1 шт...")
        def worker():
            try:
                st = SupplierBotAPI.get_stock()
                bal = SupplierBotAPI.get_balance()
                purchase = SupplierBotAPI.purchase(1)
                links = purchase.get("items") or []
                bot.send_message(
                    msg.chat.id,
                    f"📦 Stock: {st['available']}\n💰 Balance: ${bal['balance']:.2f}\n"
                    f"Status: {purchase.get('status')}\n"
                    f"Links: {len(links)}\n"
                    + (f"Sample: {links[0][:80]}…" if links else "❌ Ссылка не получена"),
                )
            except Exception as exc:
                bot.send_message(msg.chat.id, f"❌ {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def cmd_resend(msg):
        parts = (msg.text or "").split()
        if len(parts) < 2:
            bot.reply_to(msg, "/gl_resend ORDER_ID")
            return
        oid = parts[1].strip().lstrip("#").upper()
        pending = get_pending_links(oid)
        if not pending:
            bot.reply_to(msg, f"❌ Нет сохранённой ссылки для #{oid}. Используйте /gl_process {oid}")
            return
        bot.reply_to(msg, f"⏳ Повторная отправка #{oid} ({len(pending)} ссыл.)...")
        entry = load_pending_delivery().get(oid, {})
        chat_id = entry.get("chat_id")
        buyer = entry.get("buyer", "")

        def worker():
            ok = send_fp_delivery(_plugin.cardinal, chat_id, oid, pending, buyer)
            if ok:
                clear_pending_links(oid)
                _plugin._mark_processed(oid)
                bot.send_message(msg.chat.id, f"✅ #{oid} отправлен")
            else:
                bot.send_message(msg.chat.id, f"❌ #{oid} — FunPay отклонил сообщения. Проверьте логи.")
        threading.Thread(target=worker, daemon=True).start()

    tg.msg_handler(cmd_resend, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_resend")
    tg.msg_handler(cmd_test_mode, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_test_mode")
    tg.msg_handler(cmd_add_test_link, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_add_test_link")
    tg.msg_handler(cmd_import_test_links, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_import_test_links")
    tg.msg_handler(cmd_test_buy, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_test_buy")

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
            ("gl_test_mode", "тестовый режим on/off", True),
            ("gl_add_test_link", "добавить тест-ссылку", True),
            ("gl_import_test_links", "импорт ссылок из pending", True),
            ("gl_test_buy", "тест покупки API ($)", True),
            ("gl_resend", "повторить выдачу", True),
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
    """Резервный триггер — основной: ORDER_PURCHASED в чате."""
    if not _plugin:
        return
    order = event.order
    hint = _extract_order_text(order)
    if hint and not _matches_keywords(hint):
        return
    _plugin.schedule_process(str(order.id), delay=5)


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
    text = (getattr(msg, "text", None) or str(msg)).strip()
    if msg.type == MessageTypes.ORDER_PURCHASED or _is_paid_notification(text):
        _handle_order_paid(cardinal, text, msg.chat_id)
        return
    if msg.type != MessageTypes.NON_SYSTEM or msg.author_id == cardinal.account.id:
        return
    _handle_fp_message(cardinal, text, msg.chat_id, msg.type)


def on_last_chat(cardinal: Cardinal, event: LastChatMessageChangedEvent) -> None:
    if not cardinal.old_mode_enabled or not event.chat.unread:
        return
    chat = event.chat
    text = str(chat).strip()
    if chat.last_message_type == MessageTypes.ORDER_PURCHASED or _is_paid_notification(text):
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
BIND_TO_NEW_MESSAGE = [safe_handler(on_new_message)]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [safe_handler(on_last_chat)]

logger.info("$MAGENTA%s v%s загружен.$RESET", _P, VERSION)
