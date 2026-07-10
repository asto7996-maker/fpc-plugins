from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
#  Gemini Link Auto — автозакупка и выкладка ссылок на FunPay
# ──────────────────────────────────────────────────────────────────────────────

import html
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Final

from cardinal import Cardinal
try:
    from FunPayAPI.updater.events import NewOrderEvent
    _HAS_NEW_ORDER_EVENT = True
except Exception:
    NewOrderEvent = None  # type: ignore[misc, assignment]
    _HAS_NEW_ORDER_EVENT = False
try:
    from tg_bot import CBT
except ImportError:
    class CBT:  # type: ignore[no-redef]
        PLUGIN_SETTINGS = "47"
        EDIT_PLUGIN = "45"
        PLUGINS_LIST = "46"
from telebot.types import CallbackQuery, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM, Message
import telebot


def _pip(pkg: str) -> None:
    from pip._internal.cli.main import main as _m
    _m(["install", "-U", "-q", pkg])


try:
    from requests import get as http_get, post as http_post
except ImportError:
    _pip("requests")
    from requests import get as http_get, post as http_post


NAME          = "Gemini Link Auto"
VERSION       = "3.2.2"
DESCRIPTION   = "ChatGPT автозакупка + выдача Gemini-ссылок из архива"
CREDITS       = "Cursor AI"
UUID          = "f7a2e8c1-4b3d-4e9f-a8c2-1d5e9b0f6a3c"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

MAX_PROMPT_LEN:   Final[int] = 4000
PROMPT_PREVIEW_LEN: Final[int] = 250
DEFAULT_API_URL: Final[str] = "https://worker-production-53ca.up.railway.app"
SHOP_PRODUCT_NAME: Final[str] = "GPT plus 1M (NW)"
DEFAULT_LOT_MATCH: Final[str] = "GPT plus 1M (NW)"
DEFAULT_MIN_LOT_STOCK: Final[int] = 3
DEFAULT_AUTO_INTERVAL: Final[int] = 300
DEFAULT_GEMINI_LOT_MATCH: Final[str] = "gemini link"
DEFAULT_DELIVERY_PARTS: Final[int] = 3
DEFAULT_REDELIVERY_PARTS: Final[int] = 4
LINK_PART_DELAY: Final[float] = 1.8
CONFIRM_REMINDER_DELAY: Final[int] = 300
DEFAULT_CONFIRM_REMINDER: Final[str] = (
    "Заказ выполнен. Пожалуйста, зайдите в раздел «Покупки», "
    "выберите его в списке и нажмите кнопку «Подтвердить выполнение заказа»."
)
LOT_CACHE_TTL: Final[int] = 600
GEMINI_LOT_CACHE_TTL: Final[int] = 300
GEMINI_LOT_SCAN_TIMEOUT: Final[float] = 8.0
GEMINI_LOT_SCAN_MAX_API: Final[int] = 30
PRODUCT_CACHE_TTL: Final[int] = 300
UI_API_TIMEOUT: Final[float] = 5.0
BUY_API_TIMEOUT: Final[float] = 60.0
SETTINGS_FILE     = f"storage/plugins/{UUID}/settings.json"
AUTOBUY_LOG_FILE  = f"storage/plugins/{UUID}/autobuy.json"
IMPORT_LOG_FILE   = f"storage/plugins/{UUID}/import_stock.json"
WAREHOUSE_FILE    = f"storage/plugins/{UUID}/warehouse.json"
GEMINI_ARCHIVE_FILE = f"storage/plugins/{UUID}/gemini_archive.json"
GEMINI_DELIVERY_FILE = f"storage/plugins/{UUID}/gemini_deliveries.json"
BOOT_LOG_FILE        = f"storage/plugins/{UUID}/boot.log"
CB_PREFIX         = f"glnk_{UUID[:8]}"

STOCK_MODES: Final[dict[str, str]] = {
    "auto_buy": "🤖 ChatGPT — автозакупка",
    "gemini_links": "🔗 Gemini — ссылки из архива",
}
STOCK_MODE_ORDER: Final[tuple[str, ...]] = ("auto_buy", "gemini_links")
_LEGACY_STOCK_MODES: Final[frozenset[str]] = frozenset({"import_lot", "warehouse", "stocked"})

_SHOP_ORDER_RE = re.compile(
    r"Order\s*#(?P<order_id>\d+)\s*\n"
    r"Product:\s*(?P<product>[^\n]+)\s*\n"
    r"Price:\s*[^\n]+\s*\n"
    r"Date:\s*[^\n]+\s*\n"
    r"Data:\s*(?P<data>https?://\S+)",
    re.IGNORECASE,
)
_ACTIVATION_LINK_RE = re.compile(
    r"https?://(?:"
    r"serviceactivation\.google\.com/subscription/new/|"
    r"one\.google\.com/activate-plan/subscription/new/"
    r")[^\s]+",
    re.IGNORECASE,
)

logger = logging.getLogger("FPC.GeminiLink")
_P = "[GeminiLink]"

_plugin: "Plugin | None" = None
_tg_ui_registered_for: int | None = None


def _boot_log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {_P} {msg}"
    logger.info("%s %s", _P, msg)
    try:
        os.makedirs(os.path.dirname(BOOT_LOG_FILE), exist_ok=True)
        with open(BOOT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _match_gemini_plugin_settings(call: CallbackQuery) -> bool:
    """Фильтр кнопки «Настройки» в карточке плагина Cardinal."""
    data = (call.data or "").strip()
    parts = data.split(":")
    if len(parts) < 3:
        return False
    if parts[1] != UUID:
        return False
    return parts[0] in ("47", "45", str(CBT.PLUGIN_SETTINGS), str(CBT.EDIT_PLUGIN))


_match_gemini_plugin_settings.__gemini_link_settings__ = True  # type: ignore[attr-defined]


def _is_catch_all_cbq_filter(func) -> bool:
    if func is None:
        return False
    samples = ("", "x", f"47:{UUID}:0", "noop")
    for sample in samples:
        probe = type("CbProbe", (), {"data": sample})()
        try:
            if not func(probe):
                return False
        except Exception:
            return False
    return True


def _remove_gemini_settings_handlers(bot) -> None:
    handlers = bot.callback_query_handlers
    bot.callback_query_handlers[:] = [
        h for h in handlers
        if not getattr(h.get("filters", {}).get("func"), "__gemini_link_settings__", False)
    ]


def _normalize_cbq_handler_order(bot) -> None:
    """Хэндлер настроек плагина — в начало, catch-all default_cp — в конец."""
    handlers = bot.callback_query_handlers
    if not handlers:
        return
    settings_h: list = []
    catch_all_h: list = []
    rest: list = []
    for entry in handlers:
        func = entry.get("filters", {}).get("func")
        if getattr(func, "__gemini_link_settings__", False):
            settings_h.append(entry)
        elif _is_catch_all_cbq_filter(func):
            catch_all_h.append(entry)
        else:
            rest.append(entry)
    bot.callback_query_handlers[:] = settings_h + rest + catch_all_h


def _patch_catch_all_handlers(bot) -> int:
    """default_cp (lambda c: True) не должен перехватывать PLUGIN_SETTINGS этого плагина."""
    patched = 0
    for entry in bot.callback_query_handlers:
        func = entry.get("filters", {}).get("func")
        if func is None or getattr(func, "__gemini_link_patched__", False):
            continue
        if not _is_catch_all_cbq_filter(func):
            continue

        def _make_wrapper(original):
            def _wrapped(call, _orig=original):
                if _match_gemini_plugin_settings(call):
                    return False
                return _orig(call)
            _wrapped.__gemini_link_patched__ = True  # type: ignore[attr-defined]
            return _wrapped

        entry["filters"]["func"] = _make_wrapper(func)
        patched += 1
    return patched


def _ensure_gemini_settings_handler(bot, handler) -> None:
    _remove_gemini_settings_handlers(bot)

    def _run_settings(call: CallbackQuery) -> None:
        _boot_log(f"PLUGIN_SETTINGS click data={call.data!r}")
        handler(call)

    bot.callback_query_handlers.insert(0, {
        "function": _run_settings,
        "pass_bot": False,
        "filters": {"func": _match_gemini_plugin_settings},
    })
    patched = _patch_catch_all_handlers(bot)
    _normalize_cbq_handler_order(bot)
    _boot_log(
        f"PLUGIN_SETTINGS registered | cbq={len(bot.callback_query_handlers)} "
        f"catch_all_patched={patched}"
    )


def _gemini_link_open_settings(call: CallbackQuery) -> None:
    if _plugin is None:
        return
    _plugin.open_settings_card(call)


def _register_priority_cbq(tg, handler, predicate) -> None:
    """Регистрирует callback-хэндлер плагина в начало очереди."""
    tg.cbq_handler(handler, predicate)
    handlers = tg.bot.callback_query_handlers
    if handlers:
        handlers.insert(0, handlers.pop())
    _normalize_cbq_handler_order(tg.bot)


def _escape(val: Any) -> str:
    return html.escape(str(val if val is not None else ""))


def _dig(data: Any, *keys: str, default: Any = None) -> Any:
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _normalize_account_line(raw: str) -> str:
    """Формат выдачи: email|password|2FA."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "|" in s and "@" in s.split("|", 1)[0]:
        parts = [p.strip() for p in s.split("|")]
        if len(parts) >= 3:
            return f"{parts[0]}|{parts[1]}|{parts[2]}"
        return s
    if ":" in s and "@" in s.split(":")[0]:
        parts = [p.strip() for p in s.split(":")]
        if len(parts) >= 3:
            return f"{parts[0]}|{parts[1]}|{parts[2]}"
    return s


def _extract_accounts_from_payload(data: Any) -> list[str]:
    """Достаёт строки аккаунтов email|password|2FA из ответа Shop API."""
    found: list[str] = []

    def add_line(val: Any) -> None:
        if val is None:
            return
        if isinstance(val, str):
            for chunk in re.split(r"[\r\n]+", val):
                line = _normalize_account_line(chunk)
                if line and line not in found:
                    found.append(line)
            return
        if isinstance(val, dict):
            email = str(val.get("email") or val.get("login") or val.get("user") or "").strip()
            password = str(val.get("password") or val.get("pass") or "").strip()
            twofa = str(
                val.get("2fa") or val.get("two_fa") or val.get("otp") or val.get("code") or "",
            ).strip()
            if email and password:
                line = _normalize_account_line(
                    f"{email}|{password}|{twofa}" if twofa else f"{email}|{password}|",
                )
                if line and line not in found:
                    found.append(line)
                return
            parts = [
                str(val.get(k, "")).strip()
                for k in ("account", "data", "item", "content")
                if val.get(k)
            ]
            for part in parts:
                line = _normalize_account_line(part)
                if line and line not in found:
                    found.append(line)

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            add_line(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for key in ("items", "accounts", "account", "data", "content", "result"):
                if key in node:
                    walk(node[key])
            if not found:
                add_line(node)

    walk(data)
    return [a for a in found if "@" in a and "|" in a]


def _shop_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key.strip(), "Content-Type": "application/json"}


def _shop_request(
    api_url: str, api_key: str, method: str, path: str, body: dict | None = None,
    timeout: float = 60,
) -> tuple[Any, str]:
    api_url = (api_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    if not api_url:
        return None, "URL API не задан"
    if not api_key:
        return None, "API-ключ не задан"
    url = f"{api_url}{path}"
    headers = _shop_headers(api_key)
    try:
        if method.upper() == "GET":
            resp = http_get(url, headers=headers, timeout=timeout)
        else:
            resp = http_post(url, json=body or {}, headers=headers, timeout=max(timeout, 30))
        try:
            data = resp.json()
        except Exception:
            return None, (resp.text or f"HTTP {resp.status_code}")[:300]
        if isinstance(data, dict) and data.get("ok") is False:
            return None, str(data.get("error") or data.get("message") or data)[:300]
        if resp.status_code >= 400:
            return None, (resp.text or f"HTTP {resp.status_code}")[:300]
        return data, ""
    except Exception as exc:
        return None, str(exc)


def _shop_get_balance(api_url: str, api_key: str, timeout: float = BUY_API_TIMEOUT) -> tuple[float | None, str]:
    data, err = _shop_request(api_url, api_key, "GET", "/api/me", timeout=timeout)
    if err:
        return None, err
    user = _dig(data, "user")
    if not isinstance(user, dict):
        return None, "не удалось прочитать баланс"
    try:
        return float(user.get("balance", 0)), ""
    except (TypeError, ValueError):
        return None, "некорректный баланс в ответе API"


def _shop_get_products(api_url: str, api_key: str, timeout: float = BUY_API_TIMEOUT) -> tuple[list[dict[str, Any]], str]:
    data, err = _shop_request(api_url, api_key, "GET", "/api/products", timeout=timeout)
    if err:
        return [], err
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, list):
        return [], "список товаров пуст"
    return [p for p in products if isinstance(p, dict)], ""


def _product_display_name(product: dict[str, Any]) -> str:
    for field in ("name_en", "name", "title"):
        val = str(product.get(field, "")).strip()
        if val:
            return val
    return ""


def _product_matches_name(product: dict[str, Any], name: str = SHOP_PRODUCT_NAME) -> bool:
    needle = name.strip().casefold()
    if not needle:
        return False
    disp = _product_display_name(product).casefold()
    return disp == needle or needle in disp


def _shop_find_product(
    products: list[dict[str, Any]],
    name: str = SHOP_PRODUCT_NAME,
    *,
    exact_only: bool = False,
) -> dict[str, Any] | None:
    needle = name.strip().casefold()
    exact: list[dict[str, Any]] = []
    contains: list[dict[str, Any]] = []
    for product in products:
        disp = _product_display_name(product).casefold()
        if not disp:
            continue
        if disp == needle:
            exact.append(product)
        elif needle in disp:
            contains.append(product)
    if exact:
        return exact[0]
    if contains:
        contains.sort(key=lambda p: len(_product_display_name(p)))
        return contains[0]
    if exact_only:
        return None
    return None


def _shop_buy(
    api_url: str, api_key: str, product_id: int, quantity: int,
) -> tuple[list[str], str, dict[str, Any]]:
    quantity = max(1, int(quantity))
    data, err = _shop_request(
        api_url, api_key, "POST", "/api/buy",
        {"product_id": int(product_id), "quantity": quantity},
    )
    if err:
        return [], err, {}
    items = _extract_accounts_from_payload(data)
    if not items and isinstance(data, dict):
        items = [
            _normalize_account_line(str(x))
            for x in data.get("items", [])
            if str(x).strip()
        ]
    items = [x for x in items if x and "@" in x]
    if not items:
        return [], (str(data)[:300] if data else "пустой ответ"), data if isinstance(data, dict) else {}
    if len(items) < quantity:
        return (
            items,
            f"ожидали {quantity} акк., API вернул {len(items)}",
            data if isinstance(data, dict) else {},
        )
    total = _dig(data, "total_price")
    new_bal = _dig(data, "new_balance")
    info = f"куплено {len(items)} шт."
    if total is not None:
        info += f", −${float(total):.2f}"
    if new_bal is not None:
        info += f", баланс ${float(new_bal):.2f}"
    return items[:quantity], info, data if isinstance(data, dict) else {}


def _normalize_search_text(text: str) -> str:
    """Текст для поиска: без HTML, лишних пробелов и zero-width символов."""
    s = html.unescape(str(text or ""))
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("​", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()


def _lot_text_matches(text: str, needle: str) -> bool:
    if not text or not needle:
        return False
    return needle.strip().casefold() in _normalize_search_text(text)


def _normalize_lot_desc_key(text: str) -> str:
    """Ключ для сопоставления заказа с лотом (без emoji / fancy unicode)."""
    s = _normalize_search_text(text)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def _desc_contains_lot_key(order_desc: str, lot_key: str) -> bool:
    if not order_desc or not lot_key:
        return False
    if lot_key in order_desc:
        return True
    norm_order = _normalize_lot_desc_key(order_desc)
    norm_key = _normalize_lot_desc_key(lot_key)
    if not norm_key or len(norm_key) < 8:
        return False
    return norm_key in norm_order


def _order_looks_like_gemini_activation(order_text: str) -> bool:
    blob = _normalize_lot_desc_key(order_text)
    if "gemini" not in blob:
        return False
    markers = ("активац", "ссылк", "activate", "activation", "link")
    return any(m in blob for m in markers)


def _normalize_activation_url(url: str) -> str:
    u = (url or "").strip()
    if u.lower().startswith("https://"):
        u = "https://" + u[8:]
    elif u.lower().startswith("http://"):
        u = "http://" + u[7:]
    return u


def _import_target_from_state(state: str) -> str:
    state = str(state or "")
    if state.endswith(":gemini_archive"):
        return "gemini_archive"
    if state.endswith(":warehouse"):
        return "warehouse"
    return "lot"


def extract_activation_urls(text: str) -> list[str]:
    found: list[str] = []
    for raw in _ACTIVATION_LINK_RE.findall(text or ""):
        url = _normalize_activation_url(raw)
        if url and url not in found:
            found.append(url)
    return found


def split_link_parts(url: str, parts: int) -> list[str]:
    url = (url or "").strip()
    parts = max(1, int(parts))
    if parts == 1:
        return [url]
    size = (len(url) + parts - 1) // parts
    return [url[i:i + size] for i in range(0, len(url), size)]


def parse_shop_purchase_history(text: str) -> list[dict[str, str]]:
    """
    Разбирает текст Purchase History из шопа.
    Возвращает список {order_id, product, url}.
    """
    items: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    seen_orders: set[str] = set()

    for m in _SHOP_ORDER_RE.finditer(text or ""):
        order_id = m.group("order_id")
        product = (m.group("product") or "").strip()
        url = _normalize_activation_url(m.group("data") or "")
        if not url or url in seen_urls or order_id in seen_orders:
            continue
        seen_urls.add(url)
        seen_orders.add(order_id)
        items.append({"order_id": order_id, "product": product, "url": url})

    if items:
        return items

    for url in _ACTIVATION_LINK_RE.findall(text or ""):
        url = _normalize_activation_url(url)
        if url and url not in seen_urls:
            seen_urls.add(url)
            items.append({"order_id": "", "product": "", "url": url})
    return items


# ═════════════════════════════════════════════════════════════════════════════
#  Plugin
# ═════════════════════════════════════════════════════════════════════════════

class Plugin:
    def __init__(self, cardinal: Cardinal) -> None:
        self.cardinal = cardinal
        self._cfg: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._autobuy_running = False
        self._import_running = False
        self._import_buffers: dict[tuple[int, int], str] = {}
        self._import_pending: dict[int, list[dict[str, str]]] = {}
        self._import_target: dict[int, str] = {}
        self._cached_lot_id: int | None = None
        self._cached_lot_ts: float = 0.0
        self._product_cache: dict[str, Any] | None = None
        self._product_cache_ts: float = 0.0
        self._buy_plan_cache: dict[str, Any] | None = None
        self._buy_plan_cache_ts: float = 0.0
        self._lot_stock_cache: str = ""
        self._lot_stock_cache_ts: float = 0.0
        self._stop_auto = False
        self._auto_thread: threading.Thread | None = None
        self._gemini_lots_cache: list[int] = []
        self._gemini_lots_cache_ts: float = 0.0
        self._gemini_lots_scan_lock = threading.Lock()
        self._gemini_lots_scan_running = False
        self._delivery_in_progress: set[str] = set()
        self.reload_settings()

    def open_settings_card(self, call: CallbackQuery) -> None:
        """Открывает панель настроек из карточки плагина (кнопка ⚙️ Настройки)."""
        tg = self.cardinal.telegram
        if not tg:
            return
        bot = tg.bot
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        try:
            text = self.render_settings_text("hub", instant=True)
            kb = self.build_settings_keyboard("hub", instant=True)
            try:
                bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
            threading.Thread(
                target=self._refresh_settings_ui_bg, args=(chat_id, msg_id, "hub"), daemon=True,
            ).start()
            self.log("открыты настройки из карточки плагина")
        except Exception as exc:
            self.log("ошибка открытия настроек: %s", exc)
            logger.debug(traceback.format_exc())
            try:
                bot.send_message(
                    chat_id,
                    f"⚠️ Не удалось открыть настройки: <code>{_escape(exc)[:180]}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    def _refresh_settings_ui_bg(self, chat_id: int, msg_id: int, page: str = "hub") -> None:
        if not self.cardinal.telegram:
            return
        bot = self.cardinal.telegram.bot
        try:
            if page == "hub":
                if self.stock_mode() == "gemini_links":
                    self.refresh_gemini_lots_async()
                    scan_t = threading.Thread(
                        target=lambda: self._scan_gemini_lots(
                            timeout=GEMINI_LOT_SCAN_TIMEOUT, blocking=True,
                        ),
                        daemon=True,
                        name="GeminiLotScanUI",
                    )
                    scan_t.start()
                    scan_t.join(timeout=GEMINI_LOT_SCAN_TIMEOUT + 1)
                else:
                    lot_box: list[Any] = [None]
                    plan_box: list[Any] = [None]

                    def _fetch_lot() -> None:
                        lot_box[0] = self._resolve_autobuy_lot_ids(silent=True, fast=False)

                    def _fetch_plan() -> None:
                        plan_box[0] = self.calc_buy_plan(timeout=UI_API_TIMEOUT)

                    t_lot = threading.Thread(target=_fetch_lot, daemon=True)
                    t_plan = threading.Thread(target=_fetch_plan, daemon=True)
                    t_lot.start()
                    t_plan.start()
                    lot_timeout = max(UI_API_TIMEOUT + 2, len(self._iter_profile_lot_ids()) * 0.35 + 5)
                    t_lot.join(timeout=lot_timeout)
                    t_plan.join(timeout=UI_API_TIMEOUT + 2)
            elif page == "settings":
                if self.stock_mode() == "gemini_links":
                    self.refresh_gemini_lots_async()
                else:
                    self._lot_stock_info(fast=True)
            text = self.render_settings_text(page, fast=True, instant=False)
            kb = self.build_settings_keyboard(page, fast=False, instant=False)
            try:
                bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
        except Exception as exc:
            self.log("refresh UI error: %s", exc)
            logger.debug(traceback.format_exc())

    def ensure_telegram_handlers(self) -> None:
        """Регистрирует UI и гарантирует приоритет кнопки «Настройки» в карточке."""
        global _tg_ui_registered_for
        if not self.cardinal.telegram:
            self.log("Telegram недоступен — UI не зарегистрирован")
            return
        bot = self.cardinal.telegram.bot
        bot_id = id(bot)
        if _tg_ui_registered_for != bot_id:
            self.setup_telegram()
            _tg_ui_registered_for = bot_id
        _ensure_gemini_settings_handler(bot, _gemini_link_open_settings)

    def log(self, msg: str, *args) -> None:
        logger.info("%s " + msg, _P, *args)

    def reload_settings(self) -> None:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        defaults = self._default_cfg()
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in defaults.items():
                    loaded.setdefault(k, v)
                sm = str(loaded.get("stock_mode", "auto_buy"))
                if sm in _LEGACY_STOCK_MODES:
                    loaded["stock_mode"] = "gemini_links" if sm in ("stocked", "warehouse", "import_lot") else "auto_buy"
                if str(loaded.get("_cfg_version", "0")) < "3.2.2":
                    loaded.setdefault("confirm_reminder_enabled", True)
                    loaded.setdefault("confirm_reminder_delay_sec", CONFIRM_REMINDER_DELAY)
                    loaded.setdefault("confirm_reminder_text", "")
                    loaded["_cfg_version"] = "3.2.2"
                if str(loaded.get("_cfg_version", "0")) < "3.2.0":
                    loaded.setdefault("gemini_auto_enabled", True)
                    loaded.setdefault("last_delivery_error", "")
                    loaded["_cfg_version"] = "3.2.0"
                if str(loaded.get("_cfg_version", "0")) < "3.0.0":
                    loaded.setdefault("gemini_lot_match", DEFAULT_GEMINI_LOT_MATCH)
                    loaded.setdefault("gemini_delivery_parts", DEFAULT_DELIVERY_PARTS)
                    loaded.setdefault("gemini_redelivery_parts", DEFAULT_REDELIVERY_PARTS)
                    if loaded.get("stock_mode") == "stocked":
                        loaded["stock_mode"] = "gemini_links"
                    loaded["_cfg_version"] = "3.0.0"
                if str(loaded.get("_cfg_version", "0")) < "2.3.0":
                    loaded.setdefault("buy_budget_usd", 0.0)
                    loaded.setdefault("reserve_balance_usd", 10.0)
                    if not str(loaded.get("supplier_api_url", "")).strip():
                        loaded["supplier_api_url"] = DEFAULT_API_URL
                    loaded["autobuy_lot_match"] = DEFAULT_LOT_MATCH
                    loaded["_cfg_version"] = "2.3.0"
                if str(loaded.get("_cfg_version", "0")) < "2.4.0":
                    loaded.setdefault("auto_enabled", True)
                    loaded.setdefault("min_lot_stock", DEFAULT_MIN_LOT_STOCK)
                    loaded.setdefault("auto_interval_sec", DEFAULT_AUTO_INTERVAL)
                    loaded.setdefault("last_auto_at", "")
                    if loaded.get("stock_mode") == "stocked" and not loaded.get("supplier_api_key"):
                        loaded["stock_mode"] = "auto_buy"
                    loaded["_cfg_version"] = "2.4.0"
                if str(loaded.get("_cfg_version", "0")) < "2.5.0":
                    loaded["_cfg_version"] = "2.5.0"
                self._cfg = loaded
            else:
                self._cfg = defaults
                self._save_settings()
        except Exception as exc:
            logger.error("%s settings load: %s", _P, exc)
            self._cfg = defaults

    def _save_settings_dict(self, cfg: dict[str, Any]) -> None:
        with self._lock:
            tmp = f"{SETTINGS_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)

    def _save_settings(self) -> None:
        self._save_settings_dict(self._cfg)

    def get_cfg(self, key: str, default: Any = None) -> Any:
        field = self.get_schema_field(key)
        if default is None and field:
            default = field.get("default")
        return self._cfg.get(key, default)

    def set_cfg(self, key: str, value: Any) -> None:
        self._cfg[key] = value
        self._save_settings()
        self.on_setting_change(key, value)

    def on_setting_change(self, key: str, value: Any) -> None:
        if key in ("supplier_api_key", "supplier_api_url", "buy_budget_usd", "reserve_balance_usd"):
            self._invalidate_product_cache()
        if key in ("autobuy_lot_id", "autobuy_lot_match"):
            self._invalidate_lot_cache()
        if key in ("gemini_lot_match", "gemini_lot_id"):
            self._gemini_lots_cache = []
            self._gemini_lots_cache_ts = 0.0
        if key == "stock_mode":
            self._gemini_lots_cache = []
            self._gemini_lots_cache_ts = 0.0

    @staticmethod
    def _default_cfg() -> dict[str, Any]:
        return {
            "supplier_api_url": DEFAULT_API_URL,
            "supplier_api_key": "",
            "buy_budget_usd": 0.0,
            "reserve_balance_usd": 10.0,
            "autobuy_lot_match": DEFAULT_LOT_MATCH,
            "autobuy_lot_id": "",
            "auto_enabled": True,
            "min_lot_stock": DEFAULT_MIN_LOT_STOCK,
            "auto_interval_sec": DEFAULT_AUTO_INTERVAL,
            "last_auto_at": "",
            "import_skip_duplicates": True,
            "imported_order_ids": [],
            "stock_mode": "auto_buy",
            "warehouse_release_qty": 5,
            "gemini_lot_match": DEFAULT_GEMINI_LOT_MATCH,
            "gemini_lot_id": "",
            "gemini_delivery_parts": DEFAULT_DELIVERY_PARTS,
            "gemini_redelivery_parts": DEFAULT_REDELIVERY_PARTS,
            "gemini_auto_enabled": True,
            "confirm_reminder_enabled": True,
            "confirm_reminder_delay_sec": CONFIRM_REMINDER_DELAY,
            "confirm_reminder_text": "",
            "last_delivery_error": "",
            "_cfg_version": "3.2.2",
        }

    # ── UI: компактные страницы вместо длинного списка кнопок ───────────────

    @staticmethod
    def _ui_pages() -> dict[str, dict[str, Any]]:
        return {
            "hub": {"title": "🎛 Главная", "emoji": "🏠"},
            "settings": {"title": "⚙️ Настройки", "emoji": "⚙️"},
        }

    def _settings_fields(self) -> list[dict[str, Any]]:
        return [
            {"key": "supplier_api_key", "label": "API-ключ (X-API-Key)", "type": "text"},
            {"key": "buy_budget_usd", "label": "Потратить за закупку ($)", "type": "float", "min": 0, "max": 100000},
            {"key": "reserve_balance_usd", "label": "Оставить на балансе ($)", "type": "float", "min": 0, "max": 100000},
            {"key": "min_lot_stock", "label": "Мин. аккаунтов на лоте", "type": "int", "min": 0, "max": 500},
            {"key": "auto_interval_sec", "label": "Проверка авто (сек)", "type": "int", "min": 60, "max": 3600},
            {"key": "autobuy_lot_id", "label": "ID лота (необяз.)", "type": "text"},
            {"key": "gemini_lot_match", "label": "Метка лота Gemini", "type": "text"},
            {"key": "gemini_lot_id", "label": "ID лота Gemini (необяз.)", "type": "text"},
            {"key": "gemini_delivery_parts", "label": "Частей при выдаче", "type": "int", "min": 2, "max": 6},
            {"key": "gemini_redelivery_parts", "label": "Частей при перевыдаче", "type": "int", "min": 2, "max": 8},
            {"key": "warehouse_release_qty", "label": "Выложить со склада (шт.)", "type": "int", "min": 0, "max": 100},
        ]

    def get_settings_schema(self) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        for f in self._settings_fields():
            fields.append({**f, "default": self._default_cfg().get(f["key"])})
        fields.extend([
            {"key": "run_autobuy", "label": "🛒 Закупить", "type": "action"},
            {"key": "shop_balance", "label": "💰 Баланс API", "type": "action"},
            {"key": "start_import", "label": "📥 Загрузить закупку", "type": "action"},
            {"key": "release_warehouse", "label": "📤 Со склада", "type": "action"},
            {"key": "stock_status", "label": "📊 Склад", "type": "action"},
        ])
        return fields

    def _prompt_edit_intro(self, field: dict[str, Any], cur: str) -> str:
        label = _escape(field.get("label", field.get("key", "")))
        total = len(cur or "")
        preview = _escape(str(cur or "")[:PROMPT_PREVIEW_LEN])
        if total > PROMPT_PREVIEW_LEN:
            preview += "…"
        return (
            f"✏️ <b>{label}</b>\n\n"
            f"📏 Сохранено: <b>{total}</b> / {MAX_PROMPT_LEN} символов\n"
            f"👁 Превью:\n<code>{preview or '—'}</code>\n\n"
            f"Отправьте новый текст одним сообщением (до {MAX_PROMPT_LEN} симв.).\n"
            f"<code>/cancel</code> — отмена"
        )

    def get_schema_field(self, key: str) -> dict[str, Any] | None:
        for field in self.get_settings_schema():
            if field.get("key") == key:
                return field
        for field in self._settings_fields():
            if field.get("key") == key:
                return field
        return None

    def _schema_field_by_index(self, idx: int) -> dict[str, Any] | None:
        schema = self.get_settings_schema()
        if 0 <= idx < len(schema):
            return schema[idx]
        return None

    def _field_by_page_index(self, page: str, idx: int) -> dict[str, Any] | None:
        fields = self._fields_for_page(page)
        if 0 <= idx < len(fields):
            return fields[idx]
        return None

    def _shop_client(self) -> tuple[str, str]:
        url = str(self.get_cfg("supplier_api_url", DEFAULT_API_URL)).strip().rstrip("/")
        key = str(self.get_cfg("supplier_api_key", "")).strip()
        return url or DEFAULT_API_URL, key

    def _invalidate_product_cache(self) -> None:
        self._product_cache = None
        self._product_cache_ts = 0.0
        self._buy_plan_cache = None
        self._buy_plan_cache_ts = 0.0

    def _get_shop_product(self, timeout: float = BUY_API_TIMEOUT) -> tuple[dict[str, Any] | None, str]:
        if self._product_cache and (time.time() - self._product_cache_ts) < PRODUCT_CACHE_TTL:
            return self._product_cache, ""
        api_url, api_key = self._shop_client()
        if not api_key:
            return None, "Не задан API-ключ"
        products, err = _shop_get_products(api_url, api_key, timeout=timeout)
        if err:
            return None, f"Товары: {err}"
        product = _shop_find_product(products, SHOP_PRODUCT_NAME)
        if not product:
            return None, f"Товар с меткой «{SHOP_PRODUCT_NAME}» не найден в магазине"
        if not _product_matches_name(product, SHOP_PRODUCT_NAME):
            return None, (
                f"Товар магазина «{_product_display_name(product)}» "
                f"не содержит «{SHOP_PRODUCT_NAME}»"
            )
        self._product_cache = product
        self._product_cache_ts = time.time()
        return product, ""

    def _float_cfg(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get_cfg(key, default))
        except (TypeError, ValueError):
            return default

    def calc_buy_plan(
        self,
        max_qty: int | None = None,
        *,
        fast: bool = False,
        instant: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if instant:
            return {"ok": False, "instant": True}

        if fast:
            if isinstance(self._buy_plan_cache, dict) and (time.time() - self._buy_plan_cache_ts) < 60:
                return self._buy_plan_cache
            api_key = str(self.get_cfg("supplier_api_key", "") or "").strip()
            if not api_key:
                return {"ok": False, "error": "Не задан API-ключ. Настройки → API-ключ."}
            return {"ok": False, "error": "обновляется…"}

        req_timeout = timeout if timeout is not None else UI_API_TIMEOUT
        api_url, api_key = self._shop_client()
        if not api_key:
            return {"ok": False, "error": "Не задан API-ключ. Настройки → API-ключ."}
        balance, err = _shop_get_balance(api_url, api_key, timeout=req_timeout)
        if err:
            return {"ok": False, "error": f"Баланс: {err}"}
        reserve = max(0.0, self._float_cfg("reserve_balance_usd", 10.0))
        budget_set = max(0.0, self._float_cfg("buy_budget_usd", 0.0))
        available = round(balance - reserve, 2)
        if available <= 0:
            return {
                "ok": False,
                "error": f"Баланс ${balance:.2f}, резерв ${reserve:.2f} — нечем покупать",
                "balance": balance, "reserve": reserve,
            }
        spend_cap = min(budget_set, available) if budget_set > 0 else available
        product, err = self._get_shop_product(timeout=req_timeout)
        if err or not product:
            return {"ok": False, "error": err or "товар не найден", "balance": balance, "reserve": reserve}
        try:
            price = float(product.get("price", 0))
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            return {"ok": False, "error": "Цена товара неизвестна", "balance": balance, "reserve": reserve}
        qty = int(spend_cap / price)
        if max_qty is not None and max_qty > 0:
            qty = min(qty, int(max_qty))
        if qty < 1:
            return {
                "ok": False,
                "error": f"Минимум ${price:.2f} за шт., доступно ${available:.2f}",
                "balance": balance, "reserve": reserve, "price": price,
            }
        spend = round(qty * price, 2)
        if spend > available + 0.01:
            return {"ok": False, "error": "Сумма превышает баланс после резерва", "balance": balance, "reserve": reserve}
        if budget_set > 0 and spend > budget_set + 0.01:
            return {"ok": False, "error": "Сумма превышает заданный бюджет", "balance": balance, "reserve": reserve}
        product_name = _product_display_name(product) or SHOP_PRODUCT_NAME
        result = {
            "ok": True,
            "qty": qty,
            "spend": spend,
            "balance": balance,
            "reserve": reserve,
            "available": available,
            "price": price,
            "product_id": int(product["id"]),
            "product_name": product_name,
            "shop_stock": product.get("stock_count"),
        }
        self._buy_plan_cache = result
        self._buy_plan_cache_ts = time.time()
        return result

    def _validate_buy_plan(self, plan: dict[str, Any]) -> tuple[bool, str]:
        if not plan.get("ok"):
            return False, str(plan.get("error", "закупка недоступна"))
        pname = str(plan.get("product_name", "")).strip()
        if SHOP_PRODUCT_NAME.casefold() not in pname.casefold():
            return False, f"Товар «{pname}» не содержит «{SHOP_PRODUCT_NAME}»"
        spend = float(plan.get("spend", 0))
        reserve = max(0.0, self._float_cfg("reserve_balance_usd", 10.0))
        balance = float(plan.get("balance", 0))
        if spend > round(balance - reserve, 2) + 0.01:
            return False, f"Закупка ${spend:.2f} нарушает резерв ${reserve:.2f}"
        budget = max(0.0, self._float_cfg("buy_budget_usd", 0.0))
        if budget > 0 and spend > budget + 0.01:
            return False, f"Закупка ${spend:.2f} превышает бюджет ${budget:.2f}"
        return True, ""

    def _lot_detailed_description(self, lot_id: int) -> str:
        """Подробное описание лота (fields[desc]), не краткое название."""
        chunks: list[str] = []
        try:
            lf = self.cardinal.account.get_lot_fields(int(lot_id))
            for attr in ("description_ru", "description_en"):
                val = getattr(lf, attr, None)
                if val:
                    chunks.append(str(val))
            raw = getattr(lf, "fields", None)
            if isinstance(raw, dict):
                for key, val in raw.items():
                    if not val:
                        continue
                    key_l = str(key).casefold()
                    if "[desc]" in key_l or key_l.endswith("desc][ru]") or key_l.endswith("desc][en]"):
                        chunks.append(str(val))
        except Exception as exc:
            logger.debug("%s lot desc #%s: %s", _P, lot_id, exc)
        return "\n".join(chunks)

    def _ensure_profile(self):
        profile = self.cardinal.profile
        if profile and profile.get_lots():
            return profile
        try:
            profile = self.cardinal.account.get_user(self.cardinal.account.id)
            return profile
        except Exception as exc:
            self.log("не удалось обновить профиль FunPay: %s", exc)
            return self.cardinal.profile

    def _iter_profile_lot_ids(self) -> list[int]:
        ids: list[int] = []
        seen: set[int] = set()
        profile = self._ensure_profile()
        if profile:
            for lot in profile.get_lots():
                try:
                    lot_id = int(lot.id)
                except (TypeError, ValueError):
                    continue
                if lot_id not in seen:
                    seen.add(lot_id)
                    ids.append(lot_id)
        for raw_id in getattr(self.cardinal, "lots_ids", []) or []:
            try:
                lot_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if lot_id not in seen:
                seen.add(lot_id)
                ids.append(lot_id)
        return ids

    def _find_lots_by_detailed_desc(self, needle: str, *, fast: bool = False) -> list[int]:
        needle = (needle or "").strip()
        if not needle or fast:
            return []
        lot_ids = self._iter_profile_lot_ids()
        if not lot_ids:
            self.log("в профиле FunPay нет лотов для поиска %r", needle)
            return []
        matched: list[int] = []
        for lot_id in lot_ids:
            detail = self._lot_detailed_description(lot_id)
            if _lot_text_matches(detail, needle):
                matched.append(lot_id)
                self.log("лот #%s найден по метке %r в подробном описании", lot_id, needle)
        if not matched:
            self.log(
                "метка %r не найдена в подробном описании ни одного из %s лотов",
                needle, len(lot_ids),
            )
        return matched

    def _lot_stock_count(self, lot_id: int) -> int:
        try:
            lf = self.cardinal.account.get_lot_fields(int(lot_id))
            return len(lf.secrets or [])
        except Exception:
            return 0

    def _invalidate_lot_cache(self) -> None:
        self._cached_lot_id = None
        self._cached_lot_ts = 0.0
        self._lot_stock_cache = ""
        self._lot_stock_cache_ts = 0.0

    def _resolve_autobuy_lot_ids(self, silent: bool = False, fast: bool = False) -> list[int]:
        lot_id_raw = str(self.get_cfg("autobuy_lot_id", "")).strip()
        needle = str(self.get_cfg("autobuy_lot_match", DEFAULT_LOT_MATCH)).strip()

        if lot_id_raw.isdigit():
            lot_id = int(lot_id_raw)
            if needle:
                detail = self._lot_detailed_description(lot_id)
                if _lot_text_matches(detail, needle):
                    self._cached_lot_id = lot_id
                    self._cached_lot_ts = time.time()
                    return [lot_id]
                if not silent:
                    self.log(
                        "сохранённый лот #%s не содержит %r в подробном описании — ищу заново",
                        lot_id, needle,
                    )
                if str(self._cfg.get("autobuy_lot_id", "")).strip() == lot_id_raw:
                    self._cfg["autobuy_lot_id"] = ""
                    self._save_settings()
                self._invalidate_lot_cache()
            else:
                self._cached_lot_id = lot_id
                self._cached_lot_ts = time.time()
                return [lot_id]

        if self._cached_lot_id and (time.time() - self._cached_lot_ts) < LOT_CACHE_TTL:
            if not needle:
                return [self._cached_lot_id]
            detail = self._lot_detailed_description(self._cached_lot_id)
            if _lot_text_matches(detail, needle):
                return [self._cached_lot_id]
            self._invalidate_lot_cache()

        if not needle:
            return []
        matched = self._find_lots_by_detailed_desc(needle, fast=fast)
        if not matched and not silent:
            self.log("лот с меткой %r в подробном описании не найден", needle)
        if len(matched) > 1 and not silent:
            self.log("несколько лотов для %r, берём #%s", needle, matched[0])
        result = matched[:1]
        if result:
            self._cached_lot_id = result[0]
            self._cached_lot_ts = time.time()
            saved = str(self.get_cfg("autobuy_lot_id", "")).strip()
            if not saved:
                self._cfg["autobuy_lot_id"] = str(result[0])
                self._save_settings()
        return result

    def _format_account_lines(self, accounts: list[str]) -> list[str]:
        lines: list[str] = []
        for acc in accounts:
            line = _normalize_account_line(acc)
            if line and line not in lines:
                lines.append(line)
        return lines

    def _append_accounts_to_lot(self, lot_id: int, accounts: list[str]) -> tuple[bool, str]:
        lines = self._format_account_lines(accounts)
        return self._append_stock_lines_to_lot(lot_id, lines)

    def _append_stock_lines_to_lot(self, lot_id: int, lines: list[str]) -> tuple[bool, str]:
        lines = [ln.strip() for ln in self._format_account_lines(lines) if ln and ln.strip()]
        if not lines:
            return False, "нет данных для выдачи"
        acc = self.cardinal.account
        lot_id = int(lot_id)
        last_err = ""
        for attempt in range(5):
            try:
                lf = acc.get_lot_fields(lot_id)
                before = len(lf.secrets or [])
                existing = set(lf.secrets or [])
                pending = [ln for ln in lines if ln not in existing]
                if not pending:
                    lf.auto_delivery = True
                    lf.active = True
                    lf.renew_fields()
                    acc.save_lot(lf)
                    return True, f"лот #{lot_id}: все {len(lines)} акк. уже на месте (🟢 активен + АВ)"
                for line in pending:
                    lf.secrets.append(line)
                lf.auto_delivery = True
                lf.active = True
                lf.renew_fields()
                acc.save_lot(lf)
                time.sleep(0.8)
                lf_check = acc.get_lot_fields(lot_id)
                secrets_set = set(lf_check.secrets or [])
                missing = [ln for ln in lines if ln not in secrets_set]
                added = len(lf_check.secrets or []) - before
                if not missing and lf_check.active and lf_check.auto_delivery:
                    return True, (
                        f"лот #{lot_id}: +{added} шт., всего {len(lf_check.secrets)} "
                        f"(🟢 активен + АВ)"
                    )
                last_err = (
                    f"лот #{lot_id}: загружено {added}/{len(pending)}, "
                    f"не хватает {len(missing)}, АВ={lf_check.auto_delivery}, "
                    f"активен={lf_check.active}"
                )
                self.log("save_lot verify #%s attempt %s: %s", lot_id, attempt + 1, last_err)
            except Exception as exc:
                last_err = str(exc)
                self.log("save_lot #%s attempt %s: %s", lot_id, attempt + 1, exc)
            time.sleep(1.2 * (attempt + 1))
        return False, last_err or f"не удалось сохранить лот #{lot_id}"

    def _purchase_accounts(self, product_id: int, quantity: int) -> tuple[list[str], str]:
        api_url, api_key = self._shop_client()
        accounts, info, _ = _shop_buy(api_url, api_key, product_id, quantity)
        return accounts, info

    def _log_autobuy(self, entry: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(AUTOBUY_LOG_FILE), exist_ok=True)
        history: list[Any] = []
        try:
            if os.path.exists(AUTOBUY_LOG_FILE):
                with open(AUTOBUY_LOG_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
        except Exception:
            history = []
        history.append(entry)
        history = history[-50:]
        with open(AUTOBUY_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    def run_autobuy(
        self,
        notify_chat_id: int | None = None,
        max_qty: int | None = None,
        auto: bool = False,
    ) -> None:
        if self._autobuy_running:
            return
        plan = self.calc_buy_plan(max_qty=max_qty, timeout=BUY_API_TIMEOUT)
        ok, reason = self._validate_buy_plan(plan)
        if not ok:
            if not auto:
                bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, f"⛔ {_escape(reason)}", parse_mode="HTML")
            self.log("autobuy blocked: %s", reason)
            return
        lot_ids = self._resolve_autobuy_lot_ids(silent=auto)
        if not lot_ids:
            if not auto:
                bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
                msg = (
                    f"⚠️ Лот не найден. В <b>подробном описании</b> лота укажите:\n"
                    f"<code>{_escape(DEFAULT_LOT_MATCH)}</code>"
                )
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, msg, parse_mode="HTML")
            else:
                self.log("auto: лот с %r не найден", DEFAULT_LOT_MATCH)
            return
        self._autobuy_running = True
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        qty = int(plan["qty"])
        spend = float(plan["spend"])
        product_id = int(plan["product_id"])
        product_name = str(plan["product_name"])
        lot_id = lot_ids[0]
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        try:
            tag = "авто" if auto else "ручная"
            self.log(
                "%s закупка: %s × «%s» ($%.2f) → лот #%s",
                tag, qty, product_name, spend, lot_id,
            )
            if bot and notify_chat_id:
                bot.send_message(
                    notify_chat_id,
                    f"🛒 <b>{_escape(product_name)}</b>\n"
                    f"💵 <b>${spend:.2f}</b> → <b>{qty}</b> акк.\n"
                    f"📦 Лот <b>#{lot_id}</b> | формат <code>почта|пароль|2FA</code>\n"
                    f"⏳ Покупаю…",
                    parse_mode="HTML",
                )
            accounts, buy_info = self._purchase_accounts(product_id, qty)
            if not accounts:
                msg = f"❌ <b>Закупка не удалась</b>\n<code>{_escape(buy_info)}</code>"
                self.log("autobuy fail: %s", buy_info)
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, msg, parse_mode="HTML")
                self._log_autobuy({"time": started, "ok": False, "error": buy_info, "qty": qty, "auto": auto})
                return
            if len(accounts) < qty:
                self.log(
                    "autobuy partial API: запрошено %s, получено %s — выкладываю полученное",
                    qty, len(accounts),
                )

            success, info = self._append_accounts_to_lot(lot_id, accounts)
            if not success:
                self.log("autobuy upload fail: %s", info)
            summary = (
                f"{'✅' if success else '⚠️'} <b>Готово за {int(time.time() - t0)}с</b>\n"
                f"🛒 <b>{len(accounts)}</b> акк. — {buy_info}\n"
                f"📦 {_escape(info)}\n"
                f"<i>FunPay выдаст покупателю столько акк., сколько штук в заказе (1→1, 2→2).</i>"
            )
            self.log("autobuy ok: %s accounts -> #%s (%s)", len(accounts), lot_id, info)
            if bot and notify_chat_id:
                bot.send_message(notify_chat_id, summary, parse_mode="HTML")
            self._log_autobuy({
                "time": started, "ok": success, "bought": len(accounts),
                "spend": spend, "lots": lot_ids, "results": [info], "auto": auto,
                "product": product_name,
            })
            self._cfg["last_auto_at"] = started
            self._save_settings()
            self._invalidate_product_cache()
            self._invalidate_lot_cache()
        except Exception as exc:
            logger.error("%s autobuy: %s", _P, exc)
            logger.debug(traceback.format_exc())
            if bot and notify_chat_id:
                bot.send_message(
                    notify_chat_id,
                    f"❌ Ошибка закупки: <code>{_escape(exc)}</code>",
                    parse_mode="HTML",
                )
        finally:
            self._autobuy_running = False

    def _auto_tick(self) -> None:
        if not bool(self.get_cfg("auto_enabled", True)):
            return
        if self.stock_mode() != "auto_buy":
            return
        if self._autobuy_running:
            return
        if not self._shop_client()[1]:
            return
        lot_ids = self._resolve_autobuy_lot_ids(silent=True)
        if not lot_ids:
            return
        lot_id = lot_ids[0]
        current = self._lot_stock_count(lot_id)
        min_stock = max(0, int(self.get_cfg("min_lot_stock", DEFAULT_MIN_LOT_STOCK)))
        if current >= min_stock:
            return
        needed = max(1, min_stock - current)
        plan = self.calc_buy_plan(max_qty=needed, timeout=BUY_API_TIMEOUT)
        ok, reason = self._validate_buy_plan(plan)
        if not ok:
            self.log("auto: %s", reason)
            return
        self.log("auto: лот #%s — %s/%s акк., докупаю %s", lot_id, current, min_stock, plan["qty"])
        self.run_autobuy(notify_chat_id=None, max_qty=needed, auto=True)

    def _auto_worker_loop(self) -> None:
        self.log("фоновая автозакупка запущена")
        time.sleep(15)
        while not self._stop_auto:
            try:
                self._auto_tick()
            except Exception as exc:
                logger.error("%s auto worker: %s", _P, exc)
                logger.debug(traceback.format_exc())
            try:
                interval = max(60, int(self.get_cfg("auto_interval_sec", DEFAULT_AUTO_INTERVAL)))
            except (TypeError, ValueError):
                interval = DEFAULT_AUTO_INTERVAL
            for _ in range(interval):
                if self._stop_auto:
                    break
                time.sleep(1)
        self.log("фоновая автозакупка остановлена")

    def start_auto_worker(self) -> None:
        if self._auto_thread and self._auto_thread.is_alive():
            return
        self._stop_auto = False
        self._auto_thread = threading.Thread(
            target=self._auto_worker_loop, daemon=True, name="GeminiLinkAuto",
        )
        self._auto_thread.start()

    def notify_shop_balance(self, chat_id: int) -> None:
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        if not bot:
            return
        plan = self.calc_buy_plan(timeout=UI_API_TIMEOUT)
        lines = [f"💰 <b>Баланс магазина</b>", f"📦 Товар: <code>{_escape(SHOP_PRODUCT_NAME)}</code>"]
        if "balance" in plan:
            lines.append(f"💵 Баланс: <b>${float(plan['balance']):.2f}</b>")
            lines.append(f"🔒 Резерв: <b>${float(plan.get('reserve', self._float_cfg('reserve_balance_usd'))):.2f}</b>")
            budget = self._float_cfg("buy_budget_usd")
            if budget > 0:
                lines.append(f"🎯 Бюджет закупки: <b>${budget:.2f}</b>")
            else:
                lines.append("🎯 Бюджет: <i>всё доступное после резерва</i>")
        if plan.get("ok"):
            lines.append(
                f"✅ Можно купить: <b>{plan['qty']}</b> шт. × ${float(plan['price']):.2f} "
                f"= <b>${float(plan['spend']):.2f}</b>"
            )
            if plan.get("shop_stock") is not None:
                lines.append(f"🏪 В магазине: <b>{plan['shop_stock']}</b> шт.")
        else:
            lines.append(f"⚠️ {_escape(str(plan.get('error', '—')))}")
        bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")

    def notify_stock_status(self, chat_id: int) -> None:
        if self.stock_mode() == "gemini_links":
            self.notify_gemini_status(chat_id)
            return
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        if not bot:
            return
        try:
            lot_ids = self._resolve_autobuy_lot_ids()
            match = str(self.get_cfg("autobuy_lot_match", DEFAULT_LOT_MATCH))
            lines = [
                f"📊 <b>Склад</b> — <code>{_escape(match)}</code>",
                f"📦 <b>На лоте FunPay:</b> {self._lot_stock_info()}",
                f"🗄 <b>Купленные (склад плагина):</b> <b>{self.warehouse_count()}</b> шт.",
                f"<b>Режим:</b> {self.stock_mode_label()}",
            ]
            if not lot_ids:
                lines.append("\n⚠️ Лот не найден — задайте <b>ID лота</b> или метку в описании.")
            for lot_id in lot_ids:
                lf = self.cardinal.account.get_lot_fields(int(lot_id))
                active = "🟢 активен" if lf.active else "🔴 выключен"
                av = "🟢 АВ" if lf.auto_delivery else "🔴 без АВ"
                lines.append(
                    f"• Лот <b>#{lot_id}</b>: <b>{len(lf.secrets)}</b> акк. | {active} | {av}"
                )
            bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        except Exception as exc:
            bot.send_message(chat_id, f"❌ { _escape(exc)}", parse_mode="HTML")

    def _resolve_lot_by_match(self, needle: str, silent: bool = True) -> list[int]:
        needle = (needle or "").strip()
        if not needle:
            return []
        if needle.startswith("#") and needle[1:].isdigit():
            return [int(needle[1:])]
        matched = self._find_lots_by_detailed_desc(needle)
        if len(matched) > 1 and not silent:
            self.log("несколько лотов для %r, берём первый: %s", needle, matched[0])
        return matched[:1]

    def _resolve_lot_for_product(self, product: str) -> list[int]:
        return self._resolve_autobuy_lot_ids(silent=True)

    def _format_import_lines(self, items: list[dict[str, str]]) -> list[str]:
        lines: list[str] = []
        for item in items:
            url = str(item.get("url", "")).strip()
            if url and url not in lines:
                lines.append(url)
        return lines

    def _filter_new_import_items(self, items: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
        if not bool(self.get_cfg("import_skip_duplicates", True)):
            return items, 0
        known = {str(x) for x in self.get_cfg("imported_order_ids", [])}
        fresh: list[dict[str, str]] = []
        skipped = 0
        for item in items:
            oid = str(item.get("order_id") or "").strip()
            if oid and oid in known:
                skipped += 1
                continue
            fresh.append(item)
        return fresh, skipped

    def _mark_imported_orders(self, items: list[dict[str, str]]) -> None:
        known = {str(x) for x in self.get_cfg("imported_order_ids", [])}
        for item in items:
            oid = str(item.get("order_id") or "").strip()
            if oid:
                known.add(oid)
        trimmed = sorted(known)[-500:]
        self._cfg["imported_order_ids"] = trimmed
        self._save_settings()

    def _preview_import_message(
        self, items: list[dict[str, str]], skipped: int = 0, target: str = "lot",
    ) -> str:
        by_product: dict[str, int] = {}
        for item in items:
            prod = item.get("product") or "без названия"
            by_product[prod] = by_product.get(prod, 0) + 1
        dest = "в <b>архив Gemini</b>" if target == "gemini_archive" else (
            "на <b>склад</b>" if target == "warehouse" else "на <b>лот FunPay</b>"
        )
        lines = [f"📥 <b>К выкладке {dest}:</b> <b>{len(items)}</b> ссылок"]
        if skipped:
            lines.append(f"⏭ Пропущено дублей заказов: <b>{skipped}</b>")
        if target == "lot":
            for prod, cnt in sorted(by_product.items(), key=lambda x: -x[1])[:8]:
                lot_ids = self._resolve_lot_for_product(prod)
                lot_hint = f"→ лот #{lot_ids[0]}" if lot_ids else "→ лот не найден"
                lines.append(f"• <code>{_escape(prod[:55])}</code>: {cnt} шт. {lot_hint}")
        else:
            lines.append(f"📦 Сейчас на складе: <b>{self.warehouse_count()}</b> шт.")
        if items:
            sample = items[0].get("url", "")
            if len(sample) > 90:
                sample = sample[:87] + "…"
            lines.append(f"\n👁 Пример:\n<code>{_escape(sample)}</code>")
        if target == "gemini_archive":
            btn = "в архив"
        elif target == "warehouse":
            btn = "на склад"
        else:
            btn = "на FunPay"
        lines.append(f"\n<b>Подтвердить выкладку {btn}?</b>")
        return "\n".join(lines)

    def _import_confirm_keyboard(self, target: str = "lot") -> IKM:
        kb = IKM()
        label = "✅ В архив" if target == "gemini_archive" else (
            "✅ На склад" if target == "warehouse" else "✅ На FunPay"
        )
        kb.row(
            IKB(label, callback_data=f"{CB_PREFIX}:import:confirm:{target}"),
            IKB("❌ Отмена", callback_data=f"{CB_PREFIX}:import:cancel"),
        )
        return kb

    def start_import_mode(self, chat_id: int, user_id: int, target: str = "lot") -> str:
        valid = ("lot", "warehouse", "gemini_archive")
        target = target if target in valid else "lot"
        self._import_buffers[(chat_id, user_id)] = ""
        self._import_target[chat_id] = target
        if target == "gemini_archive":
            dest = (
                "Ссылки попадут в <b>архив Gemini</b>.\n"
                "При оплате лота с меткой <code>gemini link</code> в подробном описании "
                "плагин выдаст ссылку покупателю частями."
            )
        elif target == "warehouse":
            dest = (
                "Ссылки попадут на <b>локальный склад</b> (уже купленные).\n"
                "Потом выложите на FunPay режимом <b>📦 Со склада → лот</b>."
            )
        else:
            dest = "Ссылки сразу выложатся на <b>автовыдачу лота FunPay</b>."
        return (
            "📥 <b>Загрузка ссылок</b>\n\n"
            f"{dest}\n\n"
            "Вставьте ссылки <code>one.google.com/activate-plan/…</code> "
            "(по одной на строку) или <b>Purchase History</b>.\n"
            "Можно файлом <code>.txt</code>.\n\n"
            "<code>/done</code> — разобрать | <code>/cancel</code> — отмена"
        )

    def _accumulate_import_text(self, chat_id: int, user_id: int, chunk: str) -> int:
        key = (chat_id, user_id)
        buf = self._import_buffers.get(key, "") + (chunk or "")
        self._import_buffers[key] = buf
        return len(buf)

    def process_import_buffer(self, chat_id: int, user_id: int, skipped_dup: int = 0) -> None:
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        if not bot:
            return
        key = (chat_id, user_id)
        raw = self._import_buffers.get(key, "")
        target = self._import_target.get(chat_id, "lot")
        all_items = parse_shop_purchase_history(raw)
        if not all_items:
            urls = extract_activation_urls(raw)
            all_items = [{"order_id": "", "product": "", "url": u} for u in urls]
        if target == "gemini_archive":
            fresh = [it for it in all_items if str(it.get("url", "")).strip()]
            skipped = len(all_items) - len(fresh)
        else:
            fresh, skipped = self._filter_new_import_items(all_items)
        skipped += skipped_dup
        self._import_buffers.pop(key, None)
        if not fresh:
            hint = (
                "Вставьте ссылки <code>one.google.com/activate-plan/…</code> "
                "(по одной на строку)"
                if target == "gemini_archive"
                else "Проверьте формат: блоки <code>Order #…</code> и строка <code>Data: https://…</code>"
            )
            bot.send_message(
                chat_id,
                f"❌ Ссылки не найдены"
                f"{f' (дублей: {skipped})' if skipped else ''}.\n"
                f"{hint}",
                parse_mode="HTML",
            )
            return
        self._import_pending[chat_id] = fresh
        bot.send_message(
            chat_id,
            self._preview_import_message(fresh, skipped, target),
            parse_mode="HTML",
            reply_markup=self._import_confirm_keyboard(target),
        )

    def confirm_pending_import(self, chat_id: int, target: str | None = None) -> None:
        if self._import_running:
            return
        self._import_running = True
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        items = list(self._import_pending.pop(chat_id, []) or [])
        target = target or self._import_target.pop(chat_id, "lot")
        try:
            if not items:
                if bot:
                    bot.send_message(chat_id, "❌ Нет данных. Загрузите закупку заново.")
                return

            if target == "gemini_archive":
                urls = [str(it.get("url", "")) for it in items]
                added, skipped_dup = self.add_urls_to_gemini_archive(urls)
                self._mark_imported_orders(items)
                msg = (
                    f"✅ <b>В архив Gemini:</b> +<b>{added}</b> ссылок"
                    f"{f' (дублей: {skipped_dup})' if skipped_dup else ''}\n"
                    f"🗄 Всего в архиве: <b>{self.gemini_archive_count()}</b>\n"
                    f"📋 Лотов с меткой: <b>{self.count_gemini_link_lots()}</b>"
                )
                if bot:
                    bot.send_message(chat_id, msg, parse_mode="HTML")
                return

            if target == "warehouse":
                added, skipped = self.add_items_to_warehouse(items)
                self._mark_imported_orders(items)
                msg = (
                    f"✅ <b>На склад:</b> +<b>{added}</b> ссылок"
                    f"{f' (дублей: {skipped})' if skipped else ''}\n"
                    f"📦 Всего на складе: <b>{self.warehouse_count()}</b>\n\n"
                    f"Выложите на FunPay: режим <b>📦 Со склада → лот</b>"
                )
                if bot:
                    bot.send_message(chat_id, msg, parse_mode="HTML")
                return

            grouped: dict[int, list[dict[str, str]]] = {}
            unresolved: list[dict[str, str]] = []
            for item in items:
                lot_ids = self._resolve_lot_for_product(item.get("product", ""))
                if not lot_ids:
                    unresolved.append(item)
                    continue
                grouped.setdefault(lot_ids[0], []).append(item)

            if unresolved:
                msg = (
                    f"⚠️ <b>{len(unresolved)}</b> ссылок без лота — укажите <b>ID лота FunPay</b>."
                )
                if bot:
                    bot.send_message(chat_id, msg, parse_mode="HTML")
                if not grouped:
                    return

            results: list[str] = []
            total_added = 0
            t0 = time.time()
            for lot_id, lot_items in grouped.items():
                lines = self._format_import_lines(lot_items)
                ok, info = self._append_stock_lines_to_lot(lot_id, lines)
                results.append(info)
                if ok:
                    total_added += len(lines)
                    self._mark_imported_orders(lot_items)

            summary = (
                f"✅ <b>Выложено за {int(time.time() - t0)}с</b>\n"
                f"🔗 Ссылок: <b>{total_added}</b>\n"
                + "\n".join(f"📦 {_escape(r)}" for r in results)
            )
            self.log("import stock: %s links -> %s", total_added, list(grouped.keys()))
            if bot:
                bot.send_message(chat_id, summary, parse_mode="HTML")
            os.makedirs(os.path.dirname(IMPORT_LOG_FILE), exist_ok=True)
            try:
                hist: list[Any] = []
                if os.path.exists(IMPORT_LOG_FILE):
                    with open(IMPORT_LOG_FILE, "r", encoding="utf-8") as f:
                        hist = json.load(f)
                hist.append({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "count": total_added,
                    "lots": list(grouped.keys()),
                })
                with open(IMPORT_LOG_FILE, "w", encoding="utf-8") as f:
                    json.dump(hist[-50:], f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        except Exception as exc:
            logger.error("%s import confirm: %s", _P, exc)
            if bot:
                bot.send_message(chat_id, f"❌ <code>{_escape(exc)}</code>", parse_mode="HTML")
        finally:
            self._import_running = False

    def cancel_import(self, chat_id: int, user_id: int) -> None:
        self._import_buffers.pop((chat_id, user_id), None)
        self._import_pending.pop(chat_id, None)
        self._import_target.pop(chat_id, None)

    def stock_mode(self) -> str:
        mode = str(self.get_cfg("stock_mode", "auto_buy") or "auto_buy")
        if mode in _LEGACY_STOCK_MODES:
            return "gemini_links"
        return mode if mode in STOCK_MODES else "auto_buy"

    def _autobuy_allowed_quick(self) -> tuple[bool, str]:
        if not str(self.get_cfg("supplier_api_key", "")).strip():
            return False, "Не задан API-ключ"
        lot_raw = str(self.get_cfg("autobuy_lot_id", "")).strip()
        if lot_raw.isdigit() or self._cached_lot_id:
            return True, ""
        return True, ""

    def _autobuy_allowed(self) -> tuple[bool, str]:
        plan = self.calc_buy_plan(timeout=UI_API_TIMEOUT)
        ok, reason = self._validate_buy_plan(plan)
        if not ok:
            return False, reason
        if not self._resolve_autobuy_lot_ids(silent=True):
            return False, "Не найден лот FunPay — укажите ID лота"
        return True, ""

    def stock_mode_label(self, mode: str | None = None) -> str:
        return STOCK_MODES.get(mode or self.stock_mode(), mode or self.stock_mode())

    def cycle_stock_mode(self) -> str:
        cur = self.stock_mode()
        idx = STOCK_MODE_ORDER.index(cur) if cur in STOCK_MODE_ORDER else 0
        nxt = STOCK_MODE_ORDER[(idx + 1) % len(STOCK_MODE_ORDER)]
        self.set_cfg("stock_mode", nxt)
        return nxt

    def _load_warehouse(self) -> list[dict[str, str]]:
        os.makedirs(os.path.dirname(WAREHOUSE_FILE), exist_ok=True)
        try:
            if os.path.exists(WAREHOUSE_FILE):
                with open(WAREHOUSE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict) and x.get("url")]
        except Exception as exc:
            self.log("warehouse load: %s", exc)
        return []

    def _save_warehouse(self, items: list[dict[str, str]]) -> None:
        os.makedirs(os.path.dirname(WAREHOUSE_FILE), exist_ok=True)
        with open(WAREHOUSE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

    def warehouse_count(self) -> int:
        return len(self._load_warehouse())

    def add_items_to_warehouse(self, items: list[dict[str, str]]) -> tuple[int, int]:
        wh = self._load_warehouse()
        known_urls = {str(x.get("url", "")) for x in wh}
        known_orders = {str(x.get("order_id", "")) for x in wh if x.get("order_id")}
        added = 0
        skipped = 0
        for item in items:
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            oid = str(item.get("order_id", "")).strip()
            if url in known_urls or (oid and oid in known_orders):
                skipped += 1
                continue
            wh.append({
                "url": url,
                "product": item.get("product", ""),
                "order_id": oid,
                "added_at": datetime.now().isoformat(timespec="seconds"),
            })
            known_urls.add(url)
            if oid:
                known_orders.add(oid)
            added += 1
        if added:
            self._save_warehouse(wh)
        return added, skipped

    # ── Gemini link archive + выдача по заказу ────────────────────────────────

    def _load_gemini_archive(self) -> list[dict[str, str]]:
        os.makedirs(os.path.dirname(GEMINI_ARCHIVE_FILE), exist_ok=True)
        try:
            if os.path.exists(GEMINI_ARCHIVE_FILE):
                with open(GEMINI_ARCHIVE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict) and x.get("url")]
        except Exception as exc:
            self.log("gemini archive load: %s", exc)
        return []

    def _save_gemini_archive(self, items: list[dict[str, str]]) -> None:
        os.makedirs(os.path.dirname(GEMINI_ARCHIVE_FILE), exist_ok=True)
        with open(GEMINI_ARCHIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

    def gemini_archive_count(self) -> int:
        return len(self._load_gemini_archive())

    def add_urls_to_gemini_archive(self, urls: list[str]) -> tuple[int, int]:
        archive = self._load_gemini_archive()
        known = {str(x.get("url", "")) for x in archive}
        added = 0
        skipped = 0
        for raw in urls:
            url = _normalize_activation_url(raw)
            if not url or not url.startswith("http"):
                skipped += 1
                continue
            if url in known:
                skipped += 1
                continue
            archive.append({
                "url": url,
                "added_at": datetime.now().isoformat(timespec="seconds"),
            })
            known.add(url)
            added += 1
        if added:
            self._save_gemini_archive(archive)
        return added, skipped

    def take_urls_from_gemini_archive(self, count: int) -> list[str]:
        count = max(0, int(count))
        if count <= 0:
            return []
        with self._lock:
            archive = self._load_gemini_archive()
            taken = archive[:count]
            rest = archive[count:]
            if taken:
                self._save_gemini_archive(rest)
            return [str(x.get("url", "")) for x in taken if x.get("url")]

    def return_urls_to_gemini_archive(self, urls: list[str]) -> None:
        urls = [_normalize_activation_url(u) for u in urls if u]
        if not urls:
            return
        with self._lock:
            archive = self._load_gemini_archive()
            known = {str(x.get("url", "")) for x in archive}
            for url in reversed(urls):
                if url and url not in known:
                    archive.insert(0, {
                        "url": url,
                        "added_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    known.add(url)
            self._save_gemini_archive(archive)

    def _load_gemini_deliveries(self) -> dict[str, Any]:
        os.makedirs(os.path.dirname(GEMINI_DELIVERY_FILE), exist_ok=True)
        try:
            if os.path.exists(GEMINI_DELIVERY_FILE):
                with open(GEMINI_DELIVERY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_gemini_deliveries(self, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(GEMINI_DELIVERY_FILE), exist_ok=True)
        with open(GEMINI_DELIVERY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _is_gemini_order_delivered(self, order_id: str | int) -> bool:
        return str(order_id) in self._load_gemini_deliveries()

    def _mark_gemini_order_delivered(
        self, order_id: str | int, buyer: str, urls: list[str], *, manual: bool = False,
    ) -> None:
        data = self._load_gemini_deliveries()
        data[str(order_id)] = {
            "buyer": buyer,
            "urls": urls,
            "manual": bool(manual),
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        trimmed = dict(list(data.items())[-500:])
        self._save_gemini_deliveries(trimmed)

    def _confirm_reminder_text(self) -> str:
        custom = str(self.get_cfg("confirm_reminder_text", "") or "").strip()
        return custom or DEFAULT_CONFIRM_REMINDER

    def _is_confirm_reminder_sent(self, order_id: str | int) -> bool:
        rec = self._load_gemini_deliveries().get(str(order_id), {})
        return bool(rec.get("confirm_sent"))

    def _mark_confirm_reminder_sent(self, order_id: str | int) -> None:
        data = self._load_gemini_deliveries()
        rec = data.get(str(order_id), {})
        if not rec:
            return
        rec["confirm_sent"] = True
        rec["confirm_sent_at"] = datetime.now().isoformat(timespec="seconds")
        data[str(order_id)] = rec
        self._save_gemini_deliveries(data)

    def _schedule_confirm_reminder(self, order_id: str, chat_id: int, buyer: str) -> None:
        if not bool(self.get_cfg("confirm_reminder_enabled", True)):
            return
        if self._is_confirm_reminder_sent(order_id):
            return
        delay = max(60, int(self.get_cfg("confirm_reminder_delay_sec", CONFIRM_REMINDER_DELAY)))

        def _run() -> None:
            time.sleep(delay)
            if self._is_confirm_reminder_sent(order_id):
                return
            try:
                self._fp_send(
                    chat_id,
                    self._confirm_reminder_text(),
                    buyer,
                    watermark=False,
                )
                self._mark_confirm_reminder_sent(order_id)
                self.log("заказ #%s: напоминание о подтверждении отправлено", order_id)
                _boot_log(f"CONFIRM #{order_id} sent after {delay}s")
            except Exception as exc:
                self.log("заказ #%s: напоминание о подтверждении: %s", order_id, exc)
                _boot_log(f"CONFIRM #{order_id} fail: {exc}")

        threading.Thread(
            target=_run, daemon=True, name=f"GeminiConfirm-{order_id}",
        ).start()
        _boot_log(f"CONFIRM #{order_id} scheduled in {delay}s")

    def _gemini_lot_match(self) -> str:
        return str(self.get_cfg("gemini_lot_match", DEFAULT_GEMINI_LOT_MATCH)).strip()

    def _gemini_lots_cached(self) -> list[int]:
        if self._gemini_lots_cache and (time.time() - self._gemini_lots_cache_ts) < GEMINI_LOT_CACHE_TTL:
            return list(self._gemini_lots_cache)
        return []

    def _scan_gemini_lots(self, *, timeout: float | None = None, blocking: bool = True) -> list[int]:
        if not blocking:
            if self._gemini_lots_scan_lock.acquire(blocking=False):
                try:
                    if self._gemini_lots_scan_running:
                        return list(self._gemini_lots_cache)
                finally:
                    self._gemini_lots_scan_lock.release()
            return list(self._gemini_lots_cache)

        acquired = self._gemini_lots_scan_lock.acquire(blocking=blocking)
        if not acquired:
            return list(self._gemini_lots_cache)
        try:
            if self._gemini_lots_scan_running:
                return list(self._gemini_lots_cache)
            self._gemini_lots_scan_running = True
            needle = self._gemini_lot_match()
            lot_ids = self._iter_profile_lot_ids()
            matched: list[int] = list(self._gemini_lots_cache)
            known = set(matched)
            deadline = time.time() + float(timeout or GEMINI_LOT_SCAN_TIMEOUT)
            api_calls = 0
            for lot_id in lot_ids:
                if time.time() >= deadline or api_calls >= GEMINI_LOT_SCAN_MAX_API:
                    self.log(
                        "скан gemini: таймаут (%s лот(ов), api=%s/%s)",
                        len(matched), api_calls, GEMINI_LOT_SCAN_MAX_API,
                    )
                    break
                if lot_id in known:
                    continue
                detail = self._lot_detailed_description(lot_id)
                api_calls += 1
                if _lot_text_matches(detail, needle):
                    matched.append(lot_id)
                    known.add(lot_id)
            self._gemini_lots_cache = matched
            self._gemini_lots_cache_ts = time.time()
            self.log(
                "скан gemini: %s лот(ов) с %r из %s (api=%s)",
                len(matched), needle, len(lot_ids), api_calls,
            )
            return list(matched)
        finally:
            self._gemini_lots_scan_running = False
            self._gemini_lots_scan_lock.release()

    def refresh_gemini_lots_async(self) -> None:
        if self._gemini_lots_scan_running:
            return
        threading.Thread(
            target=lambda: self._scan_gemini_lots(timeout=GEMINI_LOT_SCAN_TIMEOUT, blocking=True),
            daemon=True,
            name="GeminiLotScan",
        ).start()

    def find_gemini_link_lot_ids(self, *, fast: bool = False) -> list[int]:
        cached = self._gemini_lots_cached()
        if fast:
            return cached
        if cached:
            self.refresh_gemini_lots_async()
            return cached
        if self._gemini_lots_scan_running:
            return list(self._gemini_lots_cache)
        return self._scan_gemini_lots(timeout=GEMINI_LOT_SCAN_TIMEOUT, blocking=True)

    def count_gemini_link_lots(self, *, fast: bool = False) -> int:
        return len(self.find_gemini_link_lot_ids(fast=fast))

    def _lot_temp_desc(self, lot: Any) -> str:
        return ", ".join(
            i for i in (
                getattr(lot, "server", None),
                getattr(lot, "side", None),
                getattr(lot, "description", None),
            ) if i
        )

    def _match_lot_id_by_order_desc(self, desc: str, profile: Any) -> int | None:
        desc = (desc or "").strip()
        if not desc or not profile:
            return None
        best_id: int | None = None
        best_len = 0
        try:
            for lots_map in profile.get_sorted_lots(2).values():
                lots = sorted(
                    lots_map.values(),
                    key=lambda lot: len(self._lot_temp_desc(lot)),
                    reverse=True,
                )
                for lot in lots:
                    temp_desc = self._lot_temp_desc(lot)
                    if not temp_desc:
                        continue
                    if temp_desc in desc or _desc_contains_lot_key(desc, temp_desc):
                        key_len = len(_normalize_lot_desc_key(temp_desc))
                        if key_len > best_len:
                            best_len = key_len
                            best_id = int(lot.id)
        except Exception as exc:
            logger.debug("%s match lot by desc: %s", _P, exc)
        return best_id

    def _match_gemini_lot_from_order_text(self, order_text: str) -> int | None:
        order_text = (order_text or "").strip()
        if not order_text:
            return None
        profile = self._ensure_profile()
        if profile:
            lot_id = self._match_lot_id_by_order_desc(order_text, profile)
            if lot_id:
                return lot_id
            best_id: int | None = None
            best_len = 0
            try:
                for lots_map in profile.get_sorted_lots(2).values():
                    for lot in lots_map.values():
                        temp_desc = self._lot_temp_desc(lot)
                        if temp_desc and _desc_contains_lot_key(order_text, temp_desc):
                            key_len = len(_normalize_lot_desc_key(temp_desc))
                            if key_len > best_len:
                                best_len = key_len
                                best_id = int(lot.id)
            except Exception as exc:
                logger.debug("%s match gemini lot text: %s", _P, exc)
            if best_id:
                return best_id
        cfg_lot = str(self.get_cfg("gemini_lot_id", "")).strip()
        if cfg_lot.isdigit():
            return int(cfg_lot)
        cached = list(self._gemini_lots_cache)
        if len(cached) == 1 and _order_looks_like_gemini_activation(order_text):
            return int(cached[0])
        return None

    def _resolve_order_lot_id(self, event: Any, full_order: Any = None) -> int | None:
        shortcut = getattr(event, "lot_shortcut", None)
        if shortcut is not None:
            try:
                return int(shortcut.id)
            except (TypeError, ValueError, AttributeError):
                pass
        for src in (event, full_order):
            if src is None:
                continue
            lid = getattr(src, "lot_id", None)
            if lid is not None:
                try:
                    return int(lid)
                except (TypeError, ValueError):
                    pass
        order = full_order or getattr(event, "order", None)
        order_text = self._order_text_blob(event, full_order) if event is not None else ""
        if order is not None and not order_text:
            order_text = str(getattr(order, "description", "") or "")
        cfg_lot = str(self.get_cfg("gemini_lot_id", "")).strip()
        if order is None:
            return int(cfg_lot) if cfg_lot.isdigit() else None
        desc = order_text or str(getattr(order, "description", "") or "")
        profile = self._ensure_profile()
        if profile and desc:
            subcat = getattr(order, "subcategory", None)
            if subcat:
                try:
                    lots_map = profile.get_sorted_lots(2).get(subcat, {})
                    lots = sorted(
                        lots_map.values(),
                        key=lambda lot: len(self._lot_temp_desc(lot)),
                        reverse=True,
                    )
                    for lot in lots:
                        temp_desc = self._lot_temp_desc(lot)
                        if temp_desc and (
                            temp_desc in desc or _desc_contains_lot_key(desc, temp_desc)
                        ):
                            return int(lot.id)
                except Exception as exc:
                    logger.debug("%s resolve order lot subcat: %s", _P, exc)
            lot_id = self._match_lot_id_by_order_desc(desc, profile)
            if lot_id:
                return lot_id
        matched = self._match_gemini_lot_from_order_text(order_text or desc)
        if matched:
            return matched
        if cfg_lot.isdigit():
            return int(cfg_lot)
        cached = self._gemini_lots_cached() or list(self._gemini_lots_cache)
        if len(cached) == 1 and _order_looks_like_gemini_activation(order_text or desc):
            return int(cached[0])
        return None

    def _order_text_blob(self, event: NewOrderEvent, full_order: Any = None) -> str:
        chunks: list[str] = []
        order = full_order or event.order
        for attr in ("full_description", "description", "short_description", "title", "lot_params_text"):
            val = getattr(order, attr, None)
            if val:
                chunks.append(str(val))
        if not chunks:
            chunks.append(str(getattr(event.order, "description", "") or ""))
        subcat = getattr(order, "subcategory", None)
        if subcat:
            for attr in ("fullname", "name"):
                val = getattr(subcat, attr, None)
                if val:
                    chunks.append(str(val))
        return "\n".join(chunks)

    def _order_matches_gemini_link(
        self,
        event: Any,
        full_order: Any = None,
        *,
        relaxed: bool = False,
    ) -> bool:
        needle = self._gemini_lot_match()
        if not needle:
            return False
        order_text = self._order_text_blob(event, full_order)
        lot_id = self._resolve_order_lot_id(event, full_order)
        order_id = getattr(getattr(event, "order", None), "id", "?")
        if not lot_id and _order_looks_like_gemini_activation(order_text):
            lot_id = self._match_gemini_lot_from_order_text(order_text)
        cfg_lot = str(self.get_cfg("gemini_lot_id", "")).strip()
        if not lot_id and cfg_lot.isdigit():
            lot_id = int(cfg_lot)
        if relaxed and _order_looks_like_gemini_activation(order_text):
            if lot_id or cfg_lot.isdigit() or self.gemini_archive_count() > 0:
                return True
        if not lot_id:
            self.log(
                "заказ #%s: lot_id не найден (нужна метка %r в подробном описании лота)",
                order_id, needle,
            )
            return False
        if lot_id in self._gemini_lots_cache:
            return True
        if cfg_lot.isdigit() and int(cfg_lot) == lot_id:
            return True
        detail = self._lot_detailed_description(lot_id)
        matched = _lot_text_matches(detail, needle)
        if not matched and relaxed and _order_looks_like_gemini_activation(order_text):
            matched = True
        if matched:
            if lot_id not in self._gemini_lots_cache:
                self._gemini_lots_cache.append(lot_id)
                self._gemini_lots_cache_ts = time.time()
            return True
        self.log(
            "заказ #%s лот #%s: в подробном описании нет %r",
            order_id, lot_id, needle,
        )
        return False

    def _resolve_chat_id_int(self, chat_id: Any) -> int | None:
        if chat_id is None:
            return None
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return None

    def _resolve_order_chat_id(
        self,
        full_order: Any,
        event: Any = None,
        buyer: str = "",
        order_id: str = "",
    ) -> int | None:
        buyer = (buyer or "").strip()
        candidates: list[Any] = []
        for src in (full_order, getattr(event, "order", None) if event else None, event):
            if src is None:
                continue
            cid = getattr(src, "chat_id", None)
            if cid is not None:
                candidates.append(cid)
        for cid in candidates:
            parsed = self._resolve_chat_id_int(cid)
            if parsed is not None:
                return parsed
        if buyer:
            for make_req in (True, False):
                try:
                    chat = self.cardinal.account.get_chat_by_name(buyer, make_req)
                    if chat and getattr(chat, "id", None) is not None:
                        return int(chat.id)
                except Exception as exc:
                    logger.debug("%s get_chat_by_name(%s): %s", _P, buyer, exc)
            try:
                chats = self.cardinal.account.get_chats()
                buyer_l = buyer.casefold()
                for chat in chats or []:
                    for attr in ("name", "username", "interlocutor_username"):
                        name = str(getattr(chat, attr, "") or "").strip()
                        if name and name.casefold() == buyer_l:
                            return int(chat.id)
            except Exception as exc:
                logger.debug("%s get_chats for %s: %s", _P, buyer, exc)
        if order_id:
            try:
                full = full_order or self.cardinal.account.get_order(order_id)
                cid = getattr(full, "chat_id", None)
                parsed = self._resolve_chat_id_int(cid)
                if parsed is not None:
                    return parsed
            except Exception as exc:
                logger.debug("%s get_order chat #%s: %s", _P, order_id, exc)
        return None

    def _account_send_message(self, chat_id: int, text: str, chat_name: str | None) -> Any:
        acc = self.cardinal.account
        old_mode = bool(getattr(self.cardinal, "old_mode_enabled", False))
        keep_unread = bool(getattr(self.cardinal, "keep_sent_messages_unread", False)) and old_mode
        try:
            return acc.send_message(
                chat_id, text, chat_name,
                None, not old_mode, old_mode, keep_unread,
            )
        except TypeError:
            return acc.send_message(chat_id, text, chat_name)

    def _fp_send(self, chat_id: Any, text: str, buyer: str, *, watermark: bool = False) -> None:
        cid = self._resolve_chat_id_int(chat_id)
        if cid is None:
            cid = self._resolve_order_chat_id(None, None, buyer)
        if cid is None:
            raise ValueError(f"chat_id пуст (buyer={buyer!r})")
        buyer_name = (buyer or "").strip() or None
        text = (text or "").strip()
        if not text:
            raise ValueError("пустой текст сообщения")

        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                msg = self._account_send_message(cid, text, buyer_name)
                if msg is not None:
                    _boot_log(f"FP send OK chat={cid} try={attempt} len={len(text)}")
                    return
                last_err = RuntimeError("account.send_message вернул None")
            except Exception as exc:
                last_err = exc
                _boot_log(f"FP send FAIL chat={cid} try={attempt}: {exc}")
            time.sleep(1.5 * attempt)

        try:
            result = self.cardinal.send_message(
                cid, text, buyer_name, watermark=watermark, attempts=3,
            )
            if result:
                _boot_log(f"FP send OK via cardinal chat={cid} len={len(text)}")
                return
            last_err = RuntimeError("cardinal.send_message вернул пустой результат")
        except Exception as exc:
            last_err = exc
            _boot_log(f"FP send FAIL cardinal chat={cid}: {exc}")

        raise RuntimeError(str(last_err) if last_err else "FunPay send failed")

    def _deliver_link_in_parts(self, chat_id: Any, buyer: str, url: str, parts: int) -> None:
        """3 отдельных сообщения — только фрагмент ссылки, без нумерации; в конце — инструкция."""
        chunks = split_link_parts(url, parts)
        for idx, chunk in enumerate(chunks):
            chunk = (chunk or "").strip()
            if not chunk:
                raise ValueError(f"пустая часть ссылки ({idx + 1}/{len(chunks)})")
            self._fp_send(chat_id, chunk, buyer, watermark=False)
            if idx < len(chunks) - 1:
                time.sleep(LINK_PART_DELAY)
        time.sleep(LINK_PART_DELAY)
        self._fp_send(
            chat_id,
            "Склейте все части в одну ссылку и вставьте её в браузер.",
            buyer,
            watermark=False,
        )

    def _deliver_single_gemini_link(self, chat_id: Any, buyer: str, url: str) -> tuple[bool, str]:
        parts = max(2, int(self.get_cfg("gemini_delivery_parts", DEFAULT_DELIVERY_PARTS)))
        redelivery_parts = max(parts + 1, int(self.get_cfg("gemini_redelivery_parts", DEFAULT_REDELIVERY_PARTS)))
        try:
            self._deliver_link_in_parts(chat_id, buyer, url, parts)
            return True, ""
        except Exception as exc:
            err = str(exc)
            self.log("выдача %s частями не удалась: %s — перевыдача", parts, err)
            _boot_log(f"deliver parts={parts} fail: {err}")
            try:
                self._fp_send(
                    chat_id,
                    "Перевыдача — отправляю ссылку другими частями:",
                    buyer,
                    watermark=False,
                )
                time.sleep(0.8)
                self._deliver_link_in_parts(chat_id, buyer, url, redelivery_parts)
                return True, ""
            except Exception as exc2:
                err2 = str(exc2)
                self.log("перевыдача %s частями не удалась: %s", redelivery_parts, err2)
                _boot_log(f"redeliver parts={redelivery_parts} fail: {err2}")
                return False, err2 or err

    def deliver_gemini_order(
        self,
        order_id: str,
        *,
        event: NewOrderEvent | None = None,
        full_order: Any = None,
        force: bool = False,
        manual: bool = False,
    ) -> tuple[bool, str]:
        order_id = str(order_id).strip().lstrip("#")
        if not order_id:
            return False, "Не указан номер заказа"
        mode = self.stock_mode()
        if mode != "gemini_links":
            return False, f"Режим {mode} — переключите на 🔗 Gemini"
        if not force and self._is_gemini_order_delivered(order_id):
            return False, f"Заказ #{order_id} уже выдан (добавьте force)"
        if order_id in self._delivery_in_progress:
            return False, f"Заказ #{order_id} уже обрабатывается"
        self._delivery_in_progress.add(order_id)
        try:
            return self._deliver_gemini_order_impl(
                order_id,
                event=event,
                full_order=full_order,
                force=force,
                manual=manual,
            )
        finally:
            self._delivery_in_progress.discard(order_id)

    def _deliver_gemini_order_impl(
        self,
        order_id: str,
        *,
        event: NewOrderEvent | None = None,
        full_order: Any = None,
        force: bool = False,
        manual: bool = False,
    ) -> tuple[bool, str]:
        if full_order is None:
            try:
                full_order = self.cardinal.account.get_order(order_id)
            except Exception as exc:
                return False, f"get_order #{order_id}: {exc}"
        if event is None:
            event = SimpleNamespace(
                order=full_order,
                lot_id=getattr(full_order, "lot_id", None),
                lot_shortcut=getattr(full_order, "lot_shortcut", None),
            )
        lot_id = self._resolve_order_lot_id(event, full_order)
        order_text = self._order_text_blob(event, full_order)
        relaxed = manual
        _boot_log(
            f"DELIVER #{order_id} lot_id={lot_id} archive={self.gemini_archive_count()} "
            f"force={force} manual={manual} relaxed={relaxed}"
        )
        if not self._order_matches_gemini_link(event, full_order, relaxed=relaxed):
            return False, f"Заказ #{order_id} не Gemini-link лот (lot_id={lot_id})"
        buyer = str(getattr(full_order, "buyer_username", "") or getattr(event.order, "buyer_username", "") or "")
        qty = max(1, int(getattr(full_order, "amount", None) or getattr(event.order, "amount", 1) or 1))
        chat_id = self._resolve_order_chat_id(full_order, event, buyer, order_id)
        if not chat_id:
            return False, f"Заказ #{order_id}: chat_id не найден (buyer={buyer or '?'})"
        _boot_log(f"DELIVER #{order_id} chat_id={chat_id} buyer={buyer} qty={qty}")
        try:
            self._fp_send(
                chat_id,
                "Выдаю ссылку активации Gemini…",
                buyer,
                watermark=False,
            )
        except Exception as exc:
            self.log("заказ #%s: приветствие: %s", order_id, exc)
            _boot_log(f"DELIVER #{order_id} welcome fail: {exc}")
        urls = self.take_urls_from_gemini_archive(qty)
        if len(urls) < qty:
            self.return_urls_to_gemini_archive(urls)
            try:
                self._fp_send(
                    chat_id,
                    f"Недостаточно ссылок в архиве ({len(urls)}/{qty}). Позовите продавца.",
                    buyer,
                    watermark=False,
                )
            except Exception:
                pass
            err = f"Архив пуст ({len(urls)}/{qty} ссылок)"
            self.set_cfg("last_delivery_error", err)
            return False, err
        delivered: list[str] = []
        failed: list[str] = []
        last_err = ""
        for i, url in enumerate(urls, 1):
            ok_link, err_link = self._deliver_single_gemini_link(chat_id, buyer, url)
            if ok_link:
                delivered.append(url)
                if qty > 1 and i < len(urls):
                    time.sleep(LINK_PART_DELAY)
            else:
                failed.append(url)
                last_err = err_link or "send failed"
        if failed:
            self.return_urls_to_gemini_archive(failed)
        if delivered:
            self._mark_gemini_order_delivered(order_id, buyer, delivered, manual=manual)
            self._schedule_confirm_reminder(order_id, chat_id, buyer)
            self.set_cfg("last_delivery_error", "")
            self.log("заказ #%s: выдано %s ссылок (manual=%s)", order_id, len(delivered), manual)
            _boot_log(f"DELIVER #{order_id} OK delivered={len(delivered)} manual={manual}")
            note = f" ({len(delivered)} ссылок)" if len(delivered) > 1 else ""
            return True, f"✅ Заказ #{order_id}: выдано{note}"
        err_msg = f"Заказ #{order_id}: не удалось отправить в чат FunPay"
        if last_err:
            err_msg += f"\n<code>{_escape(last_err[:200])}</code>"
        self.set_cfg("last_delivery_error", last_err or err_msg)
        try:
            self._fp_send(
                chat_id,
                "Не удалось отправить ссылку автоматически. Позовите продавца.",
                buyer,
                watermark=False,
            )
        except Exception:
            pass
        return False, err_msg

    def handle_new_order(self, event: NewOrderEvent) -> None:
        order_id = str(event.order.id)
        mode = self.stock_mode()
        _boot_log(f"ORDER #{order_id} event mode={mode}")
        if mode != "gemini_links":
            _boot_log(f"ORDER #{order_id} skip: режим {mode}, нужен gemini_links")
            return
        if not bool(self.get_cfg("gemini_auto_enabled", True)):
            _boot_log(f"ORDER #{order_id} skip: автовыдача выключена")
            return
        if self._is_gemini_order_delivered(order_id):
            self.log("заказ #%s уже обработан", order_id)
            return
        self.refresh_gemini_lots_async()
        full_order = None
        try:
            full_order = self.cardinal.account.get_order(order_id)
        except Exception as exc:
            self.log("get_order #%s: %s", order_id, exc)
        if full_order and getattr(event, "lot_id", None) is None:
            lid = getattr(full_order, "lot_id", None)
            if lid is not None:
                try:
                    event.lot_id = int(lid)
                except (TypeError, ValueError):
                    pass
        lot_id = self._resolve_order_lot_id(event, full_order)
        _boot_log(
            f"ORDER #{order_id} lot_id={lot_id} event.lot_id={getattr(event, 'lot_id', None)} "
            f"archive={self.gemini_archive_count()}"
        )
        if not lot_id:
            time.sleep(2.5)
            try:
                full_order = self.cardinal.account.get_order(order_id)
            except Exception:
                pass
            lot_id = self._resolve_order_lot_id(event, full_order)
            if lot_id:
                _boot_log(f"ORDER #{order_id} lot_id retry={lot_id}")
        if not self._order_matches_gemini_link(event, full_order, relaxed=False):
            _boot_log(f"ORDER #{order_id} skip: не gemini link лот")
            return
        ok, msg = self.deliver_gemini_order(
            order_id, event=event, full_order=full_order, force=False, manual=False,
        )
        if not ok:
            _boot_log(f"ORDER #{order_id} FAIL: {msg}")
            self.log("%s", msg)

    def notify_gemini_status(self, chat_id: int) -> None:
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        if not bot:
            return
        try:
            match = self._gemini_lot_match()
            lot_ids = self.find_gemini_link_lot_ids(fast=True)
            if not lot_ids:
                lot_ids = self._scan_gemini_lots(timeout=GEMINI_LOT_SCAN_TIMEOUT, blocking=True)
            else:
                self.refresh_gemini_lots_async()
            lines = [
                f"📊 <b>Gemini Link</b> — <code>{_escape(match)}</code>",
                f"📋 <b>Лотов на FunPay:</b> <b>{len(lot_ids)}</b>",
                f"🗄 <b>Архив ссылок:</b> <b>{self.gemini_archive_count()}</b> шт.",
                f"<b>Режим:</b> {self.stock_mode_label()}",
            ]
            for lot_id in lot_ids[:15]:
                detail = self._lot_detailed_description(lot_id)[:80].replace("\n", " ")
                lines.append(f"• Лот <b>#{lot_id}</b>: <code>{_escape(detail)}…</code>")
            if len(lot_ids) > 15:
                lines.append(f"<i>…и ещё {len(lot_ids) - 15} лотов</i>")
            if not lot_ids:
                lines.append(
                    f"\n⚠️ Добавьте <code>{_escape(match)}</code> в <b>подробное описание</b> лота."
                )
            bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        except Exception as exc:
            bot.send_message(chat_id, f"❌ {_escape(exc)}", parse_mode="HTML")

    def release_warehouse_to_lot(self, notify_chat_id: int | None, count: int | None = None) -> None:
        if self._import_running:
            return
        self._import_running = True
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        qty = int(count or self.get_cfg("warehouse_release_qty", 5))
        qty = max(0, min(100, qty))
        t0 = time.time()
        try:
            if qty <= 0:
                msg = "⛔ Количество для выкладки = 0. Укажите число > 0 в настройках."
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, msg, parse_mode="HTML")
                return
            wh = self._load_warehouse()
            if not wh:
                msg = "📦 <b>Склад пуст.</b> Сначала загрузите Purchase History кнопкой «📥 В склад»."
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, msg, parse_mode="HTML")
                return
            batch = wh[:qty]
            grouped: dict[int, list[dict[str, str]]] = {}
            unresolved = 0
            for item in batch:
                lot_ids = self._resolve_lot_for_product(item.get("product", ""))
                if not lot_ids:
                    unresolved += 1
                    continue
                grouped.setdefault(lot_ids[0], []).append(item)

            if not grouped:
                msg = (
                    f"⚠️ Не найден лот для <b>{len(batch)}</b> ссылок со склада.\n"
                    "Укажите <b>ID лота FunPay</b> в настройках."
                )
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, msg, parse_mode="HTML")
                return

            results: list[str] = []
            total_added = 0
            released_urls: set[str] = set()
            for lot_id, lot_items in grouped.items():
                lines = self._format_import_lines(lot_items)
                ok, info = self._append_stock_lines_to_lot(lot_id, lines)
                results.append(info)
                if ok:
                    total_added += len(lines)
                    for it in lot_items:
                        released_urls.add(str(it.get("url", "")))
                    self._mark_imported_orders(lot_items)

            if released_urls:
                remaining = [x for x in wh if str(x.get("url", "")) not in released_urls]
                self._save_warehouse(remaining)

            summary = (
                f"✅ <b>Со склада за {int(time.time() - t0)}с</b>\n"
                f"📤 Выложено: <b>{total_added}</b> | осталось на складе: <b>{self.warehouse_count()}</b>"
            )
            if unresolved:
                summary += f"\n⚠️ Без лота: <b>{unresolved}</b>"
            summary += "\n" + "\n".join(f"📦 {_escape(r)}" for r in results)
            if bot and notify_chat_id:
                bot.send_message(notify_chat_id, summary, parse_mode="HTML")
        except Exception as exc:
            logger.error("%s warehouse release: %s", _P, exc)
            if bot and notify_chat_id:
                bot.send_message(notify_chat_id, f"❌ <code>{_escape(exc)}</code>", parse_mode="HTML")
        finally:
            self._import_running = False

    def _format_setting_line(self, field: dict[str, Any], val: Any) -> str:
        label = _escape(field.get("label", ""))
        ftype = field.get("type", "str")
        if ftype == "bool":
            return f"{'🟢' if val else '🔴'} {label}"
        if ftype == "multiline":
            return f"• <b>{label}</b>: <i>{len(str(val or ''))} симв.</i>"
        if ftype in ("int", "float"):
            if ftype == "float":
                try:
                    val = f"{float(val):.2f}"
                except (TypeError, ValueError):
                    val = "0.00"
            return f"• <b>{label}</b>: <code>{_escape(val)}</code>"
        if ftype == "action":
            return f"▶️ <b>{label}</b>"
        preview = _escape(str(val or "")[:55])
        if len(str(val or "")) > 55:
            preview += "…"
        return f"• <b>{label}</b>: <code>{preview or '—'}</code>"

    def _fields_for_page(self, page: str) -> list[dict[str, Any]]:
        if page == "settings":
            return self._settings_fields()
        return []

    def _lot_stock_info(self, fast: bool = False) -> str:
        if not fast and self._lot_stock_cache and (time.time() - self._lot_stock_cache_ts) < 45:
            return self._lot_stock_cache
        lot_ids = self._resolve_autobuy_lot_ids(silent=True, fast=fast)
        if not lot_ids:
            return "лот не найден"
        if fast:
            return f"лот #{lot_ids[0]}"
        try:
            lf = self.cardinal.account.get_lot_fields(int(lot_ids[0]))
            result = f"{len(lf.secrets)} шт. (лот #{lot_ids[0]})"
            self._lot_stock_cache = result
            self._lot_stock_cache_ts = time.time()
            return result
        except Exception:
            return "—"

    def _lot_hint_instant(self) -> str:
        lot_raw = str(self.get_cfg("autobuy_lot_id", "")).strip()
        if lot_raw.isdigit():
            return f"#{lot_raw}"
        if self._cached_lot_id:
            return f"#{self._cached_lot_id}"
        return "…"

    def render_settings_text(self, page: str = "hub", fast: bool = False, instant: bool = False) -> str:
        pages = self._ui_pages()
        page = page if page in pages else "hub"
        lines = [
            f"⚙️ <b>{_escape(NAME)}</b> v{VERSION}",
            "━━━━━━━━━━━━━━━━━━",
            f"<i>{_escape(DESCRIPTION)}</i>",
            "",
        ]
        if page == "hub":
            mode = self.stock_mode()
            wh_qty = int(self.get_cfg("warehouse_release_qty", 5) or 0)
            budget = self._float_cfg("buy_budget_usd")
            reserve = self._float_cfg("reserve_balance_usd", 10.0)
            if instant:
                lines += [
                    f"<b>Режим:</b> {self.stock_mode_label(mode)}",
                ]
                if mode == "auto_buy":
                    lines += [
                        f"📦 <b>Лот FunPay:</b> {self._lot_hint_instant()}",
                        f"🗄 <b>Склад плагина:</b> {self.warehouse_count()} шт.",
                    ]
                    auto_on = bool(self.get_cfg("auto_enabled", True))
                    min_stock = int(self.get_cfg("min_lot_stock", DEFAULT_MIN_LOT_STOCK))
                    lines += [
                        f"🛍 <b>Товар:</b> <code>{_escape(SHOP_PRODUCT_NAME)}</code>",
                        f"📋 <b>Формат:</b> <code>почта|пароль|2FA</code>",
                        (
                            f"🤖 <b>Авто:</b> {'🟢 ВКЛ' if auto_on else '🔴 ВЫКЛ'} | "
                            f"мин. на лоте: <b>{min_stock}</b>"
                        ),
                        (
                            f"🎯 <b>Потратить:</b> "
                            f"{'всё после резерва' if budget <= 0 else f'${budget:.2f}'}"
                            f" | <b>Резерв:</b> ${reserve:.2f}"
                        ),
                        "<i>⏳ Загрузка баланса и лота…</i>",
                    ]
                else:
                    match = self._gemini_lot_match()
                    cached = self._gemini_lots_cached()
                    lot_hint = str(len(cached)) if cached else "…"
                    auto_on = bool(self.get_cfg("gemini_auto_enabled", True))
                    lines += [
                        f"{'🟢' if auto_on else '🔴'} <b>Автовыдача:</b> {'ВКЛ' if auto_on else 'ВЫКЛ'}",
                        f"📋 <b>Лотов с</b> <code>{_escape(match)}</code>: {lot_hint}",
                        f"🗄 <b>Архив ссылок:</b> {self.gemini_archive_count()} шт.",
                        f"📨 <b>Формат:</b> приветствие + 3 части ссылки + инструкция",
                        "<i>⏳ Обновление списка лотов…</i>",
                    ]
                return "\n".join(lines)

            if mode == "gemini_links":
                match = self._gemini_lot_match()
                lot_ids = self.find_gemini_link_lot_ids(fast=fast or instant)
                auto_on = bool(self.get_cfg("gemini_auto_enabled", True))
                cache_age = ""
                if self._gemini_lots_cache_ts:
                    age = int(time.time() - self._gemini_lots_cache_ts)
                    cache_age = f" <i>(кэш {age}с назад)</i>" if age > 0 else ""
                lines += [
                    f"<b>Режим:</b> {self.stock_mode_label(mode)}",
                    f"{'🟢' if auto_on else '🔴'} <b>Автовыдача:</b> {'ВКЛ' if auto_on else 'ВЫКЛ'}",
                    f"📋 <b>Лотов с</b> <code>{_escape(match)}</code>: <b>{len(lot_ids)}</b>{cache_age}",
                    f"🗄 <b>Архив ссылок:</b> {self.gemini_archive_count()} шт.",
                    f"📨 <b>Выдача:</b> приветствие + <b>3 части</b> ссылки + инструкция",
                ]
                last_err = str(self.get_cfg("last_delivery_error", "") or "").strip()
                if last_err:
                    lines.append(f"⚠️ <i>Последняя ошибка: {_escape(last_err[:140])}</i>")
                if self.gemini_archive_count() <= 0:
                    lines.append("⚠️ <b>Архив пуст</b> — загрузите ссылки кнопкой «📥 Загрузить ссылки в архив»")
                if lot_ids:
                    lines.append(f"🎯 Лот: <b>#{lot_ids[0]}</b>" + (f" (+{len(lot_ids)-1})" if len(lot_ids) > 1 else ""))
                else:
                    lines.append(
                        f"⚠️ Добавьте <code>{_escape(match)}</code> в <b>подробное описание</b> лота"
                    )
                if self._import_running:
                    lines.append("⏳ <i>Загрузка в архив…</i>")
                return "\n".join(lines)

            plan = self.calc_buy_plan(fast=fast, timeout=UI_API_TIMEOUT)
            lines += [
                f"<b>Режим:</b> {self.stock_mode_label(mode)}",
            ]
            if mode != "gemini_links":
                lines.append(
                    "⚠️ <i>Для выдачи Gemini-ссылок переключите режим на <b>🔗 Gemini</b></i>"
                )
            if mode == "auto_buy":
                lines += [
                    f"📦 <b>Лот FunPay:</b> {self._lot_stock_info(fast=fast)}",
                    f"🗄 <b>Склад плагина:</b> {self.warehouse_count()} шт.",
                ]
                auto_on = bool(self.get_cfg("auto_enabled", True))
                min_stock = int(self.get_cfg("min_lot_stock", DEFAULT_MIN_LOT_STOCK))
                lot_ids = self._resolve_autobuy_lot_ids(silent=True, fast=fast)
                lot_hint = f"#{lot_ids[0]}" if lot_ids else "не найден"
                lines.append(f"🛍 <b>Товар:</b> <code>{_escape(SHOP_PRODUCT_NAME)}</code>")
                lines.append(f"📋 <b>Формат:</b> <code>почта|пароль|2FA</code>")
                lines.append(
                    f"🤖 <b>Авто:</b> {'🟢 ВКЛ' if auto_on else '🔴 ВЫКЛ'} | "
                    f"мин. на лоте: <b>{min_stock}</b> | лот: <b>{lot_hint}</b>"
                )
                if "balance" in plan:
                    lines.append(f"💵 <b>Баланс API:</b> ${float(plan['balance']):.2f}")
                lines.append(
                    f"🎯 <b>Потратить:</b> "
                    f"{'всё после резерва' if budget <= 0 else f'${budget:.2f}'}"
                    f" | <b>Резерв:</b> ${reserve:.2f}"
                )
                if plan.get("ok"):
                    lines.append(
                        f"✅ <b>Можно купить сейчас:</b> {plan['qty']} акк. "
                        f"(${float(plan['spend']):.2f})"
                    )
                elif plan.get("error"):
                    lines.append(f"⚠️ <i>{_escape(str(plan['error']))}</i>")
                last = str(self.get_cfg("last_auto_at", "") or "")
                if last:
                    lines.append(f"🕐 <i>Последняя закупка: {last}</i>")
            if self._autobuy_running:
                lines.append("⏳ <i>Закупка…</i>")
            if self._import_running:
                lines.append("⏳ <i>Загрузка…</i>")
            return "\n".join(lines)

        title = pages[page]["title"]
        lines.append(f"<b>{title}</b>\n")
        for field in self._fields_for_page(page):
            val = self.get_cfg(field["key"])
            lines.append(self._format_setting_line(field, val))
        if page == "settings":
            if instant:
                if self.stock_mode() == "gemini_links":
                    match = self._gemini_lot_match()
                    lines.append(
                        f"🔎 Лот ищется по тексту <code>{_escape(match)}</code> "
                        f"только в <b>подробном описании</b>"
                    )
                    lines.append(f"🗄 <b>Архив:</b> {self.gemini_archive_count()} ссылок")
                    lines.append("<i>⏳ Сканирование лотов…</i>")
                else:
                    lines.append(
                        f"🔎 Лот ищется по тексту <code>{_escape(DEFAULT_LOT_MATCH)}</code> "
                        f"только в <b>подробном описании</b>"
                    )
                    lines.append("<i>⏳ Загрузка остатков лота…</i>")
                return "\n".join(lines)
            if self.stock_mode() == "gemini_links":
                match = self._gemini_lot_match()
                lot_ids = self.find_gemini_link_lot_ids(fast=fast)
                lines.append(f"\n📋 <b>Лотов с</b> <code>{_escape(match)}</code>: <b>{len(lot_ids)}</b>")
                lines.append(f"🗄 <b>Архив ссылок:</b> {self.gemini_archive_count()} шт.")
                lines.append(
                    f"📤 <b>Выдача:</b> {int(self.get_cfg('gemini_delivery_parts', DEFAULT_DELIVERY_PARTS))} части "
                    f"(перевыдача: {int(self.get_cfg('gemini_redelivery_parts', DEFAULT_REDELIVERY_PARTS))})"
                )
            else:
                lines.append(f"\n📦 <b>На лоте:</b> {self._lot_stock_info(fast=True)}")
                lines.append(f"🛍 Товар: <code>{_escape(SHOP_PRODUCT_NAME)}</code>")
                lines.append(f"📋 Формат автовыдачи: <code>почта|пароль|2FA</code>")
                lines.append(
                    f"🔎 Лот ищется по тексту <code>{_escape(DEFAULT_LOT_MATCH)}</code> "
                    f"только в <b>подробном описании</b> лота (не в названии)"
                )
        return "\n".join(lines)

    def build_settings_keyboard(self, page: str = "hub", fast: bool = False, instant: bool = False) -> IKM:
        kb = IKM()
        page = page if page in self._ui_pages() else "hub"

        if page == "hub":
            mode = self.stock_mode()
            kb.row(
                IKB(
                    f"{'• ' if mode == 'auto_buy' else ''}🤖 ChatGPT",
                    callback_data=f"{CB_PREFIX}:mode:auto_buy",
                ),
                IKB(
                    f"{'• ' if mode == 'gemini_links' else ''}🔗 Gemini",
                    callback_data=f"{CB_PREFIX}:mode:gemini_links",
                ),
            )
            if mode == "auto_buy":
                budget = self._float_cfg("buy_budget_usd")
                reserve = self._float_cfg("reserve_balance_usd", 10.0)
                auto_on = bool(self.get_cfg("auto_enabled", True))
                min_stock = int(self.get_cfg("min_lot_stock", DEFAULT_MIN_LOT_STOCK))
                plan = (
                    {"ok": False}
                    if instant
                    else self.calc_buy_plan(fast=fast, timeout=UI_API_TIMEOUT)
                )
                kb.row(IKB(
                    f"{'🟢' if auto_on else '🔴'} Авто: {'ВКЛ' if auto_on else 'ВЫКЛ'}",
                    callback_data=f"{CB_PREFIX}:togkey:auto_enabled",
                ))
                kb.row(
                    IKB(f"📉 Мин. на лоте: {min_stock}", callback_data=f"{CB_PREFIX}:editkey:min_lot_stock"),
                    IKB("💰 Баланс", callback_data=f"{CB_PREFIX}:act:shop_balance"),
                )
                kb.row(
                    IKB(f"💵 Потратить: ${budget:.2f}" if budget > 0 else "💵 Потратить: всё", callback_data=f"{CB_PREFIX}:editkey:buy_budget_usd"),
                    IKB(f"🔒 Резерв: ${reserve:.2f}", callback_data=f"{CB_PREFIX}:editkey:reserve_balance_usd"),
                )
                if plan.get("ok"):
                    kb.row(IKB(
                        f"🛒 Купить {plan['qty']} акк. (${float(plan['spend']):.2f})",
                        callback_data=f"{CB_PREFIX}:act:run_autobuy",
                    ))
                kb.row(IKB("⚙️ Настройки", callback_data=f"{CB_PREFIX}:nav:settings"))
            else:
                auto_on = bool(self.get_cfg("gemini_auto_enabled", True))
                kb.row(IKB(
                    f"{'🟢' if auto_on else '🔴'} Автовыдача: {'ВКЛ' if auto_on else 'ВЫКЛ'}",
                    callback_data=f"{CB_PREFIX}:togkey:gemini_auto_enabled",
                ))
                kb.row(
                    IKB("📤 Выдать заказ", callback_data=f"{CB_PREFIX}:act:deliver_order"),
                    IKB("📥 В архив", callback_data=f"{CB_PREFIX}:act:start_import:gemini_archive"),
                )
                kb.row(IKB("🔄 Обновить", callback_data=f"{CB_PREFIX}:nav:hub"))
                kb.row(IKB("⚙️ Настройки", callback_data=f"{CB_PREFIX}:nav:settings"))
            kb.row(IKB("📊 Статус", callback_data=f"{CB_PREFIX}:act:stock_status"))
        else:
            fields = self._fields_for_page(page)
            for i, field in enumerate(fields):
                key = field["key"]
                label = field.get("label", key)
                ftype = field.get("type", "str")
                val = self.get_cfg(key, "")
                if ftype == "float":
                    try:
                        disp = f"{float(val):.2f}"
                    except (TypeError, ValueError):
                        disp = "0.00"
                elif ftype == "int":
                    disp = str(val)
                else:
                    disp = str(val).replace("\n", " ")[:14]
                    if len(str(val)) > 14:
                        disp += "…"
                kb.add(IKB(
                    f"✏️ {label[:22]}: {disp or '—'}",
                    callback_data=f"{CB_PREFIX}:edit:{page}:{i}",
                ))
            if page == "settings" and self.stock_mode() == "auto_buy":
                plan = (
                    {"ok": False}
                    if instant
                    else self.calc_buy_plan(fast=fast, timeout=UI_API_TIMEOUT)
                )
                if plan.get("ok"):
                    kb.row(IKB(
                        f"🛒 Купить {plan['qty']} шт.",
                        callback_data=f"{CB_PREFIX}:act:run_autobuy",
                    ))
                kb.row(IKB("💰 Баланс API", callback_data=f"{CB_PREFIX}:act:shop_balance"))
            kb.row(IKB("🏠 Главная", callback_data=f"{CB_PREFIX}:nav:hub"))

        kb.add(IKB("◀️ К плагину", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
        return kb







    # ── Order helpers ────────────────────────────────────────────────────────




























    def on_settings_action(self, call: CallbackQuery, action: str, arg: str = "") -> bool:
        bot = self.cardinal.telegram.bot
        chat_id = call.message.chat.id





        if action == "run_autobuy":
            if self._autobuy_running:
                bot.answer_callback_query(call.id, "Уже выполняется…", show_alert=True)
                return True
            ok_quick, reason_quick = self._autobuy_allowed_quick()
            if not ok_quick:
                bot.answer_callback_query(call.id, reason_quick[:180], show_alert=True)
                return True
            bot.answer_callback_query(call.id, "Запускаю закупку…")
            threading.Thread(
                target=self._run_autobuy_from_ui, args=(chat_id,), daemon=True,
            ).start()
            return True
        if action == "shop_balance":
            bot.answer_callback_query(call.id, "Смотрю баланс…")
            threading.Thread(target=self.notify_shop_balance, args=(chat_id,), daemon=True).start()
            return True
        if action == "stock_status":
            bot.answer_callback_query(call.id, "Смотрю склад…")
            threading.Thread(target=self.notify_stock_status, args=(chat_id,), daemon=True).start()
            return True
        if action == "deliver_order":
            bot.answer_callback_query(call.id)
            prompt = (
                "📤 <b>Выдача заказа вручную</b>\n\n"
                "Введите номер заказа FunPay, например:\n"
                "<code>Z8U62P1Z</code>\n\n"
                "Плагин отправит покупателю ссылку частями (3 сообщения + инструкция).\n"
                "<code>/cancel</code> — отмена"
            )
            result = bot.send_message(chat_id, prompt, parse_mode="HTML")
            if self.cardinal.telegram:
                self.cardinal.telegram.set_state(
                    chat_id, 0, call.from_user.id,
                    state=f"{CB_PREFIX}:deliver:wait",
                )
            return True
        if action == "start_import":
            mode = self.stock_mode()
            if arg in ("lot", "warehouse", "gemini_archive"):
                target = arg
            elif mode == "gemini_links":
                target = "gemini_archive"
            elif mode == "auto_buy":
                target = "warehouse"
            else:
                target = "lot"
            bot.answer_callback_query(call.id)
            prompt = self.start_import_mode(chat_id, call.from_user.id, target)
            bot.send_message(chat_id, prompt, parse_mode="HTML")
            if self.cardinal.telegram:
                self.cardinal.telegram.set_state(
                    chat_id, 0, call.from_user.id,
                    state=f"{CB_PREFIX}:import:wait:{target}",
                )
            return True

        if action == "release_warehouse":
            if self._import_running:
                bot.answer_callback_query(call.id, "Уже выполняется…", show_alert=True)
                return True
            wh_qty = int(self.get_cfg("warehouse_release_qty", 0) or 0)
            if wh_qty <= 0:
                bot.answer_callback_query(call.id, "Количество = 0. Укажите в настройках.", show_alert=True)
                return True
            bot.answer_callback_query(call.id, f"Выкладываю {wh_qty} шт…")
            threading.Thread(
                target=self.release_warehouse_to_lot, args=(chat_id, wh_qty), daemon=True,
            ).start()
            return True
        return False

    def _run_autobuy_from_ui(self, chat_id: int) -> None:
        allowed, reason = self._autobuy_allowed()
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        if not allowed:
            if bot:
                bot.send_message(chat_id, f"⛔ {_escape(reason)}", parse_mode="HTML")
            return
        self.run_autobuy(notify_chat_id=chat_id)


    # ── Telegram UI (schema-driven, как Starvell) ────────────────────────────

    def setup_telegram(self) -> None:
        if not self.cardinal.telegram:
            self.log("Telegram недоступен — UI не зарегистрирован")
            return
        tg = self.cardinal.telegram
        bot = tg.bot
        plugin = self

        def show_settings(
            chat_id: int, msg_id: int, page: str = "hub",
            fast: bool = False, instant: bool = False,
        ) -> None:
            text = plugin.render_settings_text(page, fast=fast, instant=instant)
            kb = plugin.build_settings_keyboard(page, fast=fast, instant=instant)
            try:
                bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

        def refresh_settings_ui(chat_id: int, msg_id: int, page: str = "hub") -> None:
            plugin._refresh_settings_ui_bg(chat_id, msg_id, page)

        def open_page_settings(chat_id: int, msg_id: int, page: str) -> None:
            show_settings(chat_id, msg_id, page, instant=True)
            threading.Thread(
                target=refresh_settings_ui, args=(chat_id, msg_id, page), daemon=True,
            ).start()

        def open_hub_settings(chat_id: int, msg_id: int) -> None:
            open_page_settings(chat_id, msg_id, "hub")

        def on_callback(call: CallbackQuery) -> None:
            data = call.data or ""
            if not data.startswith(f"{CB_PREFIX}:"):
                return
            parts = data.split(":")
            action = parts[1]
            chat_id, msg_id = call.message.chat.id, call.message.message_id

            if action == "noop":
                bot.answer_callback_query(call.id)
                return
            if action == "nav":
                page = parts[2] if len(parts) > 2 else "hub"
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                target = open_hub_settings if page == "hub" else (
                    lambda cid, mid: open_page_settings(cid, mid, page)
                )
                threading.Thread(
                    target=target, args=(chat_id, msg_id), daemon=True,
                ).start()
                return
            if action == "togkey":
                key = parts[2] if len(parts) > 2 else ""
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                if key:
                    plugin.set_cfg(key, not bool(plugin.get_cfg(key)))
                    plugin._invalidate_product_cache()
                    threading.Thread(
                        target=open_hub_settings, args=(chat_id, msg_id), daemon=True,
                    ).start()
                return
            if action == "editkey" and len(parts) >= 3:
                key = parts[2]
                field = plugin.get_schema_field(key)
                if not field:
                    bot.answer_callback_query(call.id)
                    return
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                cur = plugin.get_cfg(key, "")
                label = field.get("label", key)
                hint = ""
                if key == "buy_budget_usd":
                    hint = "\n\n<i>0 = потратить всё доступное после резерва</i>"
                elif key == "min_lot_stock":
                    hint = "\n\n<i>Если на лоте меньше — плагин докупит сам</i>"
                try:
                    preview = (
                        f"{float(cur):.2f}" if field.get("type") == "float"
                        else str(int(cur)) if field.get("type") == "int"
                        else str(cur)
                    )
                except (TypeError, ValueError):
                    preview = str(cur)
                prompt = (
                    f"✏️ <b>{_escape(label)}</b>\n\n"
                    f"Сейчас: <code>{_escape(preview)}</code>{hint}\n\n"
                    f"Введите новое значение.\n<code>/cancel</code> — отмена"
                )
                result = bot.send_message(chat_id, prompt, parse_mode="HTML")
                tg.set_state(
                    chat_id, result.id, call.from_user.id,
                    state=f"{CB_PREFIX}:edit:settings:{key}",
                )
                return
            if action == "tog" and len(parts) >= 4:
                ui_page, idx_s = parts[2], parts[3]
                field = plugin._field_by_page_index(ui_page, int(idx_s))
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                if field and field.get("type") == "bool":
                    key = field["key"]
                    plugin.set_cfg(key, not bool(plugin.get_cfg(key)))
                    if ui_page == "hub":
                        threading.Thread(
                            target=open_hub_settings, args=(chat_id, msg_id), daemon=True,
                        ).start()
                    else:
                        threading.Thread(
                            target=open_page_settings, args=(chat_id, msg_id, ui_page), daemon=True,
                        ).start()
                return
            if action == "mode" and len(parts) >= 3:
                new_mode = parts[2]
                if new_mode in _LEGACY_STOCK_MODES:
                    new_mode = "gemini_links"
                plugin.set_cfg("stock_mode", new_mode)
                if new_mode == "auto_buy":
                    plugin.start_auto_worker()
                try:
                    bot.answer_callback_query(call.id, plugin.stock_mode_label(new_mode)[:180])
                except Exception:
                    pass
                threading.Thread(
                    target=open_hub_settings, args=(chat_id, msg_id), daemon=True,
                ).start()
                return
            if action == "act":
                key = parts[2]
                arg = parts[3] if len(parts) > 3 else ""
                if plugin.on_settings_action(call, key, arg):
                    return
                bot.answer_callback_query(call.id)
                return
            if action == "import":
                sub = parts[2] if len(parts) > 2 else ""
                uid = call.from_user.id
                if sub.startswith("confirm"):
                    target = parts[3] if len(parts) > 3 else plugin._import_target.get(chat_id, "lot")
                    bot.answer_callback_query(call.id, "Выкладываю…")
                    threading.Thread(
                        target=plugin.confirm_pending_import, args=(chat_id, target), daemon=True,
                    ).start()
                    return
                if sub == "cancel":
                    plugin.cancel_import(chat_id, uid)
                    tg.clear_state(chat_id, uid)
                    bot.answer_callback_query(call.id, "Отменено")
                    try:
                        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
                    except Exception:
                        pass
                    return
                bot.answer_callback_query(call.id)
                return
            if action == "edit" and len(parts) >= 4:
                ui_page, idx_s = parts[2], parts[3]
                field = plugin._field_by_page_index(ui_page, int(idx_s))
                if not field:
                    bot.answer_callback_query(call.id)
                    return
                key = field["key"]
                ftype = field.get("type", "str")
                cur = plugin.get_cfg(key, "")
                label = field.get("label", key)
                if ftype == "multiline":
                    prompt = plugin._prompt_edit_intro(field, str(cur))
                else:
                    max_len = field.get("max_len", 500)
                    preview = _escape(str(cur)[:max_len])
                    if len(str(cur)) > max_len:
                        preview += "…"
                    prompt = (
                        f"✏️ <b>{_escape(label)}</b>\n\nТекущее:\n<code>{preview}</code>\n\n"
                        f"Введите новое значение.\n/cancel — отмена"
                    )
                result = bot.send_message(chat_id, prompt, parse_mode="HTML")
                tg.set_state(
                    chat_id, result.id, call.from_user.id,
                    state=f"{CB_PREFIX}:edit:{ui_page}:{key}",
                )
                bot.answer_callback_query(call.id)

        def on_import_text(message: Message) -> None:
            state_data = tg.get_state(message.chat.id, message.from_user.id)
            state = str((state_data or {}).get("state", ""))
            if not state.startswith(f"{CB_PREFIX}:import:wait"):
                return
            target = _import_target_from_state(state)
            chat_id = message.chat.id
            uid = message.from_user.id
            plugin._import_target[chat_id] = target
            text = message.text or ""
            low = text.strip().lower()
            if low in ("/cancel", "отмена"):
                plugin.cancel_import(chat_id, uid)
                tg.clear_state(chat_id, uid)
                bot.reply_to(message, "❌ Импорт отменён")
                return
            if low in ("/done", "готово", "done"):
                tg.clear_state(chat_id, uid)
                plugin.process_import_buffer(chat_id, uid)
                return
            added = plugin._accumulate_import_text(chat_id, uid, text)
            bot.reply_to(
                message,
                f"✅ +{len(text)} симв. (всего <b>{added}</b>)\n"
                f"Ещё части или <code>/done</code>",
                parse_mode="HTML",
            )

        def on_import_document(message: Message) -> None:
            state_data = tg.get_state(message.chat.id, message.from_user.id)
            state = str((state_data or {}).get("state", ""))
            if not state.startswith(f"{CB_PREFIX}:import:wait"):
                return
            target = _import_target_from_state(state)
            doc = message.document
            if not doc:
                return
            chat_id = message.chat.id
            uid = message.from_user.id
            try:
                file_info = bot.get_file(doc.file_id)
                raw = bot.download_file(file_info.file_path)
                text = raw.decode("utf-8", errors="replace")
            except Exception as exc:
                bot.reply_to(message, f"❌ Не удалось прочитать файл: {_escape(exc)}", parse_mode="HTML")
                return
            plugin._import_buffers[(chat_id, uid)] = text
            plugin._import_target[chat_id] = target
            tg.clear_state(chat_id, uid)
            bot.reply_to(message, f"📄 Файл принят ({len(text)} симв.), разбираю…")
            plugin.process_import_buffer(chat_id, uid)

        def _is_importing(m: Message) -> bool:
            state_data = tg.get_state(m.chat.id, m.from_user.id)
            state = str((state_data or {}).get("state", ""))
            return state.startswith(f"{CB_PREFIX}:import:wait")

        def on_text(message: Message) -> None:
            state_data = tg.get_state(message.chat.id, message.from_user.id)
            if not state_data or "state" not in state_data:
                return
            state = state_data["state"]
            if not str(state).startswith(f"{CB_PREFIX}:edit:"):
                return
            state_parts = str(state).split(":", 3)
            ui_page = state_parts[2] if len(state_parts) > 3 else "settings"
            key = state_parts[-1]
            field = plugin.get_schema_field(key)
            if not field:
                tg.clear_state(message.chat.id, message.from_user.id)
                return
            text = message.text or ""
            if text.strip().lower() in ("/cancel", "отмена"):
                tg.clear_state(message.chat.id, message.from_user.id)
                bot.reply_to(message, "❌ Отменено")
                return
            if field.get("type") == "int":
                try:
                    val = int(text.strip())
                except ValueError:
                    bot.reply_to(message, "⚠️ Введите целое число")
                    return
                min_v = field.get("min", 0)
                max_v = field.get("max", 100)
                if val < min_v or val > max_v:
                    bot.reply_to(message, f"⚠️ Допустимо: {min_v}–{max_v}")
                    return
                plugin.set_cfg(key, val)
            elif field.get("type") == "float":
                try:
                    val = float(text.strip().replace(",", "."))
                except ValueError:
                    bot.reply_to(message, "⚠️ Введите число, например 10 или 5.50")
                    return
                min_v = float(field.get("min", 0))
                max_v = float(field.get("max", 100000))
                if val < min_v or val > max_v:
                    bot.reply_to(message, f"⚠️ Допустимо: {min_v}–{max_v}")
                    return
                plugin.set_cfg(key, val)
            elif field.get("type") == "multiline":
                max_len = int(field.get("max_len", MAX_PROMPT_LEN))
                if len(text) > max_len:
                    bot.reply_to(
                        message,
                        f"⚠️ Слишком длинно: {len(text)} / {max_len} символов. "
                        f"Сократите промпт или разбейте шаблон.",
                    )
                    return
                plugin.set_cfg(key, text)
            else:
                plugin.set_cfg(key, text.strip())
            tg.clear_state(message.chat.id, message.from_user.id)
            if field.get("type") == "multiline":
                bot.reply_to(
                    message,
                    f"✅ Сохранено: <b>{field.get('label', key)}</b> ({len(text)} симв.)",
                    parse_mode="HTML",
                )
            else:
                bot.reply_to(message, f"✅ Сохранено: <b>{field.get('label', key)}</b>", parse_mode="HTML")

        def _is_editing(m: Message) -> bool:
            state_data = tg.get_state(m.chat.id, m.from_user.id)
            if not state_data or "state" not in state_data:
                return False
            return str(state_data["state"]).startswith(f"{CB_PREFIX}:edit:")

        def _is_deliver_waiting(m: Message) -> bool:
            state_data = tg.get_state(m.chat.id, m.from_user.id)
            if not state_data or "state" not in state_data:
                return False
            return str(state_data["state"]) == f"{CB_PREFIX}:deliver:wait"

        def on_deliver_text(message: Message) -> None:
            chat_id = message.chat.id
            uid = message.from_user.id
            text = (message.text or "").strip()
            if text.lower() in ("/cancel", "отмена"):
                tg.clear_state(chat_id, uid)
                bot.reply_to(message, "❌ Отменено")
                return
            order_id = text.split()[0].strip().lstrip("#")
            if not order_id:
                bot.reply_to(message, "⚠️ Введите номер заказа, например <code>Z8U62P1Z</code>", parse_mode="HTML")
                return
            tg.clear_state(chat_id, uid)
            bot.reply_to(message, f"⏳ Выдаю заказ <b>#{_escape(order_id)}</b>…", parse_mode="HTML")

            def _run() -> None:
                try:
                    ok, msg = plugin.deliver_gemini_order(order_id, force=False, manual=True)
                except Exception as exc:
                    _boot_log(f"DELIVER #{order_id} EXC: {exc}")
                    ok, msg = False, f"❌ Ошибка: {exc}"
                try:
                    bot.send_message(chat_id, msg, parse_mode="HTML")
                except Exception:
                    bot.send_message(chat_id, msg)

            threading.Thread(target=_run, daemon=True, name=f"GlDeliverUI-{order_id}").start()

        tg.msg_handler(on_deliver_text, func=_is_deliver_waiting)

        _register_priority_cbq(
            tg,
            on_callback,
            lambda c: (c.data or "").startswith(f"{CB_PREFIX}:"),
        )
        tg.msg_handler(on_import_text, func=_is_importing)
        tg.msg_handler(on_import_document, content_types=["document"], func=_is_importing)
        tg.msg_handler(on_text, func=_is_editing)

        def send_panel(message: Message) -> None:
            chat_id = message.chat.id
            text_msg = plugin.render_settings_text("hub", instant=True)
            kb = plugin.build_settings_keyboard("hub", instant=True)
            sent = bot.send_message(chat_id, text_msg, reply_markup=kb, parse_mode="HTML")
            threading.Thread(
                target=refresh_settings_ui, args=(chat_id, sent.message_id, "hub"), daemon=True,
            ).start()

        def send_stock_cmd(message: Message) -> None:
            chat_id = message.chat.id
            threading.Thread(target=plugin.notify_stock_status, args=(chat_id,), daemon=True).start()

        def _tg_command_name(m: Message) -> str:
            t = getattr(m, "text", None) or ""
            if not t.startswith("/"):
                return ""
            return t.split()[0].split("@")[0][1:].lower()

        def _tg_matches(m: Message, *cmds: str) -> bool:
            cmd = _tg_command_name(m)
            return bool(cmd) and cmd in {c.lower().lstrip("/") for c in cmds}

        tg.msg_handler(send_panel, func=lambda m: _tg_matches(m, "gemini_link", "gl"))
        tg.msg_handler(send_stock_cmd, func=lambda m: _tg_matches(m, "gl_stock"))

        def send_deliver_cmd(message: Message) -> None:
            parts = (message.text or "").split()
            if len(parts) < 2:
                bot.reply_to(
                    message,
                    "Использование: <code>/gl_deliver NRWWY843</code>\n"
                    "Повтор: <code>/gl_deliver NRWWY843 force</code>",
                    parse_mode="HTML",
                )
                return
            order_id = parts[1].strip().lstrip("#")
            force = len(parts) > 2 and parts[2].casefold() in ("force", "f", "повтор")
            try:
                bot.reply_to(message, f"⏳ Выдаю заказ <b>#{_escape(order_id)}</b>…", parse_mode="HTML")
            except Exception:
                pass

            def _run() -> None:
                try:
                    ok, msg = plugin.deliver_gemini_order(order_id, force=force, manual=True)
                except Exception as exc:
                    _boot_log(f"DELIVER #{order_id} EXC: {exc}")
                    ok, msg = False, f"❌ Ошибка: {exc}"
                try:
                    bot.reply_to(message, msg, parse_mode="HTML")
                except Exception:
                    try:
                        bot.reply_to(message, msg)
                    except Exception:
                        pass

            threading.Thread(target=_run, daemon=True, name=f"GlDeliver-{order_id}").start()

        tg.msg_handler(send_deliver_cmd, func=lambda m: _tg_matches(m, "gl_deliver"))

        def send_debug(message: Message) -> None:
            bot = tg.bot
            lines = [
                f"🔧 <b>Gemini Link debug</b> v{VERSION}",
                f"UUID: <code>{UUID}</code>",
                f"new_order_event: <code>{_HAS_NEW_ORDER_EVENT}</code>",
                f"cbq_handlers: <b>{len(bot.callback_query_handlers)}</b>",
                f"boot_log: <code>{BOOT_LOG_FILE}</code>",
            ]
            if _plugin:
                lines.append(f"mode: <code>{_plugin.stock_mode()}</code>")
            patched = _patch_catch_all_handlers(bot)
            lines.append(f"catch_all_patched: <b>{patched}</b>")
            _ensure_gemini_settings_handler(bot, _gemini_link_open_settings)
            lines.append("settings_handler: <b>re-registered</b>")
            bot.reply_to(message, "\n".join(lines), parse_mode="HTML")
            _boot_log("gl_debug command")

        tg.msg_handler(send_debug, func=lambda m: _tg_matches(m, "gl_debug"))

        try:
            plugin.cardinal.add_telegram_commands(UUID, [
                ("gemini_link", "панель Gemini Link Auto", True),
                ("gl_stock", "склад Gemini Link", True),
                ("gl_deliver", "выдать ссылку по номеру заказа", True),
                ("gl_debug", "диагностика Gemini Link", True),
            ])
        except Exception as exc:
            plugin.log("add_telegram_commands: %s", exc)

        self.log("Telegram UI зарегистрирован (/gemini_link, /gl_stock, /gl_deliver, /gl_debug)")


# ═════════════════════════════════════════════════════════════════════════════
#  FunPay Cardinal bindings
# ═════════════════════════════════════════════════════════════════════════════

def bind_to_new_order(cardinal: Cardinal, event: NewOrderEvent) -> None:
    if _plugin is None:
        return

    def _run() -> None:
        try:
            _plugin.handle_new_order(event)
        except Exception as exc:
            oid = getattr(getattr(event, "order", None), "id", "?")
            _boot_log(f"ORDER #{oid} handler FAIL: {exc}")
            logger.error("%s handle_new_order #%s: %s", _P, oid, exc)
            logger.debug(traceback.format_exc())

    try:
        threading.Thread(
            target=_run,
            daemon=True,
            name=f"GeminiLinkOrder-{event.order.id}",
        ).start()
    except Exception as exc:
        logger.error("%s bind_to_new_order: %s", _P, exc)
        logger.debug(traceback.format_exc())



def init_plugin(cardinal: Cardinal) -> None:
    global _plugin
    _boot_log(f"PRE_INIT start v{VERSION}")
    try:
        _plugin = Plugin(cardinal)
        _plugin.ensure_telegram_handlers()
        _boot_log("PRE_INIT ensure_telegram_handlers OK")
    except Exception as exc:
        _boot_log(f"PRE_INIT FAIL: {exc}")
        logger.error("%s PRE_INIT: %s", _P, exc)
        logger.debug(traceback.format_exc())
        raise


def post_init_plugin(cardinal: Cardinal) -> None:
    if _plugin is None:
        _boot_log("POST_INIT skip: _plugin is None")
        return
    try:
        _plugin.ensure_telegram_handlers()
        if _plugin.stock_mode() == "gemini_links":
            _plugin.refresh_gemini_lots_async()
        elif _plugin.stock_mode() == "auto_buy":
            _plugin.start_auto_worker()
        _boot_log(f"POST_INIT OK mode={_plugin.stock_mode()}")
        logger.info("%s v%s загружен (%s)", _P, VERSION, _plugin.stock_mode_label())
    except Exception as exc:
        _boot_log(f"POST_INIT FAIL: {exc}")
        logger.error("%s POST_INIT: %s", _P, exc)
        logger.debug(traceback.format_exc())


def pre_start_plugin(cardinal: Cardinal) -> None:
    if _plugin is None:
        return
    try:
        _plugin.ensure_telegram_handlers()
        _boot_log("PRE_START ensure_telegram_handlers OK")
    except Exception as exc:
        _boot_log(f"PRE_START FAIL: {exc}")
        logger.error("%s PRE_START: %s", _P, exc)


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_POST_INIT = [post_init_plugin]
BIND_TO_PRE_START = [pre_start_plugin]
BIND_TO_NEW_ORDER = [bind_to_new_order] if _HAS_NEW_ORDER_EVENT else []
BIND_TO_INIT_ORDER: list = []  # только NEW_ORDER; ручная выдача — /gl_deliver


_boot_log(f"module imported v{VERSION} new_order={_HAS_NEW_ORDER_EVENT}")
