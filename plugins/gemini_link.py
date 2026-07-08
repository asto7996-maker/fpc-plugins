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
from typing import Any, Final

from cardinal import Cardinal
from tg_bot import CBT
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
VERSION       = "2.2.0"
DESCRIPTION   = "Автозакупка gemini_18m или купленные ссылки → автовыдача FunPay"
CREDITS       = "Cursor AI"
UUID          = "f7a2e8c1-4b3d-4e9f-a8c2-1d5e9b0f6a3c"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

MAX_PROMPT_LEN:   Final[int] = 4000
PROMPT_PREVIEW_LEN: Final[int] = 250
DEFAULT_LOT_MATCH: Final[str] = "GPT plus 1M (NW)"
DEFAULT_SUPPLIER_PRODUCT: Final[str] = "gemini_18m"
AUTOBUY_MAX_PARALLEL: Final[int] = 8
SETTINGS_FILE     = f"storage/plugins/{UUID}/settings.json"
AUTOBUY_LOG_FILE  = f"storage/plugins/{UUID}/autobuy.json"
IMPORT_LOG_FILE   = f"storage/plugins/{UUID}/import_stock.json"
WAREHOUSE_FILE    = f"storage/plugins/{UUID}/warehouse.json"
CB_PREFIX         = f"glnk_{UUID[:8]}"

STOCK_MODES: Final[dict[str, str]] = {
    "auto_buy": "🤖 Автозакупка → FunPay",
    "stocked": "📦 Купленные ссылки → FunPay",
}
STOCK_MODE_ORDER: Final[tuple[str, ...]] = ("auto_buy", "stocked")
_LEGACY_STOCK_MODES: Final[frozenset[str]] = frozenset({"import_lot", "warehouse"})

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


def _escape(val: Any) -> str:
    return html.escape(str(val if val is not None else ""))


def _dig(data: Any, *keys: str, default: Any = None) -> Any:
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _extract_accounts_from_payload(data: Any) -> list[str]:
    """Достаёт строки аккаунтов из типовых ответов API поставщиков."""
    found: list[str] = []

    def add_line(val: Any) -> None:
        if val is None:
            return
        if isinstance(val, str):
            s = val.strip()
            if s and s not in found:
                found.append(s)
            return
        if isinstance(val, dict):
            parts = [
                str(val.get(k, "")).strip()
                for k in ("account", "data", "item", "content", "login", "email", "user")
                if val.get(k)
            ]
            if "password" in val and parts:
                parts.append(str(val["password"]).strip())
            if "pass" in val and len(parts) == 1:
                parts.append(str(val["pass"]).strip())
            if len(parts) >= 2:
                line = ":".join(parts[:3])
            elif parts:
                line = parts[0]
            else:
                line = ""
            if line and line not in found:
                found.append(line)

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            for chunk in re.split(r"[\r\n]+", node):
                chunk = chunk.strip()
                if chunk and chunk not in found:
                    found.append(chunk)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for key in (
                "accounts", "account", "items", "data", "goods", "products",
                "content", "result", "secrets", "stock", "lines",
            ):
                if key in node:
                    walk(node[key])
            if not found:
                for val in node.values():
                    if isinstance(val, (list, dict, str)):
                        walk(val)
            if not found and {"login", "password"} <= set(node.keys()):
                add_line(node)
            elif not found and {"email", "password"} <= set(node.keys()):
                add_line(node)

    walk(data)
    return [a for a in found if len(a) >= 3]


def _supplier_request(
    api_url: str,
    api_key: str,
    product_id: str,
    product_name: str,
    quantity: int,
    mode: str,
) -> tuple[list[str], str]:
    """Закупка аккаунтов у поставщика. Возвращает (строки аккаунтов, инфо/ошибка)."""
    api_url = (api_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    product_id = (product_id or "").strip()
    product_name = (product_name or DEFAULT_SUPPLIER_PRODUCT).strip()
    quantity = max(0, int(quantity))
    if quantity <= 0:
        return [], "количество 0 — закупка отключена"

    if not api_url:
        return [], "URL API поставщика не задан"
    if not api_key:
        return [], "API-ключ поставщика не задан"

    mode = (mode or "json_post").lower()
    last_err = "неизвестная ошибка"

    if mode == "smm_v2":
        params = {
            "key": api_key,
            "action": "buy",
            "service": product_id or product_name,
            "quantity": quantity,
        }
        try:
            resp = http_get(f"{api_url}", params=params, timeout=90)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            accounts = _extract_accounts_from_payload(data)
            if not accounts and isinstance(data, dict):
                order_id = _dig(data, "order") or _dig(data, "order_id")
                if order_id:
                    for _ in range(30):
                        time.sleep(2)
                        st = http_get(
                            api_url,
                            params={"key": api_key, "action": "status", "order": order_id},
                            timeout=60,
                        )
                        st_data = st.json()
                        accounts = _extract_accounts_from_payload(st_data)
                        if accounts:
                            break
            if accounts:
                return accounts[:quantity], f"куплено {len(accounts[:quantity])} шт."
            last_err = resp.text[:300] or f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = str(exc)
        return [], last_err

    if mode == "gemini_worker":
        base = api_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }
        payloads = (
            {"product_id": product_id or product_name, "product": product_id or product_name, "quantity": quantity},
            {"productId": product_id or product_name, "count": quantity},
        )
        endpoints = ("/api/purchase", "/api/buy", "/purchase", "/buy")
        for ep in endpoints:
            for body in payloads:
                try:
                    resp = http_post(f"{base}{ep}", json=body, headers=headers, timeout=90)
                    try:
                        data = resp.json()
                    except Exception:
                        data = resp.text
                    if isinstance(data, dict) and data.get("success") is False:
                        last_err = str(data.get("error") or data.get("message") or data)[:300]
                        continue
                    accounts = _extract_accounts_from_payload(data)
                    if accounts:
                        return accounts[:quantity], f"куплено {len(accounts[:quantity])} шт."
                    last_err = (resp.text or str(data))[:300]
                except Exception as exc:
                    last_err = str(exc)
        return [], last_err or "gemini_worker: не удалось купить"

    if mode == "get_query":
        params = {
            "api_key": api_key,
            "key": api_key,
            "product_id": product_id,
            "product": product_id or product_name,
            "name": product_name,
            "count": quantity,
            "quantity": quantity,
        }
        try:
            resp = http_get(api_url, params={k: v for k, v in params.items() if v}, timeout=90)
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            accounts = _extract_accounts_from_payload(data)
            if accounts:
                return accounts[:quantity], f"куплено {len(accounts[:quantity])} шт."
            last_err = resp.text[:300] or f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = str(exc)
        return [], last_err

    body = {
        "key": api_key,
        "api_key": api_key,
        "action": "buy",
        "product_id": product_id,
        "product": product_id or product_name,
        "name": product_name,
        "service": product_id or product_name,
        "count": quantity,
        "quantity": quantity,
    }
    try:
        resp = http_post(api_url, json=body, timeout=90)
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        if isinstance(data, dict) and data.get("success") is False:
            return [], str(data.get("error") or data.get("message") or data)[:300]
        accounts = _extract_accounts_from_payload(data)
        if accounts:
            return accounts[:quantity], f"куплено {len(accounts[:quantity])} шт."
        last_err = (resp.text or str(data))[:300]
    except Exception as exc:
        last_err = str(exc)
    return [], last_err


def _supplier_buy_parallel(
    api_url: str,
    api_key: str,
    product_id: str,
    product_name: str,
    quantity: int,
    mode: str,
    parallel: int,
    bulk: bool,
) -> tuple[list[str], str]:
    if bulk:
        return _supplier_request(api_url, api_key, product_id, product_name, quantity, mode)
    accounts: list[str] = []
    errors: list[str] = []
    workers = max(1, min(parallel, AUTOBUY_MAX_PARALLEL, quantity))
    lock = threading.Lock()

    def buy_one() -> None:
        got, info = _supplier_request(api_url, api_key, product_id, product_name, 1, mode)
        with lock:
            if got:
                accounts.extend(got)
            else:
                errors.append(info)

    threads = [threading.Thread(target=buy_one, daemon=True) for _ in range(quantity)]
    batch_size = workers
    for i in range(0, len(threads), batch_size):
        chunk = threads[i:i + batch_size]
        for t in chunk:
            t.start()
        for t in chunk:
            t.join()
    if accounts:
        return accounts[:quantity], f"куплено {len(accounts[:quantity])} шт. (параллельно)"
    return [], errors[0] if errors else "не удалось купить"


def _lot_text_matches(text: str, needle: str) -> bool:
    if not text or not needle:
        return False
    return needle.casefold() in text.casefold()


def _normalize_activation_url(url: str) -> str:
    return url.strip().rstrip("-").rstrip("=")


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


def _parse_import_routes(raw: str) -> list[tuple[str, str]]:
    """Строки вида «Gemini 18m | GPT plus 1M (NW)» или «… | #12345»."""
    routes: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            left, right = line.split("|", 1)
        elif "=>" in line:
            left, right = line.split("=>", 1)
        else:
            continue
        product_key = left.strip()
        lot_ref = right.strip()
        if product_key and lot_ref:
            routes.append((product_key, lot_ref))
    return routes

# ═════════════════════════════════════════════════════════════════════════════
#  Plugin (архитектура StarvellPlugin)
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
        self.reload_settings()

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
                sm = str(loaded.get("stock_mode", "stocked"))
                if sm in _LEGACY_STOCK_MODES:
                    loaded["stock_mode"] = "stocked"
                if not loaded.get("autobuy_enabled"):
                    loaded["autobuy_auto_restock"] = False
                try:
                    if int(loaded.get("autobuy_quantity", 0)) <= 0:
                        loaded["autobuy_auto_restock"] = False
                except (TypeError, ValueError):
                    pass
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
        if key == "autobuy_enabled" and not value:
            self._cfg["autobuy_auto_restock"] = False
            self._save_settings()
        if key == "autobuy_quantity":
            try:
                if int(value) <= 0:
                    self._cfg["autobuy_auto_restock"] = False
                    self._save_settings()
            except (TypeError, ValueError):
                pass
        if key == "stock_mode" and str(value) == "stocked":
            self._cfg["autobuy_auto_restock"] = False
            self._save_settings()

    @staticmethod
    def _default_cfg() -> dict[str, Any]:
        return {
            "autobuy_enabled": False,
            "autobuy_quantity": 0,
            "autobuy_lot_match": DEFAULT_LOT_MATCH,
            "autobuy_lot_id": "",
            "supplier_api_url": "",
            "supplier_api_key": "",
            "supplier_product_id": "gemini_18m",
            "supplier_product_name": DEFAULT_SUPPLIER_PRODUCT,
            "supplier_mode": "gemini_worker",
            "supplier_bulk": True,
            "supplier_parallel": 4,
            "account_line_template": "{account}",
            "import_routes": (
                "Gemini 18m | GPT plus 1M (NW)\n"
                "Gemini 18m links | GPT plus 1M (NW)"
            ),
            "import_line_template": "{url}",
            "import_skip_duplicates": True,
            "imported_order_ids": [],
            "stock_mode": "stocked",
            "warehouse_release_qty": 5,
            "autobuy_auto_restock": False,
            "autobuy_min_lot_stock": 0,
            "_cfg_version": "2.2.0",
        }

    # ── UI: компактные страницы вместо длинного списка кнопок ───────────────

    @staticmethod
    def _ui_pages() -> dict[str, dict[str, Any]]:
        return {
            "hub": {"title": "🎛 Главная", "emoji": "🏠"},
            "autobuy": {"title": "🛒 API / лот", "emoji": "🛒"},
        }


    def _autobuy_fields(self) -> list[dict[str, Any]]:
        return [
            {"key": "autobuy_enabled", "label": "Автозакупка включена", "type": "bool"},
            {"key": "autobuy_quantity", "label": "Покупать за раз (0 = выкл.)", "type": "int", "min": 0, "max": 100},
            {"key": "autobuy_auto_restock", "label": "Докупать при нехватке на лоте", "type": "bool"},
            {"key": "autobuy_min_lot_stock", "label": "Мин. остаток на лоте", "type": "int", "min": 0, "max": 100},
            {"key": "autobuy_lot_match", "label": "Метка в описании лота", "type": "text"},
            {"key": "autobuy_lot_id", "label": "ID лота (быстрее)", "type": "text"},
            {"key": "supplier_api_url", "label": "URL API поставщика", "type": "text"},
            {"key": "supplier_api_key", "label": "API-ключ поставщика", "type": "text"},
            {"key": "supplier_product_id", "label": "ID товара (gemini_18m)", "type": "text"},
            {"key": "supplier_product_name", "label": "Название товара", "type": "text"},
            {"key": "supplier_mode", "label": "Режим API", "type": "text"},
            {"key": "supplier_bulk", "label": "Одним запросом (bulk)", "type": "bool"},
            {"key": "supplier_parallel", "label": "Параллельность", "type": "int", "min": 1, "max": AUTOBUY_MAX_PARALLEL},
            {"key": "import_routes", "label": "Маршруты товар→лот", "type": "multiline", "max_len": 2000},
            {"key": "import_line_template", "label": "Шаблон ссылки в АВ", "type": "text"},
            {"key": "import_skip_duplicates", "label": "Пропускать дубли заказов", "type": "bool"},
            {"key": "warehouse_release_qty", "label": "Выложить со склада (шт.)", "type": "int", "min": 0, "max": 100},
        ]


    def get_settings_schema(self) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        for f in self._autobuy_fields():
            fields.append({**f, "default": self._default_cfg().get(f["key"])})
        fields.extend([
            {"key": "run_autobuy", "label": "🛒 Закупить", "type": "action"},
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

    def _resolve_autobuy_lot_ids(self, silent: bool = False) -> list[int]:
        lot_id_raw = str(self.get_cfg("autobuy_lot_id", "")).strip()
        if lot_id_raw.isdigit():
            return [int(lot_id_raw)]
        needle = str(self.get_cfg("autobuy_lot_match", DEFAULT_LOT_MATCH)).strip()
        if not needle:
            return []
        profile = self.cardinal.profile
        if not profile:
            try:
                profile = self.cardinal.account.get_user(self.cardinal.account.id)
            except Exception as exc:
                if not silent:
                    self.log("profile error: %s", exc)
                return []
        matched: list[int] = []
        for lot in profile.get_lots():
            desc = str(getattr(lot, "description", "") or "")
            title = str(getattr(lot, "title", "") or desc)
            if _lot_text_matches(desc, needle) or _lot_text_matches(title, needle):
                try:
                    matched.append(int(lot.id))
                except (TypeError, ValueError):
                    continue
        if not matched and not silent:
            self.log("лоты с меткой %r не найдены", needle)
        return matched[:1] if len(matched) > 1 else matched

    def _format_account_lines(self, accounts: list[str]) -> list[str]:
        tpl = str(self.get_cfg("account_line_template", "{account}") or "{account}")
        lines: list[str] = []
        for acc in accounts:
            acc = acc.strip()
            if not acc:
                continue
            if "{account}" in tpl:
                line = tpl.replace("{account}", acc)
            elif "{login}" in tpl and ":" in acc:
                parts = acc.split(":")
                line = tpl.replace("{login}", parts[0])
                if "{password}" in tpl and len(parts) > 1:
                    line = line.replace("{password}", parts[1])
                if "{email}" in tpl and len(parts) > 2:
                    line = line.replace("{email}", parts[2])
            else:
                line = acc
            if line not in lines:
                lines.append(line)
        return lines

    def _append_accounts_to_lot(self, lot_id: int, accounts: list[str]) -> tuple[bool, str]:
        lines = self._format_account_lines(accounts)
        return self._append_stock_lines_to_lot(lot_id, lines)

    def _append_stock_lines_to_lot(self, lot_id: int, lines: list[str]) -> tuple[bool, str]:
        lines = [ln.strip() for ln in lines if ln and ln.strip()]
        if not lines:
            return False, "нет данных для выдачи"
        acc = self.cardinal.account
        for attempt in range(3):
            try:
                lf = acc.get_lot_fields(int(lot_id))
                before = len(lf.secrets)
                existing = set(lf.secrets)
                for line in lines:
                    if line not in existing:
                        lf.secrets.append(line)
                        existing.add(line)
                lf.auto_delivery = True
                if not lf.active and lf.secrets:
                    lf.active = True
                lf.renew_fields()
                acc.save_lot(lf)
                added = len(lf.secrets) - before
                return True, f"лот #{lot_id}: +{added} шт., всего {len(lf.secrets)}"
            except Exception as exc:
                self.log("save_lot #%s attempt %s: %s", lot_id, attempt + 1, exc)
                time.sleep(1.5)
        return False, f"не удалось сохранить лот #{lot_id}"

    def _purchase_accounts(self, quantity: int) -> tuple[list[str], str]:
        return _supplier_buy_parallel(
            str(self.get_cfg("supplier_api_url", "")),
            str(self.get_cfg("supplier_api_key", "")),
            str(self.get_cfg("supplier_product_id", "")),
            str(self.get_cfg("supplier_product_name", DEFAULT_SUPPLIER_PRODUCT)),
            quantity,
            str(self.get_cfg("supplier_mode", "json_post")),
            int(self.get_cfg("supplier_parallel", 4)),
            bool(self.get_cfg("supplier_bulk", True)),
        )

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

    def run_autobuy(self, notify_chat_id: int | None = None) -> None:
        if self._autobuy_running:
            return
        allowed, reason = self._autobuy_allowed()
        if not allowed:
            bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
            if bot and notify_chat_id:
                bot.send_message(notify_chat_id, f"⛔ {_escape(reason)}", parse_mode="HTML")
            self.log("autobuy blocked: %s", reason)
            return
        self._autobuy_running = True
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        qty = int(self.get_cfg("autobuy_quantity", 0))
        match = str(self.get_cfg("autobuy_lot_match", DEFAULT_LOT_MATCH))
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        try:
            if bot and notify_chat_id:
                bot.send_message(
                    notify_chat_id,
                    f"🛒 <b>Закупка {qty} шт.</b> <code>{_escape(match)}</code>\n⏳ Покупаю у поставщика…",
                    parse_mode="HTML",
                )
            accounts, buy_info = self._purchase_accounts(qty)
            if not accounts:
                msg = f"❌ <b>Закупка не удалась</b>\n<code>{_escape(buy_info)}</code>"
                self.log("autobuy fail: %s", buy_info)
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, msg, parse_mode="HTML")
                self._log_autobuy({"time": started, "ok": False, "error": buy_info, "qty": qty})
                return

            lot_ids = self._resolve_autobuy_lot_ids()
            if not lot_ids:
                msg = (
                    f"✅ Куплено <b>{len(accounts)}</b> шт., но лот не найден.\n"
                    f"Укажите <b>ID лота</b> или метку <code>{_escape(match)}</code> в описании."
                )
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, msg, parse_mode="HTML")
                self._log_autobuy({"time": started, "ok": False, "bought": len(accounts), "error": "no lot"})
                return

            results: list[str] = []
            ok = True
            for lot_id in lot_ids:
                success, info = self._append_accounts_to_lot(lot_id, accounts)
                results.append(info)
                ok = ok and success

            summary = (
                f"{'✅' if ok else '⚠️'} <b>Готово за {int(time.time() - t0)}с</b>\n"
                f"🛒 Куплено: <b>{len(accounts)}</b> — {buy_info}\n"
                + "\n".join(f"📦 {_escape(r)}" for r in results)
            )
            self.log("autobuy ok: %s accounts -> %s", len(accounts), lot_ids)
            if bot and notify_chat_id:
                bot.send_message(notify_chat_id, summary, parse_mode="HTML")
            self._log_autobuy({
                "time": started, "ok": ok, "bought": len(accounts),
                "lots": lot_ids, "results": results,
            })
        except Exception as exc:
            logger.error("%s autobuy: %s", _P, exc)
            if bot and notify_chat_id:
                bot.send_message(
                    notify_chat_id,
                    f"❌ Ошибка закупки: <code>{_escape(exc)}</code>",
                    parse_mode="HTML",
                )
        finally:
            self._autobuy_running = False

    def notify_stock_status(self, chat_id: int) -> None:
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
                lines.append(
                    f"• Лот <b>#{lot_id}</b>: <b>{len(lf.secrets)}</b> шт. "
                    f"({'🟢 автовыдача' if lf.auto_delivery else '🔴 без АВ'})"
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
        profile = self.cardinal.profile
        if not profile:
            try:
                profile = self.cardinal.account.get_user(self.cardinal.account.id)
            except Exception:
                return []
        matched: list[int] = []
        for lot in profile.get_lots():
            desc = str(getattr(lot, "description", "") or "")
            title = str(getattr(lot, "title", "") or desc)
            if _lot_text_matches(desc, needle) or _lot_text_matches(title, needle):
                try:
                    matched.append(int(lot.id))
                except (TypeError, ValueError):
                    continue
        if len(matched) > 1 and not silent:
            self.log("несколько лотов для %r, берём первый: %s", needle, matched[0])
        return matched[:1]

    def _resolve_lot_for_product(self, product: str) -> list[int]:
        product_l = (product or "").casefold()
        for key, lot_ref in _parse_import_routes(str(self.get_cfg("import_routes", ""))):
            if key.casefold() in product_l or (product_l and product_l in key.casefold()):
                lots = self._resolve_lot_by_match(lot_ref)
                if lots:
                    return lots
        return self._resolve_autobuy_lot_ids(silent=True)

    def _format_import_lines(self, items: list[dict[str, str]]) -> list[str]:
        tpl = str(self.get_cfg("import_line_template", "{url}") or "{url}")
        lines: list[str] = []
        for item in items:
            line = (
                tpl.replace("{url}", item.get("url", ""))
                .replace("{order_id}", item.get("order_id", ""))
                .replace("{product}", item.get("product", ""))
                .strip()
            )
            if line and line not in lines:
                lines.append(line)
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
        dest = "на <b>склад</b>" if target == "warehouse" else "на <b>лот FunPay</b>"
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
        btn = "на склад" if target == "warehouse" else "на FunPay"
        lines.append(f"\n<b>Подтвердить выкладку {btn}?</b>")
        return "\n".join(lines)

    def _import_confirm_keyboard(self, target: str = "lot") -> IKM:
        kb = IKM()
        label = "✅ На склад" if target == "warehouse" else "✅ На FunPay"
        kb.row(
            IKB(label, callback_data=f"{CB_PREFIX}:import:confirm:{target}"),
            IKB("❌ Отмена", callback_data=f"{CB_PREFIX}:import:cancel"),
        )
        return kb

    def start_import_mode(self, chat_id: int, user_id: int, target: str = "lot") -> str:
        target = target if target in ("lot", "warehouse") else "lot"
        self._import_buffers[(chat_id, user_id)] = ""
        self._import_target[chat_id] = target
        if target == "warehouse":
            dest = (
                "Ссылки попадут на <b>локальный склад</b> (уже купленные).\n"
                "Потом выложите на FunPay режимом <b>📦 Со склада → лот</b>."
            )
        else:
            dest = "Ссылки сразу выложатся на <b>автовыдачу лота FunPay</b>."
        return (
            "📥 <b>Загрузка закупки</b>\n\n"
            f"{dest}\n\n"
            "Вставьте <b>Purchase History</b> (частями или файлом <code>.txt</code>).\n"
            "Бот найдёт ссылки активации Google.\n\n"
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
        all_items = parse_shop_purchase_history(raw)
        fresh, skipped = self._filter_new_import_items(all_items)
        skipped += skipped_dup
        self._import_buffers.pop(key, None)
        if not fresh:
            bot.send_message(
                chat_id,
                f"❌ Ссылки не найдены"
                f"{f' (дублей: {skipped})' if skipped else ''}.\n"
                "Проверьте формат: блоки <code>Order #…</code> и строка <code>Data: https://…</code>",
                parse_mode="HTML",
            )
            return
        self._import_pending[chat_id] = fresh
        target = self._import_target.get(chat_id, "lot")
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
                    f"⚠️ <b>{len(unresolved)}</b> ссылок без лота — настройте "
                    f"<b>Маршруты товар→лот</b> или <b>ID лота</b>."
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
        mode = str(self.get_cfg("stock_mode", "stocked") or "stocked")
        if mode in _LEGACY_STOCK_MODES:
            return "stocked"
        return mode if mode in STOCK_MODES else "stocked"

    def _autobuy_allowed(self) -> tuple[bool, str]:
        if not bool(self.get_cfg("autobuy_enabled", False)):
            return False, "Автозакупка выключена. Включите переключатель на главной."
        try:
            qty = int(self.get_cfg("autobuy_quantity", 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            return False, "Количество = 0. Закупка отключена."
        if not str(self.get_cfg("supplier_api_url", "")).strip():
            return False, "Не задан URL API поставщика."
        if not str(self.get_cfg("supplier_api_key", "")).strip():
            return False, "Не задан API-ключ поставщика."
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
                    "Настройте <b>Маршруты товар→лот</b> или <b>ID лота</b>."
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
        if ftype == "int":
            return f"• <b>{label}</b>: <code>{_escape(val)}</code>"
        if ftype == "action":
            return f"▶️ <b>{label}</b>"
        preview = _escape(str(val or "")[:55])
        if len(str(val or "")) > 55:
            preview += "…"
        return f"• <b>{label}</b>: <code>{preview or '—'}</code>"

    def _fields_for_page(self, page: str) -> list[dict[str, Any]]:
        if page == "autobuy":
            return self._autobuy_fields()
        return []

    def _lot_stock_info(self) -> str:
        lot_ids = self._resolve_autobuy_lot_ids(silent=True)
        if not lot_ids:
            return "лот не найден"
        try:
            lf = self.cardinal.account.get_lot_fields(int(lot_ids[0]))
            return f"{len(lf.secrets)} шт. (лот #{lot_ids[0]})"
        except Exception:
            return "—"

    def render_settings_text(self, page: str = "hub") -> str:
        pages = self._ui_pages()
        page = page if page in pages else "hub"
        qty = int(self.get_cfg("autobuy_quantity", 5))
        match = str(self.get_cfg("autobuy_lot_match", DEFAULT_LOT_MATCH))
        lines = [
            f"⚙️ <b>{_escape(NAME)}</b> v{VERSION}",
            "━━━━━━━━━━━━━━━━━━",
            f"<i>{_escape(DESCRIPTION)}</i>",
            "",
        ]
        if page == "hub":
            mode = self.stock_mode()
            try:
                ab_qty = int(self.get_cfg("autobuy_quantity", 0))
            except (TypeError, ValueError):
                ab_qty = 0
            wh_qty = int(self.get_cfg("warehouse_release_qty", 5) or 0)
            ab_on = bool(self.get_cfg("autobuy_enabled"))
            lines += [
                f"<b>Режим:</b> {self.stock_mode_label(mode)}",
                f"📦 <b>На лоте FunPay:</b> {self._lot_stock_info()}",
                f"🗄 <b>Купленные (склад):</b> {self.warehouse_count()} шт.",
            ]
            if mode == "auto_buy":
                ab_state = "🟢 ВКЛ" if ab_on and ab_qty > 0 else "🔴 ВЫКЛ"
                lines.append(f"🤖 <b>Автозакупка:</b> {ab_state} | за раз: <b>{ab_qty}</b> шт.")
                if ab_qty <= 0:
                    lines.append("⛔ <i>Количество 0 — закупка полностью отключена</i>")
            else:
                lines.append(f"📤 <b>Выложить со склада:</b> {wh_qty} шт. за раз")
            if self._autobuy_running:
                lines.append("⏳ <i>Закупка…</i>")
            if self._import_running:
                lines.append("⏳ <i>Выкладка…</i>")
            return "\n".join(lines)

        title = pages[page]["title"]
        lines.append(f"<b>{title}</b>\n")
        for field in self._fields_for_page(page):
            val = self.get_cfg(field["key"])
            lines.append(self._format_setting_line(field, val))
        if page == "autobuy":
            lines.append(f"\n📦 <b>На лоте FunPay:</b> {self._lot_stock_info()}")
            lines.append(f"🗄 <b>На складе:</b> {self.warehouse_count()} шт.")
            lines.append(f"<b>Режим:</b> {self.stock_mode_label()}")
            lines.append(
                f"🔎 Ищем лоты с меткой <code>{_escape(match)}</code> "
                f"или ID <code>{_escape(str(self.get_cfg('autobuy_lot_id', '') or '—'))}</code>"
            )
        return "\n".join(lines)

    def build_settings_keyboard(self, page: str = "hub") -> IKM:
        kb = IKM()
        page = page if page in self._ui_pages() else "hub"

        if page == "hub":
            mode = self.stock_mode()
            kb.row(
                IKB(
                    f"{'• ' if mode == 'auto_buy' else ''}🤖 Автозакупка",
                    callback_data=f"{CB_PREFIX}:mode:auto_buy",
                ),
                IKB(
                    f"{'• ' if mode == 'stocked' else ''}📦 Купленные",
                    callback_data=f"{CB_PREFIX}:mode:stocked",
                ),
            )
            if mode == "auto_buy":
                ab_on = bool(self.get_cfg("autobuy_enabled"))
                ab_qty = int(self.get_cfg("autobuy_quantity", 0) or 0)
                kb.row(IKB(
                    f"{'🟢' if ab_on and ab_qty > 0 else '🔴'} Автозакупка: "
                    f"{'ВКЛ' if ab_on and ab_qty > 0 else 'ВЫКЛ'}",
                    callback_data=f"{CB_PREFIX}:togkey:autobuy_enabled",
                ))
                if ab_on and ab_qty > 0:
                    kb.row(IKB(
                        f"🤖 Купить {ab_qty} шт → FunPay",
                        callback_data=f"{CB_PREFIX}:act:run_autobuy",
                    ))
                kb.row(IKB("⚙️ API поставщика", callback_data=f"{CB_PREFIX}:nav:autobuy"))
            else:
                wh_qty = int(self.get_cfg("warehouse_release_qty", 5) or 0)
                kb.row(IKB("📥 Загрузить Purchase History", callback_data=f"{CB_PREFIX}:act:start_import:warehouse"))
                if wh_qty > 0:
                    kb.row(IKB(
                        f"📤 Выложить {wh_qty} шт → FunPay",
                        callback_data=f"{CB_PREFIX}:act:release_warehouse",
                    ))
            kb.row(IKB("📊 Остатки", callback_data=f"{CB_PREFIX}:act:stock_status"))
        else:
            fields = self._fields_for_page(page)
            for i, field in enumerate(fields):
                key = field["key"]
                label = field.get("label", key)
                ftype = field.get("type", "str")
                if ftype == "bool":
                    on = bool(self.get_cfg(key))
                    kb.add(IKB(
                        f"{'🟢' if on else '🔴'} {label[:40]}",
                        callback_data=f"{CB_PREFIX}:tog:{page}:{i}",
                    ))
                else:
                    val = str(self.get_cfg(key, "")).replace("\n", " ")[:14]
                    if len(str(self.get_cfg(key, ""))) > 14:
                        val += "…"
                    kb.add(IKB(
                        f"✏️ {label[:24]}: {val or '—'}",
                        callback_data=f"{CB_PREFIX}:edit:{page}:{i}",
                    ))
            if page == "autobuy":
                ab_on = bool(self.get_cfg("autobuy_enabled"))
                ab_qty = int(self.get_cfg("autobuy_quantity", 0) or 0)
                kb.row(IKB(
                    f"{'🟢' if ab_on else '🔴'} Автозакупка: {'ВКЛ' if ab_on else 'ВЫКЛ'}",
                    callback_data=f"{CB_PREFIX}:togkey:autobuy_enabled",
                ))
                if ab_on and ab_qty > 0:
                    kb.row(IKB(f"🤖 Купить {ab_qty} шт", callback_data=f"{CB_PREFIX}:act:run_autobuy"))
                kb.row(IKB("📊 Остатки", callback_data=f"{CB_PREFIX}:act:stock_status"))
            kb.row(IKB("🏠 Главная", callback_data=f"{CB_PREFIX}:nav:hub"))

        kb.add(IKB("◀️ К плагину", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
        return kb







    # ── Order helpers ────────────────────────────────────────────────────────




























    def on_settings_action(self, call: CallbackQuery, action: str, arg: str = "") -> bool:
        bot = self.cardinal.telegram.bot
        chat_id = call.message.chat.id





        if action == "run_autobuy":
            allowed, reason = self._autobuy_allowed()
            if not allowed:
                bot.answer_callback_query(call.id, reason[:180], show_alert=True)
                return True
            if self._autobuy_running:
                bot.answer_callback_query(call.id, "Уже выполняется…", show_alert=True)
                return True
            qty = int(self.get_cfg("autobuy_quantity", 0))
            bot.answer_callback_query(call.id, f"Закупка {qty} шт…")
            threading.Thread(target=self.run_autobuy, args=(chat_id,), daemon=True).start()
            return True
        if action == "stock_status":
            bot.answer_callback_query(call.id, "Смотрю склад…")
            threading.Thread(target=self.notify_stock_status, args=(chat_id,), daemon=True).start()
            return True
        if action == "start_import":
            target = arg if arg in ("lot", "warehouse") else "warehouse"
            if self.stock_mode() == "stocked":
                target = "warehouse"
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


    # ── Telegram UI (schema-driven, как Starvell) ────────────────────────────

    def setup_telegram(self) -> None:
        if not self.cardinal.telegram:
            return
        tg = self.cardinal.telegram
        bot = tg.bot
        plugin = self

        def show_settings(chat_id: int, msg_id: int, page: str = "hub") -> None:
            text = plugin.render_settings_text(page)
            kb = plugin.build_settings_keyboard(page)
            try:
                bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

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
                show_settings(chat_id, msg_id, parts[2] if len(parts) > 2 else "hub")
                bot.answer_callback_query(call.id)
                return
            if action == "togkey":
                key = parts[2] if len(parts) > 2 else ""
                if key:
                    plugin.set_cfg(key, not bool(plugin.get_cfg(key)))
                    show_settings(chat_id, msg_id, "hub")
                bot.answer_callback_query(call.id)
                return
            if action == "tog" and len(parts) >= 4:
                ui_page, idx_s = parts[2], parts[3]
                field = plugin._field_by_page_index(ui_page, int(idx_s))
                if field and field.get("type") == "bool":
                    key = field["key"]
                    plugin.set_cfg(key, not bool(plugin.get_cfg(key)))
                    show_settings(chat_id, msg_id, ui_page)
                bot.answer_callback_query(call.id)
                return
            if action == "mode" and len(parts) >= 3:
                plugin.set_cfg("stock_mode", parts[2])
                show_settings(chat_id, msg_id, "hub")
                bot.answer_callback_query(call.id, plugin.stock_mode_label(parts[2])[:180])
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
            target = "warehouse" if state.endswith(":warehouse") else "lot"
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
            target = "warehouse" if state.endswith(":warehouse") else "lot"
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
            ui_page = state_parts[2] if len(state_parts) > 3 else "autobuy"
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
                min_v = field.get("min", 1)
                max_v = field.get("max", 50)
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

        def on_plugin_settings(call: CallbackQuery) -> None:
            if f"{CBT.PLUGIN_SETTINGS}:{UUID}" not in (call.data or ""):
                if not (call.data or "").startswith(f"{CBT.EDIT_PLUGIN}:{UUID}"):
                    return
            show_settings(call.message.chat.id, call.message.message_id, "hub")
            bot.answer_callback_query(call.id)

        def _is_editing(m: Message) -> bool:
            state_data = tg.get_state(m.chat.id, m.from_user.id)
            if not state_data or "state" not in state_data:
                return False
            return str(state_data["state"]).startswith(f"{CB_PREFIX}:edit:")

        tg.cbq_handler(on_callback, lambda c: (c.data or "").startswith(f"{CB_PREFIX}:"))
        tg.cbq_handler(on_plugin_settings, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))
        tg.msg_handler(on_import_text, func=_is_importing)
        tg.msg_handler(on_import_document, content_types=["document"], func=_is_importing)
        tg.msg_handler(on_text, func=_is_editing)

        def send_panel(message: Message) -> None:
            chat_id = message.chat.id
            text_msg = plugin.render_settings_text("hub")
            kb = plugin.build_settings_keyboard("hub")
            bot.send_message(chat_id, text_msg, reply_markup=kb, parse_mode="HTML")

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

        self.log("Telegram UI зарегистрирован (/gemini_link, /gl_stock)")


# ═════════════════════════════════════════════════════════════════════════════
#  FunPay Cardinal bindings
# ═════════════════════════════════════════════════════════════════════════════

def init_plugin(cardinal: Cardinal) -> None:
    global _plugin
    _plugin = Plugin(cardinal)
    _plugin.setup_telegram()
    logger.info("%s v%s загружен", _P, VERSION)


BIND_TO_PRE_INIT = [init_plugin]
