from __future__ import annotations
import html as _html
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import deque, Counter
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING


def _ensure_deps():
    try:
        import requests  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "requests"])


_ensure_deps()

import requests
import telebot
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
from tg_bot import CBT
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
from FunPayAPI.common.enums import MessageTypes, SubCategoryTypes
from FunPayAPI.types import LotFields

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "KOSell Rent"
VERSION = "1.1.29b"
DESCRIPTION = "Официальный плагин KOSell.store"
CREDITS = "@KOSell1"
UUID = "0d8a4c2f-6e71-4b3a-9c4d-2f1e8a7b6c50"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.kosell_rent")
LP = "[KOSellRent]"

API_BASE = "https://www.kosell.store/api/v1"
TIMEOUT = 25

PLUGIN_DIR = "storage/plugins/kosell_rent"
os.makedirs(PLUGIN_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
MAPPINGS_FILE = os.path.join(PLUGIN_DIR, "mappings.json")
RENTALS_FILE = os.path.join(PLUGIN_DIR, "rentals.json")
HIDDEN_FILE = os.path.join(PLUGIN_DIR, "hidden.json")
HANDLED_FILE = os.path.join(PLUGIN_DIR, "handled_orders.json")

ORDER_URL = "https://funpay.com/orders/{order_id}/"

DEFAULT_TEXTS = {
    "text_delivery": (
        "🎮 Ваш арендованный аккаунт ({game}):\n\n"
        "👤 Логин: {login}\n"
        "🔑 Пароль: {password}\n\n"
        "⏳ Аренда на {hours} ч.\n"
        "🕒 Действует до: {expires}\n\n"
        "🔐 Steam Guard код: напишите команду !код {login}\n"
        "Приятной игры!"
    ),
    "text_extension": (
        "✅ Аренда аккаунта {login} ({game}) продлена на {hours} ч.\n"
        "🕒 Новое время окончания: {expires}"
    ),
    "text_friend_activated": (
        "✅ Режим «Для друга» активен {minutes} мин.\n"
        "Оплатите лот ещё раз — бот выдаст новый отдельный аккаунт, а не продлит текущий."
    ),
    "text_friend_already": "✅ Режим «Для друга» уже активен.",
    "text_code": "🔐 Steam Guard код для {login}: {code}\nДействует ~{ttl} сек.",
    "text_code_ask_login": (
        "У вас несколько аккаунтов. Укажите логин: !код логин\n{logins}"
    ),
    "text_code_not_found": "❌ Активный аккаунт не найден. Если только что оплатили — подождите минуту.",
    "text_ask_which_account": (
        "У вас несколько аккаунтов для {game}. Какой продлить?\n"
        "Напишите: !прод логин\n{logins}"
    ),
    "text_extend_applied": "✅ Продление применено для {login} на {hours} ч.\n🕒 До: {expires}",
    "text_extend_no_pending": "Нет ожидающих продлений. Купите лот, затем используйте !прод логин.",
    "text_review_bonus": "🎉 Спасибо за отзыв! Аккаунт продлён на {hours} ч.\n🕒 До: {expires}",
    "text_partial": (
        "✅ Выдано аккаунтов: {delivered} из {ordered}.\n"
        "Остальные временно недоступны — продавец уведомлён и довыдаст их в ближайшее время."
    ),
    "text_problem": (
        "⚠️ Что-то пошло не так с автоматической выдачей. "
        "Продавец уже уведомлён и скоро вам поможет. Спасибо за ожидание!"
    ),
    "text_rental_ended": (
        "⌛️ Аренда аккаунта ({game}) завершена. Спасибо, что выбрали нас!\n\n"
        "Если всё понравилось — оставьте, пожалуйста, отзыв 💚 Это очень помогает нам "
        "держать низкие цены и радовать вас. Ждём снова!"
    ),
}

DEFAULT_SETTINGS = {
    "enabled": True,
    "api_key": "",
    "currency": "RUB",
    "auto_hide_no_stock": False,
    "auto_hide_zero_balance": False,
    "auto_refund": False,
    "review_bonus_enabled": False,
    "notify_sales": False,
    "friend_minutes": 10,
    "review_bonus_hours": 3,
    "review_bonus_min_stars": 5,
    "balance_threshold": 0,
    "notify_rental_end": True,
    "tz_offset_hours": 3,
    "poll_sec": 30,
    "extend_cooldown": 30,
    "texts": {},
    "map_url": "https://www.kosell.store/downloads/kosell_funpay_map.json",
    "ac_usd_rub": 95.0,
    "ac_subcat_cap": 15,
    "ac_min_price": 15,
    "ac_template": None,
}

MAP_FILE = os.path.join(PLUGIN_DIR, "funpay_map.json")
MISC_GAMES_FILE = os.path.join(PLUGIN_DIR, "misc_funpay_games.json")
NAME_OVERRIDES_FILE = os.path.join(PLUGIN_DIR, "name_overrides.json")
MAP_URLS = [
    "https://www.kosell.store/downloads/kosell_funpay_map.json",
    "https://kosell.store/downloads/kosell_funpay_map.json",
    "https://ru.kosell.store/downloads/kosell_funpay_map.json",
]
PH_TIME, PH_GAME = "KOSTIME", "KOSGAME"
KOSELL_MIN_HOURS = 1
UPDATE_URLS = [
    "https://www.kosell.store/downloads/KOSellRent.py",
    "https://kosell.store/downloads/KOSellRent.py",
]
PLUGIN_INFO_URLS = [
    "https://www.kosell.store/api/plugin/info",
    "https://kosell.store/api/plugin/info",
    "https://ru.kosell.store/api/plugin/info",
]
UPDATE_CHECK_SEC = 1800
SELF_PATH = os.path.abspath(__file__)
UPDATE_PENDING_FILE = os.path.join(PLUGIN_DIR, "update_pending.json")
AC_DEBUG = False
AC_CREATE_DELAY = 2.5
AC_DELETE_DELAY = 0.4
FP_LIMITS_HINT = (
    "Лимиты FunPay: краткое описание EN — до ~70 символов; "
    "на подкатегорию обычно ~8–10 лотов (зависит от аккаунта). "
    "Дублирующие лоты в одном разделе запрещены."
)
DURATION_PRESETS = {
    "short": [3, 10, 24],
    "standard": [3, 24, 72, 168],
    "full": [3, 24, 72, 168, 360, 720],
}

SETTINGS: dict = {}
MAPPINGS: list = []
RENTALS: dict = {}
HIDDEN: dict = {}
HANDLED: list = []

cardinal_instance: "Cardinal | None" = None
tg = None
bot: "telebot.TeleBot | None" = None

_lock = threading.Lock()
_poll_stop = threading.Event()
_poll_thread: "threading.Thread | None" = None

_friend: dict = {}           # buyer_key -> activated_at (ts)
_pending_ext: dict = {}      # buyer_key -> {hours, order_id, product_id, allowed_logins, ts}
_ext_last: dict = {}         # buyer_key -> ts (cooldown)
_processed_msgs: set = set()
_processed_review: set = set()
_admin_alert_dedup: dict = {}
_balance_alerted = {"low": False}
_last_stock_avail: dict = {}   # product_id -> available_accounts (мгновенная реакция на изменение)
_hidden_dirty: bool = False

_map_cache: dict = {"data": None, "ts": 0}
_misc_cache: dict = {}
_ac_wizard: dict = {}
_ac_plan: dict = {}          # user_id -> {"items": [...]}
_ac_running: dict = {"on": False}
_ac_lock = threading.Lock()
_update_state: dict = {"pending_restart": False, "latest": None}


def _ac_begin(title: str) -> bool:
    with _ac_lock:
        if _ac_running.get("on"):
            return False
        _ac_running.clear()
        _ac_running.update({"on": True, "title": title, "done": 0, "total": 0,
                            "created": 0, "skipped": 0, "failed": 0, "started": None})
        return True
_commission_cache: dict = {}  # subcat_id -> float
_products_cache: dict = {"data": None, "ts": 0, "err": False}   # кэш KOSell /rental/products
_pub_lots_cache: dict = {}    # subcat_id -> {"ts": ts, "lots": [(price, desc, server), ...]}
_price_cache: dict = {}       # (product_id, hours) -> {"ts": ts, "rub": float}
_node_form_cache: dict = {}   # subcat_id -> {"ts": ts, "form": {...}}
AC_CACHE_TTL = 900           # 15 минут


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"{LP} save {path} failed: {e}")


def load_all():
    global SETTINGS, MAPPINGS, RENTALS, HIDDEN, HANDLED
    SETTINGS = {**DEFAULT_SETTINGS, **_load_json(SETTINGS_FILE, {})}
    SETTINGS["texts"] = {**DEFAULT_TEXTS, **(SETTINGS.get("texts") or {})}
    # Разовая миграция: ускоряем проверку наличия (старый дефолт был 60с).
    if not SETTINGS.get("_poll_interval_migrated"):
        try:
            if int(SETTINGS.get("poll_sec", 30) or 30) >= 60:
                SETTINGS["poll_sec"] = 30
        except Exception:
            SETTINGS["poll_sec"] = 30
        SETTINGS["_poll_interval_migrated"] = True
        save_settings()
    mp = _load_json(MAPPINGS_FILE, [])
    MAPPINGS = mp if isinstance(mp, list) else []
    RENTALS = _load_json(RENTALS_FILE, {})
    HIDDEN = _load_json(HIDDEN_FILE, {})
    h = _load_json(HANDLED_FILE, [])
    HANDLED = h if isinstance(h, list) else []
    known = {str(m.get("lot_id")) for m in MAPPINGS if m.get("lot_id")}
    orphans = [lid for lid in HIDDEN if lid not in known]
    if orphans:
        for lid in orphans:
            HIDDEN.pop(lid, None)
        save_hidden()
        logger.info(f"{LP} Очищено осиротевших скрытых лотов: {len(orphans)}")
    logger.info(f"{LP} Loaded: mappings={len(MAPPINGS)}, rentals_buyers={len(RENTALS)}")


def save_settings():
    _save_json(SETTINGS_FILE, SETTINGS)


def save_mappings():
    _save_json(MAPPINGS_FILE, MAPPINGS)


def save_rentals():
    _save_json(RENTALS_FILE, RENTALS)


def save_hidden():
    global _hidden_dirty
    _save_json(HIDDEN_FILE, HIDDEN)
    _hidden_dirty = False


def _flush_hidden():
    global _hidden_dirty
    if _hidden_dirty:
        save_hidden()


def save_handled():
    _save_json(HANDLED_FILE, HANDLED[-500:])


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def get_text(key: str) -> str:
    return (SETTINGS.get("texts") or {}).get(key) or DEFAULT_TEXTS.get(key, "")


def fmt(key: str, **kwargs) -> str:
    try:
        return get_text(key).format_map(_SafeDict(**kwargs))
    except Exception:
        return get_text(key)


def _parse_iso(value) -> float:
    if not value:
        return 0.0
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _tz_label(offset: int) -> str:
    if offset == 3:
        return "МСК"
    if offset == 0:
        return "UTC"
    return f"UTC{'+' if offset >= 0 else ''}{offset}"


def _fmt_expires(value) -> str:
    ts = _parse_iso(value)
    if not ts:
        return str(value or "—")
    try:
        offset = int(SETTINGS.get("tz_offset_hours", 3) or 0)
    except Exception:
        offset = 3
    try:
        tz = timezone(timedelta(hours=offset))
        return datetime.fromtimestamp(ts, tz=tz).strftime("%d.%m.%Y %H:%M ") + _tz_label(offset)
    except Exception:
        return str(value)


class KOSellAPI:
    def __init__(self, api_key: str):
        self.key = api_key
        self.s = requests.Session()
        self.s.headers.update({
            "X-API-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._calls = deque()

    def _throttle(self):
        now = time.time()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= 58:
            wait = 60 - (now - self._calls[0]) + 0.5
            if wait > 0:
                logger.warning(f"{LP} [API] self-throttle, wait {wait:.1f}s")
                time.sleep(wait)
        self._calls.append(time.time())

    def _req(self, method, path, **kwargs):
        kwargs.setdefault("timeout", TIMEOUT)
        url = f"{API_BASE}{path}"
        for attempt in range(3):
            self._throttle()
            try:
                r = self.s.request(method, url, **kwargs)
            except requests.RequestException as e:
                logger.warning(f"{LP} [API] {method} {path} net error (try {attempt+1}): {e}")
                if attempt == 2:
                    return None
                time.sleep(2)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5") or 5)
                logger.warning(f"{LP} [API] 429 on {path}, wait {wait}s")
                time.sleep(wait)
                continue
            return r
        return None

    @staticmethod
    def _err_code(r) -> str:
        if r is None:
            return "connection_error"
        try:
            j = r.json()
            if isinstance(j, dict):
                return str(j.get("error") or j.get("detail") or f"http_{r.status_code}")
        except Exception:
            pass
        return f"http_{r.status_code}"

    def balance(self):
        r = self._req("GET", "/account/balance")
        if r is not None and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None

    def products(self, search: "str | None" = None, currency: str = "RUB"):
        path = f"/rental/products?currency={requests.utils.quote(currency)}"
        if search:
            path += f"&search={requests.utils.quote(search)}"
        r = self._req("GET", path)
        if r is not None and r.status_code == 200:
            try:
                data = r.json()
                return data if isinstance(data, list) else []
            except Exception:
                return None
        return None

    def calculate_price(self, product_id: int, hours: int):
        body = {"product_id": int(product_id), "hours": int(hours)}
        r = self._req("POST", "/rental/calculate-price", json=body)
        if r is not None and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None

    def rent(self, product_id: int, hours: int, currency: str):
        body = {"product_id": int(product_id), "duration_hours": int(hours), "currency": currency}
        r = self._req("POST", "/rental/rent", json=body)
        if r is not None and r.status_code in (200, 201):
            try:
                return r.json(), None
            except Exception:
                return None, "bad_response"
        return None, self._err_code(r)

    def credentials(self, uid: str):
        r = self._req("GET", f"/rental/{uid}/credentials")
        if r is not None and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None

    def code(self, uid: str):
        r = self._req("GET", f"/rental/{uid}/code")
        if r is not None and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None

    def extend(self, uid: str, hours: int, currency: str):
        body = {"hours": int(hours), "currency": currency}
        r = self._req("POST", f"/rental/{uid}/extend", json=body)
        if r is not None and r.status_code in (200, 201):
            try:
                return r.json(), None
            except Exception:
                return None, "bad_response"
        return None, self._err_code(r)

    def active(self):
        r = self._req("GET", "/rental/active")
        if r is not None and r.status_code == 200:
            try:
                data = r.json()
                return data if isinstance(data, list) else []
            except Exception:
                return None
        return None


def _sanitize_key(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] in "\"'`" and s[-1] == s[0]:
        s = s[1:-1].strip()
    return "".join(ch for ch in s if ch.isprintable() and not ch.isspace())


def _get_api() -> "KOSellAPI | None":
    key = _sanitize_key(SETTINGS.get("api_key", ""))
    if not key:
        return None
    return KOSellAPI(key)


def _dbg(msg: str):
    if SETTINGS.get("ac_debug"):
        logger.info(f"{LP} [AC-DEBUG] {msg}")


def _products(api: "KOSellAPI | None", force: bool = False):
    if api is None:
        return None
    now = time.time()
    if (not force and _products_cache["data"] is not None
            and now - _products_cache["ts"] < AC_CACHE_TTL):
        return _products_cache["data"]
    data = api.products()
    if data is None:
        _products_cache["err"] = True
        _dbg("products(): ошибка API (None) — отдаю прошлый кэш если есть")
        return _products_cache["data"] if _products_cache["data"] is not None else None
    _products_cache.update({"data": data, "ts": now, "err": False})
    _dbg(f"products(): загружено {len(data)} товаров, кэш на {AC_CACHE_TTL//60} мин")
    return data


def _bar(done: int, total: int, width: int = 14) -> str:
    total = max(1, total)
    frac = min(1.0, done / total)
    fill = int(round(frac * width))
    return f"{'█' * fill}{'░' * (width - fill)} {int(frac * 100)}% ({done}/{total})"


def _send(chat_id, text: str):
    if cardinal_instance and chat_id and text:
        try:
            cardinal_instance.send_message(chat_id, text, watermark=False)
        except Exception as e:
            logger.error(f"{LP} send to {chat_id} failed: {e}")


def notify_buyer_problem(chat_id):
    _send(chat_id, get_text("text_problem"))


def _admins():
    if cardinal_instance and cardinal_instance.telegram:
        return list(cardinal_instance.telegram.authorized_users)
    return []


def notify_admins(reason: str, order=None, raw: str = "", extra: str = "", dedup_key: "str | None" = None):
    if dedup_key:
        last = _admin_alert_dedup.get(dedup_key, 0)
        if time.time() - last < 60:
            return
        _admin_alert_dedup[dedup_key] = time.time()

    lines = [f"<b>{LP}</b>", f"⚠️ {_html.escape(reason)}"]
    kb = None
    if order is not None:
        oid = getattr(order, "id", "?")
        buyer = getattr(order, "buyer_username", "?")
        lines.append(f"Покупатель: <b>{_html.escape(str(buyer))}</b>")
        lines.append(f"Заказ: <code>{_html.escape(str(oid))}</code>")
        desc = getattr(order, "description", "") or ""
        if desc:
            lines.append(f"Лот: {_html.escape(desc[:80])}")
        kb = K()
        kb.add(B("🔗 Открыть заказ", url=ORDER_URL.format(order_id=oid)))
    if extra:
        lines.append(_html.escape(extra))
    if raw:
        lines.append(f"<code>{_html.escape(raw[:200])}</code>")
    text = "\n".join(lines)
    if not bot:
        return
    for admin_id in _admins():
        try:
            bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


def notify_admins_info(text: str):
    if not bot:
        return
    for admin_id in _admins():
        try:
            bot.send_message(admin_id, f"{LP} {text}", parse_mode="HTML")
        except Exception:
            pass


_HUMAN_ERR = {
    "no_stock": "Нет свободных аккаунтов на KOSell",
    "insufficient_balance": "Недостаточно средств на балансе KOSell",
    "invalid_api_key": "Неверный API ключ KOSell",
    "account_disabled": "Аккаунт KOSell отключён",
    "rate_limited": "Превышен лимит запросов KOSell",
    "not_found": "Товар/аренда не найдены на KOSell",
    "connection_error": "Нет связи с KOSell",
}


def _human_err(code: str) -> str:
    return _HUMAN_ERR.get(code, f"Ошибка KOSell: {code}")


def _duration_in_desc(hours: int, desc_l: str) -> bool:
    if not desc_l or not hours:
        return False
    for lang in ("ru", "en"):
        t = _format_kostime(int(hours), lang).strip().lower()
        if t and t in desc_l:
            return True
    return False


def _match_mapping(order, lot_id) -> "dict | None":
    desc_l = (getattr(order, "description", "") or "").lower()
    subcat = getattr(getattr(order, "subcategory", None), "id", None)

    exact = None
    if lot_id:
        for m in MAPPINGS:
            if m.get("lot_id") and str(m["lot_id"]) == str(lot_id):
                exact = m
                break

    cands = []
    for m in MAPPINGS:
        kw = (m.get("title_keyword") or "").strip().lower()
        sc = m.get("subcategory_id")
        if sc and subcat and str(sc) != str(subcat):
            continue
        if kw:
            if kw in desc_l:
                cands.append(m)
        elif sc and subcat and str(sc) == str(subcat):
            cands.append(m)

    dur_cands = [m for m in (cands or MAPPINGS) if _duration_in_desc(int(m.get("hours") or 0), desc_l)
                 and (not (m.get("title_keyword") or "").strip()
                      or (m.get("title_keyword") or "").strip().lower() in desc_l)]

    if exact and (not desc_l or _duration_in_desc(int(exact.get("hours") or 0), desc_l)):
        return exact

    if dur_cands:
        if exact and exact in dur_cands:
            return exact
        if len(dur_cands) == 1:
            _dbg(f"_match_mapping: матч по длительности (lot_id={lot_id} не совпал/неверен) -> "
                 f"hours={dur_cands[0].get('hours')} {dur_cands[0].get('product_name')}")
            return dur_cands[0]

    if exact:
        return exact
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        logger.warning(f"{LP} order {getattr(order, 'id', '?')}: неоднозначная привязка "
                       f"(игра найдена, длительность из названия не распознана), пропуск во избежание неверной выдачи")
        return None
    return None


def _buyer_key(chat_id) -> str:
    return str(chat_id)


def _now() -> float:
    return time.time()


def _active_rentals(buyer_key: str) -> list:
    out = []
    for r in RENTALS.get(buyer_key, []):
        exp = r.get("expires_ts", 0)
        if not exp or exp > _now() - 60:
            out.append(r)
    return out


def _add_rental(buyer_key, rec: dict):
    RENTALS.setdefault(buyer_key, []).append(rec)
    save_rentals()


def _cleanup_rentals():
    changed = False
    for bk in list(RENTALS.keys()):
        kept = [r for r in RENTALS[bk] if (r.get("expires_ts", 0) or 0) > _now() - 3600]
        if len(kept) != len(RENTALS[bk]):
            RENTALS[bk] = kept
            changed = True
        if not kept:
            del RENTALS[bk]
            changed = True
    if changed:
        save_rentals()


def _friend_active(buyer_key: str) -> bool:
    ts = _friend.get(buyer_key)
    if not ts:
        return False
    if _now() - ts > SETTINGS.get("friend_minutes", 10) * 60:
        _friend.pop(buyer_key, None)
        return False
    return True


def _friend_set(buyer_key: str):
    _friend[buyer_key] = _now()


def _friend_clear(buyer_key: str):
    _friend.pop(buyer_key, None)


def _maybe_refund(order):
    if not SETTINGS.get("auto_refund"):
        return False
    oid = getattr(order, "id", None)
    if not oid or not cardinal_instance:
        return False
    try:
        cardinal_instance.account.refund(oid)
        notify_admins_info(f"💸 Выполнен авто-возврат по заказу {oid}")
        return True
    except Exception as e:
        notify_admins(f"Не удалось сделать авто-возврат: {e}", order=order, dedup_key=f"refund:{oid}")
        return False


def _do_new_rental(api, mapping, chat_id, buyer_name, order, hours: int, notify_fail: bool = True):
    product_id = mapping["product_id"]
    currency = mapping.get("currency") or SETTINGS.get("currency", "RUB")
    game = mapping.get("product_name") or "аккаунт"

    hours = max(KOSELL_MIN_HOURS, min(720, int(hours or 0)))
    data, err = api.rent(product_id, hours, currency)
    if err or not data:
        if err == "insufficient_balance":
            _balance_alerted["low"] = True
        if notify_fail:
            notify_buyer_problem(chat_id)
            notify_admins(f"Не удалось выдать аккаунт: {_human_err(err or 'unknown')}",
                          order=order, raw=str(err), dedup_key=f"rent:{err}")
            if err in ("no_stock", "insufficient_balance"):
                _maybe_refund(order)
        return False, err

    uid = data.get("rental_uid")
    login = data.get("steam_login", "")
    password = ""
    creds = api.credentials(uid)
    if creds:
        password = creds.get("steam_password", "")
    if not password:
        notify_admins("Аренда создана, но не удалось получить пароль (credentials)",
                      order=order, extra=f"uid={uid} login={login}", dedup_key=f"cred:{uid}")

    expires_raw = data.get("expires_at")
    rec = {
        "uid": uid,
        "product_id": product_id,
        "product_name": game,
        "login": login,
        "expires_raw": expires_raw,
        "expires_ts": _parse_iso(expires_raw),
        "lot_id": mapping.get("lot_id"),
        "order_id": getattr(order, "id", None),
        "created_at": _now(),
        "review_bonus_given": False,
    }
    _add_rental(_buyer_key(chat_id), rec)

    _send(chat_id, fmt("text_delivery", login=login, password=password, game=game,
                       hours=hours, expires=_fmt_expires(expires_raw)))
    if SETTINGS.get("notify_sales"):
        notify_admins_info(f"✅ Выдан аккаунт {login} ({game}) покупателю "
                           f"{getattr(order, 'buyer_username', '?')}, заказ {getattr(order, 'id', '?')}")
    return True, None


def _do_extend(api, mapping, chat_id, rental, order, hours: int):
    currency = mapping.get("currency") or SETTINGS.get("currency", "RUB")
    game = mapping.get("product_name") or rental.get("product_name") or "аккаунт"
    hours = max(KOSELL_MIN_HOURS, min(720, int(hours or 0)))
    data, err = api.extend(rental["uid"], hours, currency)
    if err or not data:
        notify_buyer_problem(chat_id)
        notify_admins(f"Не удалось продлить аренду: {_human_err(err or 'unknown')}",
                      order=order, extra=f"uid={rental['uid']}", raw=str(err), dedup_key=f"ext:{err}")
        if err == "insufficient_balance":
            _balance_alerted["low"] = True
        if err in ("no_stock", "insufficient_balance"):
            _maybe_refund(order)
        return False
    new_exp = data.get("new_expires_at")
    rental["expires_raw"] = new_exp
    rental["expires_ts"] = _parse_iso(new_exp)
    save_rentals()
    _send(chat_id, fmt("text_extension", login=rental.get("login", ""), game=game,
                       hours=hours, expires=_fmt_expires(new_exp)))
    if SETTINGS.get("notify_sales"):
        notify_admins_info(f"✅ Продлён {rental.get('login')} (+{hours} ч), заказ {getattr(order, 'id', '?')}")
    return True


def _process_order(cardinal: "Cardinal", order, lot_id):
    api = _get_api()
    if not api:
        notify_admins("Заказ получен, но API ключ KOSell не задан — выдача невозможна", order=order,
                      dedup_key="no_api")
        notify_buyer_problem(order.chat_id)
        return

    mapping = _match_mapping(order, lot_id)
    if not mapping:
        logger.info(f"{LP} order {order.id}: нет привязки (lot_id={lot_id})")
        return

    buyer_name = order.buyer_username or ""
    chat_obj = cardinal.account.get_chat_by_name(buyer_name, make_request=True)
    chat_id = chat_obj.id if chat_obj else order.chat_id

    qty = order.amount or 1
    hours_unit = int(mapping.get("hours") or 1)
    buyer_key = _buyer_key(chat_id)

    logger.info(f"{LP} order {order.id} matched product={mapping.get('product_id')} qty={qty} hours_unit={hours_unit}")

    if _friend_active(buyer_key):
        delivered = 0
        last_err = None
        for i in range(qty):
            ok, err = _do_new_rental(api, mapping, chat_id, buyer_name, order, hours_unit, notify_fail=False)
            if ok:
                delivered += 1
                if i < qty - 1:
                    time.sleep(3)
            else:
                last_err = err
                break
        if delivered == 0:
            notify_buyer_problem(chat_id)
            notify_admins(f"Режим друга: не удалось выдать ни одного аккаунта ({_human_err(last_err or 'unknown')})",
                          order=order, raw=str(last_err), dedup_key=f"friend:{last_err}")
            if last_err in ("no_stock", "insufficient_balance"):
                _maybe_refund(order)
        elif delivered < qty:
            _send(chat_id, fmt("text_partial", delivered=delivered, ordered=qty))
            notify_admins(f"Режим друга: выдано {delivered} из {qty} "
                          f"(не хватило: {_human_err(last_err or 'no_stock')})",
                          order=order, extra="Возврат части средств за недовыданные аккаунты можно оформить через тикет FunPay при необходимости.",
                          dedup_key=f"friendpartial:{getattr(order, 'id', '')}")
        return

    total_hours = hours_unit * qty
    existing = [r for r in _active_rentals(buyer_key) if str(r.get("product_id")) == str(mapping["product_id"])]

    if len(existing) == 0:
        _do_new_rental(api, mapping, chat_id, buyer_name, order, total_hours)
    elif len(existing) == 1:
        _do_extend(api, mapping, chat_id, existing[0], order, total_hours)
    else:
        logins = [r.get("login", "") for r in existing]
        _pending_ext[buyer_key] = {
            "hours": total_hours,
            "order_id": order.id,
            "product_id": mapping["product_id"],
            "allowed_logins": logins,
            "ts": _now(),
        }
        _send(chat_id, fmt("text_ask_which_account",
                           game=mapping.get("product_name") or "аккаунт",
                           logins="\n".join(f"• {l}" for l in logins)))


def on_new_order(cardinal: "Cardinal", event: NewOrderEvent):
    if not SETTINGS.get("enabled"):
        return
    order = event.order
    try:
        with _lock:
            if order.id in HANDLED:
                return
            HANDLED.append(order.id)
            save_handled()
        lot_id = getattr(event, "lot_id", None)
        if not lot_id:
            m = re.search(r'/(?:lots|chips)/offer\?id=(\d+)', getattr(order, "html", "") or "")
            lot_id = m.group(1) if m else None
        _process_order(cardinal, order, lot_id)
    except Exception as e:
        logger.error(f"{LP} on_new_order error: {e}", exc_info=True)
        try:
            notify_buyer_problem(order.chat_id)
            notify_admins("Внутренняя ошибка при обработке заказа", order=order, raw=str(e))
        except Exception:
            pass


def _cmd_friend(chat_id, buyer_key):
    if _friend_active(buyer_key):
        _send(chat_id, get_text("text_friend_already"))
        return
    _friend_set(buyer_key)
    _send(chat_id, fmt("text_friend_activated", minutes=SETTINGS.get("friend_minutes", 10)))


def _cmd_code(chat_id, buyer_key, arg):
    api = _get_api()
    if not api:
        _send(chat_id, get_text("text_code_not_found"))
        return
    rentals = _active_rentals(buyer_key)
    if not rentals:
        _send(chat_id, get_text("text_code_not_found"))
        return
    target = None
    if arg:
        for r in rentals:
            if (r.get("login") or "").lower() == arg.lower():
                target = r
                break
        if not target:
            _send(chat_id, get_text("text_code_not_found"))
            return
    else:
        if len(rentals) == 1:
            target = rentals[0]
        else:
            logins = "\n".join(f"• {r.get('login', '')}" for r in rentals)
            _send(chat_id, fmt("text_code_ask_login", logins=logins))
            return
    data = api.code(target["uid"])
    if not data or not data.get("code"):
        _send(chat_id, get_text("text_code_not_found"))
        notify_admins("Не удалось получить Steam Guard код", extra=f"uid={target['uid']} login={target.get('login')}",
                      dedup_key=f"code:{target['uid']}")
        return
    ttl = int(data.get("expires_in", 30) or 30)
    if ttl < 5:
        _send(chat_id, "⏳ Секунду, готовлю свежий код…")
        time.sleep(ttl + 1)
        fresh = api.code(target["uid"])
        if fresh and fresh.get("code"):
            data = fresh
            ttl = int(data.get("expires_in", 30) or 30)
    _send(chat_id, fmt("text_code", login=target.get("login", ""),
                       code=data.get("code"), ttl=ttl))


def _cmd_extend(chat_id, buyer_key, arg, buyer_name):
    last = _ext_last.get(buyer_key, 0)
    if _now() - last < SETTINGS.get("extend_cooldown", 30):
        return
    pending = _pending_ext.get(buyer_key)
    if not pending or not arg:
        _send(chat_id, get_text("text_extend_no_pending"))
        return
    if arg.lower() not in [l.lower() for l in pending.get("allowed_logins", [])]:
        _send(chat_id, get_text("text_extend_no_pending"))
        return
    rentals = _active_rentals(buyer_key)
    target = None
    for r in rentals:
        if (r.get("login") or "").lower() == arg.lower():
            target = r
            break
    if not target:
        _send(chat_id, get_text("text_extend_no_pending"))
        return
    api = _get_api()
    if not api:
        notify_buyer_problem(chat_id)
        return
    _ext_last[buyer_key] = _now()
    mapping = next((m for m in MAPPINGS if str(m.get("product_id")) == str(pending["product_id"])), {})
    fake_order = type("O", (), {"id": pending.get("order_id"), "buyer_username": buyer_name,
                                "chat_id": chat_id, "description": ""})()
    if _do_extend(api, mapping or {"product_id": pending["product_id"]}, chat_id, target, fake_order, pending["hours"]):
        _pending_ext.pop(buyer_key, None)


def _handle_buyer_text(chat_id, text: str, buyer_name=None):
    if not SETTINGS.get("enabled"):
        return
    text = (text or "").strip()
    if not text:
        return
    low = text.lower()
    buyer_key = _buyer_key(chat_id)

    dkey = f"{chat_id}:{low[:40]}:{int(_now()) // 5}"
    if dkey in _processed_msgs:
        return
    _processed_msgs.add(dkey)
    if len(_processed_msgs) > 500:
        _processed_msgs.clear()

    if low in ("!friend", "!друг"):
        _cmd_friend(chat_id, buyer_key)
    elif low.startswith("!код") or low.startswith("!code"):
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else None
        _cmd_code(chat_id, buyer_key, arg)
    elif low.startswith("!прод") or low.startswith("!prod"):
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else None
        _cmd_extend(chat_id, buyer_key, arg, buyer_name)


_RX_REVIEW_GIVEN = re.compile(
    r'(Покупатель|The buyer).+(оставил отзыв|написал отзыв|изменил отзыв|has given feedback|has changed feedback)',
    re.IGNORECASE)
_RX_STARS = re.compile(r'(\d)\s*(?:звезд|зірк|star|★)', re.IGNORECASE)


def _handle_review(msg):
    if not SETTINGS.get("review_bonus_enabled"):
        return
    text = str(getattr(msg, "text", "") or "")
    if not _RX_REVIEW_GIVEN.search(text) and "★" not in text:
        return
    dedup = f"{msg.chat_id}:{getattr(msg, 'id', '')}"
    if dedup in _processed_review:
        return
    _processed_review.add(dedup)
    if len(_processed_review) > 500:
        _processed_review.clear()

    stars = None
    m = _RX_STARS.search(text)
    if m:
        try:
            stars = int(m.group(1))
        except Exception:
            stars = None
    if stars is None:
        stars = text.count("★") or None
    min_stars = SETTINGS.get("review_bonus_min_stars", 5)
    if stars is not None and stars < min_stars:
        return

    chat_id = msg.chat_id
    buyer_key = _buyer_key(chat_id)
    rentals = _active_rentals(buyer_key)
    if not rentals:
        return
    target = sorted(rentals, key=lambda r: r.get("expires_ts", 0), reverse=True)[0]
    if target.get("review_bonus_given"):
        return
    api = _get_api()
    if not api:
        return
    bonus_hours = SETTINGS.get("review_bonus_hours", 2)
    currency = SETTINGS.get("currency", "RUB")
    data, err = api.extend(target["uid"], bonus_hours, currency)
    if err or not data:
        notify_admins(f"Бонус за отзыв не начислен: {_human_err(err or 'unknown')}",
                      extra=f"uid={target['uid']}", dedup_key=f"revbonus:{err}")
        return
    new_exp = data.get("new_expires_at")
    target["expires_raw"] = new_exp
    target["expires_ts"] = _parse_iso(new_exp)
    target["review_bonus_given"] = True
    save_rentals()
    _send(chat_id, fmt("text_review_bonus", hours=bonus_hours, expires=_fmt_expires(new_exp)))


def _is_own_message(cardinal: "Cardinal", msg) -> bool:
    if getattr(msg, "by_bot", False):
        return True
    own_id = getattr(cardinal.account, "id", None)
    if own_id is not None and getattr(msg, "author_id", None) == own_id:
        return True
    own_name = getattr(cardinal.account, "username", None)
    if own_name and getattr(msg, "author", None) == own_name:
        return True
    return False


def on_new_message(cardinal: "Cardinal", event: NewMessageEvent):
    msg = event.message
    if not msg:
        return
    try:
        if getattr(msg, "type", None) != MessageTypes.NON_SYSTEM:
            _handle_review(msg)
            return
        if _is_own_message(cardinal, msg) or msg.author_id == 0 or getattr(msg, "i_am_buyer", False):
            return
        if not msg.text:
            return
        chat_name = getattr(msg, "chat_name", None) or getattr(msg, "author", None)
        _handle_buyer_text(msg.chat_id, msg.text, buyer_name=chat_name)
    except Exception as e:
        logger.error(f"{LP} on_new_message error: {e}", exc_info=True)


def on_last_chat_message_changed(cardinal: "Cardinal", event):
    chat = getattr(event, "chat", None)
    if not chat:
        return
    try:
        if getattr(chat, "last_by_bot", False) or not getattr(chat, "unread", False):
            return
        last = getattr(chat, "last_message_text", None)
        if not last:
            return
        _handle_buyer_text(chat.id, last, buyer_name=getattr(chat, "name", None))
    except Exception as e:
        logger.error(f"{LP} on_last_chat_message_changed error: {e}", exc_info=True)


_LOT_RETRY_WAITS = [6.0, 15.0, 20.0]


def _set_lot_active(lot_id, active: bool, fast: bool = False) -> bool:
    if not cardinal_instance:
        return False
    last_err = None
    attempts = len(_LOT_RETRY_WAITS) + 1
    for i in range(attempts):
        try:
            lf = cardinal_instance.account.get_lot_fields(int(lot_id))
            if lf.active == active:
                return True
            lf.active = active
            cardinal_instance.account.save_lot(lf)
            if not fast:
                time.sleep(0.5)
            return True
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                wait = _LOT_RETRY_WAITS[i]
                logger.warning(f"{LP} set_lot_active {lot_id}={active} попытка {i+1}/{attempts} не удалась: {e}. "
                               f"Жду {wait:.0f}с и повторяю.")
                time.sleep(wait)
    logger.error(f"{LP} set_lot_active {lot_id}={active} failed после {attempts} попыток: {last_err}")
    return False


def _hide_lot(lot_id, reason: str, persist: bool = True, fast: bool = False):
    global _hidden_dirty
    if str(lot_id) in HIDDEN:
        return False
    if _set_lot_active(lot_id, False, fast=fast):
        HIDDEN[str(lot_id)] = reason
        if persist:
            save_hidden()
        else:
            _hidden_dirty = True
        logger.info(f"{LP} lot {lot_id} hidden ({reason})")
        return True
    return False


def _restore_lot(lot_id, persist: bool = True, fast: bool = False):
    global _hidden_dirty
    if str(lot_id) not in HIDDEN:
        return False
    if _set_lot_active(lot_id, True, fast=fast):
        HIDDEN.pop(str(lot_id), None)
        if persist:
            save_hidden()
        else:
            _hidden_dirty = True
        logger.info(f"{LP} lot {lot_id} restored")
        return True
    return False


def _poll_cycle():
    api = _get_api()
    if not api:
        return
    mapped_lots = [m for m in MAPPINGS if m.get("lot_id")]

    if SETTINGS.get("auto_hide_zero_balance"):
        bal = api.balance()
        if bal is not None:
            cur = SETTINGS.get("currency", "RUB")
            amount = bal.get("balance_rub" if cur == "RUB" else "balance_usd", 0) or 0
            threshold = SETTINGS.get("balance_threshold", 0)
            if amount <= threshold:
                if not _balance_alerted["low"]:
                    notify_admins(f"Баланс KOSell на нуле/ниже порога ({amount} {cur}). "
                                  f"Скрываю лоты - пополните баланс.", dedup_key="balance_low")
                    _balance_alerted["low"] = True
                for m in mapped_lots:
                    _hide_lot(m["lot_id"], "balance")
                return
            else:
                if _balance_alerted["low"]:
                    notify_admins_info(f"✅ Баланс KOSell пополнен ({amount} {cur}). Восстанавливаю лоты.")
                _balance_alerted["low"] = False
                for lid, reason in list(HIDDEN.items()):
                    if reason == "balance":
                        _restore_lot(lid)

    if SETTINGS.get("auto_hide_no_stock"):
        products = api.products()
        if products is None:
            # Транзиентная ошибка API — не трогаем лоты, чтобы не скрыть всё зря.
            return
        avail = {}
        for p in products:
            try:
                avail[int(p.get("id"))] = int(p.get("available_accounts", 0) or 0)
            except Exception:
                continue
        # Реагируем только на изменение наличия на KOSell (или первый проход после старта).
        changed_pids = set()
        if not _last_stock_avail:
            changed_pids = set(avail.keys())
        else:
            for pid, count in avail.items():
                if _last_stock_avail.get(pid) != count:
                    changed_pids.add(pid)
            for pid in _last_stock_avail:
                if pid not in avail:
                    changed_pids.add(pid)
        _last_stock_avail.clear()
        _last_stock_avail.update(avail)
        stock_fast = True
        stock_persist = False
        for m in mapped_lots:
            pid = m.get("product_id")
            try:
                pid_int = int(pid)
            except Exception:
                # Ручная привязка без product_id — не управляем наличием.
                continue
            count = avail.get(pid_int)
            lid = str(m["lot_id"])
            in_hidden_stock = HIDDEN.get(lid) == "stock"
            if pid_int not in changed_pids:
                inconsistent = (
                    ((count is None or count <= 0) and lid not in HIDDEN)
                    or ((count is not None and count > 0) and in_hidden_stock)
                )
                if not inconsistent:
                    continue
            if count is None:
                # Товара больше нет в каталоге KOSell → в наличии его точно нет → скрываем.
                _hide_lot(m["lot_id"], "stock", persist=stock_persist, fast=stock_fast)
            elif count <= 0:
                _hide_lot(m["lot_id"], "stock", persist=stock_persist, fast=stock_fast)
            elif in_hidden_stock:
                _restore_lot(m["lot_id"], persist=stock_persist, fast=stock_fast)
        _flush_hidden()


def _notify_expirations():
    if not SETTINGS.get("notify_rental_end", True):
        return
    now = _now()
    changed = False
    for bk, lst in list(RENTALS.items()):
        for r in lst:
            exp = r.get("expires_ts") or 0
            if exp and now >= exp and not r.get("end_notified"):
                try:
                    _send(int(bk), fmt("text_rental_ended",
                                       login=r.get("login", ""),
                                       game=r.get("product_name") or "аккаунт"))
                except Exception:
                    pass
                r["end_notified"] = True
                changed = True
    if changed:
        save_rentals()


def _ver_tuple(v) -> tuple:
    if isinstance(v, (tuple, list)):
        return tuple(int(x) for x in v)
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in parts) or (0,)


def _parse_remote_version(code: str) -> "str | None":
    m = re.search(r'^\s*VERSION\s*=\s*["\']([^"\']+)["\']', code, re.M)
    return m.group(1) if m else None


def _download_text(url: str) -> "str | None":
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception as e:
        logger.debug(f"{LP} update download {url}: {e}")
    return None


def _apply_update(new_code: str, new_ver: str) -> bool:
    if "BIND_TO_PRE_INIT" not in new_code or "VERSION" not in new_code:
        logger.error(f"{LP} update: скачанный файл не похож на плагин — отмена")
        return False
    try:
        bak = SELF_PATH + ".bak"
        try:
            import shutil
            if os.path.exists(bak):
                os.remove(bak)
            shutil.copy2(SELF_PATH, bak)
        except Exception:
            pass
        new_code = new_code.replace("\r\n", "\n").replace("\r", "\n")
        tmp = SELF_PATH + ".new"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_code)
        os.replace(tmp, SELF_PATH)
        logger.info(f"{LP} обновление v{VERSION} → v{new_ver} записано в {SELF_PATH}")
        return True
    except Exception as e:
        logger.error(f"{LP} update apply error: {e}", exc_info=True)
        return False


def _save_update_pending(latest: str, refs: list, notes: str = ""):
    try:
        with open(UPDATE_PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump({"latest": latest, "refs": refs, "notes": notes}, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"{LP} save update_pending: {e}")


def _notify_update_ready(latest: str, notes: str = ""):
    if not bot:
        return
    txt = (f"<b>{LP}</b>\n"
           f"⬆️ Доступна версия <b>v{latest}</b> (у вас v{VERSION}).\n")
    if notes:
        txt += f"\n<b>Что нового:</b>\n{_html.escape(notes)}\n"
    txt += "\nОбновление уже загружено. Нажмите кнопку ниже, чтобы перезапустить FPC и применить его."
    kb = K()
    kb.row(B("♻️ Перезапустить FPC", callback_data=f"{P}_restart"))
    refs = []
    for admin_id in _admins():
        try:
            msg = bot.send_message(admin_id, txt, parse_mode="HTML", reply_markup=kb)
            refs.append([admin_id, msg.message_id])
        except Exception:
            pass
    _save_update_pending(latest, refs, notes)


def _finalize_update_after_restart():
    if not os.path.exists(UPDATE_PENDING_FILE):
        return
    try:
        with open(UPDATE_PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = None
    try:
        os.remove(UPDATE_PENDING_FILE)
    except Exception:
        pass
    if not data or not bot:
        return
    if _ver_tuple(data.get("latest")) != _ver_tuple(VERSION):
        return
    kb = K()
    kb.row(B("🎛 Открыть меню плагина", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    done_txt = f"<b>{LP}</b>\n✅ Обновление применено. Текущая версия: <b>v{VERSION}</b>."
    notes = (data.get("notes") or "").strip()
    if notes:
        done_txt += f"\n\n<b>Что нового:</b>\n{_html.escape(notes)}"
    for admin_id, mid in data.get("refs", []):
        try:
            bot.edit_message_text(done_txt, admin_id, mid, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


def _extract_notes(data: dict) -> str:
    """Достаёт текст «что нового» из ответа API (строка или список строк)."""
    plugin = data.get("plugin") or {}
    raw = (data.get("changelog") or data.get("release_notes") or data.get("notes")
           or data.get("whats_new") or plugin.get("changelog") or plugin.get("release_notes")
           or plugin.get("notes") or "")
    if isinstance(raw, (list, tuple)):
        lines = [str(x).strip() for x in raw if str(x).strip()]
        raw = "\n".join(f"• {ln}" if not ln.startswith(("•", "-", "—")) else ln for ln in lines)
    raw = str(raw).strip()
    if len(raw) > 1500:
        raw = raw[:1500].rstrip() + "…"
    return raw


def _check_update_via_api() -> "tuple[str, str, str] | None":
    from urllib.parse import urljoin
    for info_url in PLUGIN_INFO_URLS:
        try:
            r = requests.get(info_url, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception as e:
            logger.debug(f"{LP} update info {info_url}: {e}")
            continue
        remote = str(data.get("latest") or data.get("version") or "").strip()
        if not remote or _ver_tuple(remote) == _ver_tuple(VERSION):
            return None
        dl = ((data.get("plugin") or {}).get("download_url")) or f"/api/plugin/download/plugin/{remote}"
        dl_abs = urljoin(info_url, dl)
        code = _download_text(dl_abs)
        if code and _parse_remote_version(code):
            return code, remote, _extract_notes(data)
    return None


def check_update():
    code = None
    remote = None
    notes = ""
    via_api = _check_update_via_api()
    if via_api:
        code, remote, notes = via_api
    else:
        for url in UPDATE_URLS:
            code = _download_text(url)
            if code:
                break
        if not code:
            logger.warning(f"{LP} проверка обновлений: ни один источник недоступен "
                           f"(API {PLUGIN_INFO_URLS[0]} и статичные URL). Проверьте доступ к kosell.store.")
            return
        remote = _parse_remote_version(code)
    if not remote:
        logger.warning(f"{LP} проверка обновлений: не удалось определить версию на сервере")
        return
    if _ver_tuple(remote) == _ver_tuple(VERSION):
        logger.info(f"{LP} проверка обновлений: актуальная версия v{VERSION} (на сервере v{remote})")
        return
    if _ver_tuple(remote) == _ver_tuple(_update_state.get("latest")) and _update_state.get("pending_restart"):
        return
    logger.info(f"{LP} проверка обновлений: найдена v{remote} (у вас v{VERSION}), загружаю…")
    if _apply_update(code, remote):
        _update_state["pending_restart"] = True
        _update_state["latest"] = remote
        _update_state["notes"] = notes
        _notify_update_ready(remote, notes)
        logger.info(f"{LP} обновление v{remote} загружено, уведомление отправлено админам ({len(_admins())})")


def _poller_loop():
    last_upd = 0.0
    try:
        _poll_stop.wait(8)
        if not _poll_stop.is_set():
            check_update()
            last_upd = time.time()
    except Exception as e:
        logger.error(f"{LP} startup update check error: {e}")
    while not _poll_stop.is_set():
        _poll_stop.wait(max(10, SETTINGS.get("poll_sec", 30)))
        if _poll_stop.is_set():
            break
        try:
            if SETTINGS.get("enabled"):
                _notify_expirations()
            _cleanup_rentals()
            if SETTINGS.get("enabled") and (SETTINGS.get("auto_hide_no_stock") or SETTINGS.get("auto_hide_zero_balance")):
                _poll_cycle()
            if not _update_state.get("pending_restart") and time.time() - last_upd > UPDATE_CHECK_SEC:
                last_upd = time.time()
                try:
                    check_update()
                except Exception as e:
                    logger.error(f"{LP} update check error: {e}")
        except Exception as e:
            logger.error(f"{LP} poll error: {e}", exc_info=True)


def _start_poller():
    global _poll_thread
    if _poll_thread and _poll_thread.is_alive():
        return
    _poll_stop.clear()
    _poll_thread = threading.Thread(target=_poller_loop, daemon=True)
    _poll_thread.start()


def _stop_poller(*args, **kwargs):
    _poll_stop.set()


P = "kos"


def _back_btn(cb):
    return B("◀️ Назад", callback_data=cb)


# ============================================================================
#  АВТОСОЗДАНИЕ ЛОТОВ v1
# ============================================================================

def _ac_tpl() -> dict:
    t = SETTINGS.get("ac_template")
    return t if isinstance(t, dict) else {}


def _ac_has_template() -> bool:
    t = _ac_tpl()
    return bool(t.get("summary_ru") and t.get("durations"))


def _norm_game(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\[.*?\]", " ", s)
    s = re.sub(r"[+:&/\\,.\-_!?|™®]", " ", s)
    s = re.sub(r"[^a-z0-9а-яё' ]", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def _format_kostime(hours: int, lang: str = "ru") -> str:
    if lang == "en":
        if hours >= 720 and hours % 720 == 0:
            m = hours // 720
            return f"{m} month{'s' if m != 1 else ''}"
        if hours >= 24 and hours % 24 == 0:
            d = hours // 24
            return f"{d} day{'s' if d != 1 else ''}"
        return f"{hours}h"
    if hours >= 720:
        m = max(1, round(hours / 720))
        if m == 1:
            return "1 месяц"
        if 2 <= m % 10 <= 4 and m % 100 not in (12, 13, 14):
            return f"{m} месяца"
        return f"{m} месяцев"
    if hours >= 24 and hours % 24 == 0:
        d = hours // 24
        if d == 1:
            return "1 день"
        if 2 <= d % 10 <= 4 and d % 100 not in (12, 13, 14):
            return f"{d} дня"
        return f"{d} дней"
    if hours % 10 == 1 and hours % 100 != 11:
        return f"{hours} час"
    if hours % 10 in (2, 3, 4) and hours % 100 not in (12, 13, 14):
        return f"{hours} часа"
    return f"{hours} часов"


def _apply_placeholders(text: str, game_name: str, hours: int, lang: str = "ru") -> str:
    if not text:
        return text
    t = _format_kostime(hours, lang)
    g = game_name or ""
    out = text
    for ph in (PH_TIME, "kostime"):
        out = re.sub(re.escape(ph), t, out, flags=re.I)
    for ph in (PH_GAME, "kosgame"):
        out = re.sub(re.escape(ph), g, out, flags=re.I)
    return out


def _normalize_short_en(text: str) -> str:
    text = _ascii(text)
    if len(text) > 70:
        return (text[:67] + "...").strip()
    return text


def _kosell_cost_rub(api: "KOSellAPI | None", prod: dict, hours: int) -> float:
    pid = int(prod.get("id") or 0)
    key = (pid, int(hours))
    now = time.time()
    c = _price_cache.get(key)
    if c and now - c["ts"] < AC_CACHE_TTL:
        return c["rub"]
    rub = None
    if api is not None and pid:
        data = api.calculate_price(pid, hours)
        if data and data.get("total_rub") is not None:
            try:
                rub = float(data["total_rub"])
                _dbg(f"calc-price pid={pid} {hours}ч → {rub}₽ "
                     f"(скидка {data.get('discount_percent')}%)")
            except Exception:
                rub = None
    if rub is None:
        pph_rub = float(prod.get("price_per_hour_rub") or prod.get("price_per_hour") or 0)
        rub = pph_rub * hours
        _dbg(f"calc-price fallback pid={pid} {hours}ч → {rub}₽ (price_per_hour_rub×часы)")
    _price_cache[key] = {"ts": now, "rub": rub}
    return rub


def _get_commission(subcat_id: int) -> float:
    sid = int(subcat_id)
    if sid in _commission_cache:
        return _commission_cache[sid]
    k = 1.0
    try:
        acc = cardinal_instance.account
        headers = {"accept": "*/*", "x-requested-with": "XMLHttpRequest"}
        data = {"nodeId": sid, "price": 1000}
        r = acc.method("post", "lots/calc", headers, data)
        j = r.json()
        methods = j.get("methods") or []
        cur = SETTINGS.get("currency", "RUB")
        matching = [m for m in methods if m.get("unit") == cur]
        if matching:
            k = float(min(matching, key=lambda x: float(x.get("price", 1000))).get("price", 1000)) / 1000.0
    except Exception as e:
        logger.debug(f"{LP} [AC] commission sub={sid}: {e}")
    _commission_cache[sid] = k
    return k


def _money(x) -> str:
    try:
        v = round(float(x) + 1e-9, 2)
    except Exception:
        return str(x)
    if v == int(v):
        return f"{int(v)}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _pct(x) -> str:
    try:
        v = round(float(x), 2)
    except Exception:
        return str(x)
    if v == int(v):
        return f"{int(v)}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _min_price_floor(tpl: "dict | None" = None) -> float:
    try:
        v = float(SETTINGS.get("ac_min_price", 15) or 0)
    except Exception:
        v = 15.0
    return v if v >= 1.0 else 1.0


def _public_price_rub(api: "KOSellAPI | None", prod: dict, hours: int, tpl: "dict | None" = None) -> float:
    t = tpl or _ac_tpl()
    floor = _min_price_floor(t)
    if t.get("price_mode") == "fixed":
        fp = t.get("fixed_prices") or {}
        val = fp.get(str(hours), fp.get(hours))
        if val is None:
            val = t.get("fixed_public_rub")
        return round(float(max(floor, float(val or floor))), 2)
    markup = float(t.get("markup_percent", 50) or 0)
    base = _kosell_cost_rub(api, prod, hours)
    return round(float(max(floor, base * (1 + markup / 100.0))), 2)


def _lot_input_price(public_rub: float, subcat_id: int) -> float:
    return float(max(_min_price_floor(), public_rub))


def _map_urls_to_try() -> list:
    custom = (SETTINGS.get("map_url") or "").strip()
    urls = []
    if custom and custom not in MAP_URLS:
        urls.append(custom)
    for u in MAP_URLS:
        if u not in urls:
            urls.append(u)
    return urls


def _fetch_map(force: bool = False) -> "dict | None":
    now = time.time()
    if not force and _map_cache["data"] and now - _map_cache["ts"] < 600:
        return _map_cache["data"]
    data = None
    ok_url = None
    for url in _map_urls_to_try():
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
            ctype = (r.headers.get("Content-Type") or "").lower()
            if r.status_code == 200 and ("json" in ctype or r.text.lstrip()[:1] == "{"):
                candidate = r.json()
                if isinstance(candidate, dict) and candidate.get("games"):
                    data = candidate
                    ok_url = url
                    _save_json(MAP_FILE, data)
                    break
            _dbg(f"map {url}: HTTP {r.status_code}")
        except Exception as e:
            _dbg(f"map {url}: {e}")
    if data and ok_url:
        logger.info(f"{LP} [AC] карта загружена ({len(data.get('games', []))} игр): {ok_url}")
        _map_cache["warned"] = False
    elif not _map_cache.get("warned"):
        logger.info(f"{LP} [AC] все URL карты недоступны — локальный кэш {MAP_FILE}")
        _map_cache["warned"] = True
    if data is None:
        data = _load_json(MAP_FILE, None)
    if data:
        _map_cache["data"] = data
        _map_cache["ts"] = now
    return data


def _map_games_available(api: "KOSellAPI") -> list:
    mp = _fetch_map()
    if not mp:
        return []
    prods = _products(api) or []
    by_id = {int(p["id"]): p for p in prods if str(p.get("id", "")).isdigit()}
    by_name = {str(p.get("name", "")).strip().lower(): p for p in prods}
    out = []
    for g in mp.get("games", []):
        p = by_id.get(int(g.get("kosell_product_id", -1)))
        if not p:
            p = by_name.get(str(g.get("kosell_name", "")).strip().lower())
        if not p:
            for al in (g.get("aliases") or []):
                p = by_name.get(str(al).strip().lower())
                if p:
                    break
        if p:
            out.append((g, p))
    return out


def _load_misc_games() -> dict:
    if _misc_cache:
        return _misc_cache
    data = _load_json(MISC_GAMES_FILE, {})
    by_name, by_norm = {}, {}
    for g in data.get("games", []):
        nm = (g.get("name") or "").strip()
        if not nm:
            continue
        by_name[nm.lower()] = g
        by_norm[_norm_game(nm)] = g
    _misc_cache.update({"meta": data, "by_name": by_name, "by_norm": by_norm})
    return _misc_cache


def _name_overrides() -> dict:
    data = _load_json(NAME_OVERRIDES_FILE, {})
    out = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if k and v:
                out[str(k).strip().lower()] = str(v)
    return out


def _display_name(kosell_name: str) -> str:
    ov = _name_overrides()
    return ov.get((kosell_name or "").strip().lower(), kosell_name or "")


def _misc_any_game_id() -> "str | None":
    m = _load_misc_games().get("by_name") or {}
    for key in ("любая игра", "любая", "any game", "other"):
        g = m.get(key)
        if g and g.get("id"):
            return str(g["id"])
    return None


def _match_misc_game(kosell_name: str) -> "dict | None":
    m = _load_misc_games()
    kn = (kosell_name or "").strip().lower()
    if kn in m["by_name"]:
        return m["by_name"][kn]
    nn = _norm_game(kosell_name)
    if nn in m["by_norm"]:
        return m["by_norm"][nn]
    kt = nn.split()
    best, best_len = None, 0
    for nm, g in m["by_norm"].items():
        ft = nm.split()
        if not ft:
            continue
        if len(ft) == 1:
            if len(ft[0]) < 4:
                continue
            if ft[0] not in kt:
                continue
        elif kt[: len(ft)] != ft:
            continue
        if len(ft) > best_len:
            best_len, best = len(ft), g
    return best


def _all_sellable_games(api: "KOSellAPI") -> list:
    meta = _load_misc_games().get("meta") or {}
    misc_sub = int(meta.get("subcategory_id") or 451)
    misc_field = meta.get("field") or "server_id"
    mapped_pids = set()
    out = []

    def _add_misc(pid, raw_name):
        disp = _display_name(raw_name) or raw_name
        mg = _match_misc_game(disp) or _match_misc_game(raw_name)
        fields = {"fields[type]": "Аренда"}
        if mg:
            fields[misc_field] = str(mg["id"])
        else:
            any_id = _misc_any_game_id()
            if any_id:
                fields[misc_field] = any_id
        out.append({
            "product_id": pid,
            "product_name": raw_name,
            "game_name": (mg.get("name") if mg else None) or disp,
            "subcategory_id": misc_sub,
            "category_id": None,
            "fields": fields,
            "source": "misc",
        })

    for g, prod in _map_games_available(api):
        pid = int(prod["id"])
        gname = prod.get("name") or g.get("kosell_name") or ""
        mapped_pids.add(pid)
        if not _norm_game(gname):
            _dbg(f"игра без латиницы/кириллицы → прочие игры: {gname!r} → {_display_name(gname)!r} (pid={pid})")
            _add_misc(pid, gname)
            continue
        fp = g.get("funpay", {})
        out.append({
            "product_id": pid,
            "product_name": gname,
            "game_name": gname,
            "subcategory_id": int(fp["subcategory_id"]),
            "category_id": fp.get("category_id"),
            "fields": dict(fp.get("fields") or {}),
            "source": "map",
        })

    for prod in (_products(api) or []):
        pid = int(prod["id"])
        if pid in mapped_pids:
            continue
        raw = prod.get("name", "")
        mg = _match_misc_game(_display_name(raw)) or _match_misc_game(raw)
        if not mg:
            continue
        fields = {"fields[type]": "Аренда", misc_field: str(mg["id"])}
        out.append({
            "product_id": pid,
            "product_name": raw,
            "game_name": mg.get("name") or raw,
            "subcategory_id": misc_sub,
            "category_id": None,
            "fields": fields,
            "source": "misc",
        })
    return out


def _existing_lot_keys() -> set:
    keys = set()
    for m in MAPPINGS:
        pid, hrs = m.get("product_id"), m.get("hours")
        if pid is not None and hrs is not None:
            keys.add((int(pid), int(hrs)))
    return keys


def _fp_public_lots(subcat_id: int) -> list:
    sid = int(subcat_id)
    c = _pub_lots_cache.get(sid)
    now = time.time()
    if c and now - c["ts"] < AC_CACHE_TTL:
        return c["lots"]
    out = []
    try:
        lots = cardinal_instance.account.get_subcategory_public_lots(SubCategoryTypes.COMMON, sid)
        for l in lots:
            pr = float(getattr(l, "price", 0) or 0)
            if pr <= 0:
                continue
            desc = (getattr(l, "description", "") or "").lower()
            server = (getattr(l, "server", "") or "")
            server = server.lower() if isinstance(server, str) else ""
            out.append((pr, desc, server))
        _pub_lots_cache[sid] = {"ts": now, "lots": out}
        _dbg(f"FP подкат {sid}: загружено {len(out)} лотов (кэш 15 мин)")
    except Exception as e:
        logger.debug(f"{LP} [AC] public lots fail sub={sid}: {e}")
    return out


_RX_MINORDER = re.compile(r"от\s*\d+\s*(?:час\w*|ч\b|дн\w*|д\b|сут\w*|нед\w*|мес\w*)", re.I)
_RX_BONUS = re.compile(r"[+(]?\s*\d+\s*(?:час\w*|ч\b|дн\w*|д\b)?\s*за\s*отзыв", re.I)


def _duration_regex(hours: int) -> "re.Pattern":
    pats = []
    h = hours
    pats.append(rf"(?<!\d){h}\s*ч(?:ас(?:а|ов)?)?\b")
    if h % 24 == 0:
        d = h // 24
        pats.append(rf"(?<!\d){d}\s*д(?:\b|ень|ня|ней|н\.)")
        pats.append(rf"(?<!\d){d}\s*сут")
        if h == 24:
            pats.append(r"\bсуток\b|\bсутки\b")
    if h % 168 == 0:
        w = h // 168
        pats.append(rf"(?<!\d){w}\s*недел")
    if h % 720 == 0:
        mo = h // 720
        pats.append(rf"(?<!\d){mo}\s*мес(?:яц|яца|яцев)?\b")
    return re.compile("|".join(pats), re.I)


def _fp_lowest_prices_for(subcat_id: int, hours: int, game_name: "str | None" = None, n: int = 5) -> list:
    lots = _fp_public_lots(subcat_id)
    if not lots:
        return []
    rx = _duration_regex(hours)
    gtok = None
    if game_name:
        gtok = _norm_game(game_name).split()
        gtok = gtok[0] if gtok else None
    matched = []
    for pr, desc, server in lots:
        if game_name and gtok and gtok not in desc and gtok not in server:
            continue
        clean = _RX_MINORDER.sub(" ", desc)
        clean = _RX_BONUS.sub(" ", clean)
        if rx.search(clean):
            matched.append(pr)
    if not matched:
        return []
    return sorted(matched)[:n]


def _fp_lowest_prices(subcat_id: int, n: int = 5) -> list:
    lots = _fp_public_lots(subcat_id)
    return sorted(p for p, _, _ in lots)[:n]


def _ascii(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\u0400-\u04FF]", "", text)
    text = re.sub(r"[^\x20-\x7E]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _durations_for(prod: dict, tpl: "dict | None" = None) -> list:
    t = tpl or _ac_tpl()
    want = t.get("durations") or [3, 24, 72, 168]
    mn = int(prod.get("min_hours") or 1)
    mx = int(prod.get("max_hours") or 720)
    return sorted(h for h in want if mn <= h <= mx)


def _build_lot_texts(game_name: str, hours: int, tpl: "dict | None" = None):
    t = tpl or _ac_tpl()
    sru = _apply_placeholders(t.get("summary_ru", ""), game_name, hours, "ru")
    sen = _normalize_short_en(_apply_placeholders(t.get("summary_en", ""), game_name, hours, "en"))
    fru = _apply_placeholders(t.get("desc_ru", ""), game_name, hours, "ru")
    fen = _ascii(_apply_placeholders(t.get("desc_en", ""), game_name, hours, "en"))
    if not sen:
        sen = "RENTAL ACCOUNT INSTANT AUTO DELIVERY"
    return sru, sen, fru, fen


def _node_form(subcat_id: int) -> dict:
    sid = int(subcat_id)
    c = _node_form_cache.get(sid)
    now = time.time()
    if c and now - c["ts"] < AC_CACHE_TTL:
        return c["form"]
    account = cardinal_instance.account
    form = {"defaults": {}, "selects": {}, "required": set()}
    try:
        from bs4 import BeautifulSoup
        r = account.method("get", f"lots/offerEdit?node={sid}", {}, {}, raise_not_200=True)
        bs = BeautifulSoup(r.content.decode(), "lxml")
        f = bs.find("form", class_="form-offer-editor") or bs
        for inp in f.find_all("input"):
            name = inp.get("name")
            if not name or name == "query":
                continue
            if inp.get("type") == "checkbox":
                if inp.has_attr("checked"):
                    form["defaults"][name] = "on"
            else:
                form["defaults"][name] = inp.get("value") or ""
            if inp.get("required") is not None or "required" in (inp.get("class") or []):
                form["required"].add(name)
        for ta in f.find_all("textarea"):
            if ta.get("name"):
                form["defaults"][ta["name"]] = ta.text or ""
        for sel in f.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            parent = sel.find_parent(class_="form-group")
            pcls = (parent.get("class") or []) if parent else []
            if "hidden" in pcls and "lot-field" not in pcls:
                continue
            opts = []
            selected_val = ""
            for o in sel.find_all("option"):
                val = o.get("value") or ""
                opts.append((val, (o.text or "").strip()))
                if o.get("selected") is not None and val:
                    selected_val = val
            form["selects"][name] = opts
            form["defaults"][name] = selected_val
            if sel.get("required") is not None or "required" in (sel.get("class") or []):
                form["required"].add(name)
        csrf = f.find("input", {"name": "csrf_token"})
        if csrf and csrf.get("value"):
            account.csrf_token = csrf["value"]
        _dbg(f"_node_form sub={sid}: selects={list(form['selects'].keys())} required={sorted(form['required'])}")
    except Exception as e:
        logger.debug(f"{LP} [AC] node_form sub={sid}: {e}")
    _node_form_cache[sid] = {"ts": now, "form": form}
    return form


_PLATFORM_WORDS = {"pc", "пк", "ps", "ps3", "ps4", "ps5", "xbox", "switch", "steam",
                   "origin", "epic", "windows", "win", "mac", "computer", "компьютер"}
_PLATFORM_DETECT = _PLATFORM_WORDS | {"playstation", "плейстейшн", "macos", "linux", "линукс",
                                      "android", "андроид", "ios", "айос", "mobile", "мобил",
                                      "телефон", "nintendo", "нинтендо", "ps2"}
_PLATFORM_AVOID = ("android", "андроид", "ios", "айос", "mobile", "мобил", "телефон",
                   "ps", "xbox", "switch", "nintendo", "playstation", "плейстейшн")


def _looks_platform(nonempty: list) -> bool:
    if not nonempty:
        return False
    hits = 0
    for _v, t in nonempty:
        tl = (t or "").strip().lower()
        if any(p == tl or p in tl for p in _PLATFORM_DETECT):
            hits += 1
    return hits >= max(1, (len(nonempty) + 1) // 2)
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
          "viii": "8", "ix": "9", "x": "10"}
_EDITIONS = [
    ("ultimate", "ультимейт", "ультиматив"),
    ("collector", "коллекцион"),
    ("legendary", "легендар"),
    ("definitive", "дефинитив"),
    ("complete", "complet", "комплит", "полное", "полная"),
    ("game of the year", "goty", "года"),
    ("premium", "премиум"),
    ("deluxe", "делюкс", "делюх"),
    ("gold", "голд", "золот"),
    ("anniversary", "юбилейн", "годовщин"),
    ("enhanced", "энханс", "улучшен"),
    ("special", "спешл", "специальн"),
    ("standard", "standart", "стандарт", "обычн"),
]


def _norm_roman(s: str) -> str:
    s = _norm_game(s)
    return " ".join(_ROMAN.get(tok, tok) for tok in s.split())


def _pick_option(name: str, opts: list, game_name: str = "") -> str:
    nonempty = [(v, (t or "").strip()) for v, t in opts if v]
    if not nonempty:
        return ""
    low = name.lower()
    if "platform" in low or _looks_platform(nonempty):
        for v, t in nonempty:
            if "steam" in t.lower():
                return v
        for v, t in nonempty:
            tl = t.lower()
            if ("windows" in tl or "win" in tl) and "epic" not in tl:
                return v
        for v, t in nonempty:
            tl = t.lower()
            if ("pc" in tl or "пк" in tl or "computer" in tl or "компьютер" in tl) and "epic" not in tl:
                return v
        for v, t in nonempty:
            tl = t.lower()
            if "epic" not in tl and "origin" not in tl and not any(a in tl for a in _PLATFORM_AVOID):
                return v
        return nonempty[0][0]
    if "server" in low or low.endswith("[game]") or low == "game":
        g = set(_norm_roman(game_name).split())
        if g:
            best, best_key = None, (0, 0.0, 0)
            for v, t in nonempty:
                tt = set(_norm_roman(t).split())
                if not tt:
                    continue
                inter = len(g & tt)
                sc = inter / len(tt)
                tl = t.lower()
                if "steam" in tl:
                    plat = 3
                elif ("pc" in tl or "пк" in tl or "windows" in tl) and "epic" not in tl:
                    plat = 2
                elif any(b in tl for b in ("ps", "xbox", "switch", "epic", "playstation", "плейстейшн")):
                    plat = -1
                else:
                    plat = 1
                key = (inter, sc, plat)
                if inter and key > best_key:
                    best_key, best = key, v
            if best and best_key[0] >= 1 and best_key[1] >= 0.5:
                return best
        for key in ("steam",):
            for v, t in nonempty:
                if key in t.lower():
                    return v
        for key in ("pc", "пк"):
            for v, t in nonempty:
                if key in t.lower():
                    return v
        return nonempty[0][0]
    if "offer" in low or "type" in low:
        for v, t in nonempty:
            tl = t.lower()
            if "аренд" in tl or "rent" in tl:
                return v
        gl = (game_name or "").lower()
        for variants in _EDITIONS:
            if any(k in gl for k in variants):
                for v, t in nonempty:
                    if any(k in t.lower() for k in variants):
                        return v
                break
        for key in ("стандарт", "standart", "standard", "обычн", "basic", "base", "common"):
            for v, t in nonempty:
                if key in t.lower():
                    return v
        return nonempty[0][0]
    return nonempty[0][0]


def _fill_missing_from_error(nf: dict, node_form: dict, err_text: str, game_name: str) -> bool:
    pairs = re.findall(r'\[\s*"([^"]+)"\s*,\s*"([^"]*)"\s*\]', err_text or "")
    changed = False
    selects = node_form.get("selects") or {}
    for fname, _msg in pairs:
        cur = nf.get(fname)
        if cur:
            continue
        if fname in selects:
            chosen = _pick_option(fname, selects[fname], game_name)
            if chosen:
                nf[fname] = chosen
                changed = True
                _dbg(f"  дозаполнил select {fname}={chosen} :D")
        else:
            low = fname.lower()
            if "level" in low or "уровен" in low:
                val = "1"
            elif "summary][en" in fname or "desc][en" in fname:
                val = "RENTAL ACCOUNT INSTANT AUTO DELIVERY"
            else:
                val = "1"
            nf[fname] = val
            changed = True
            _dbg(f"  дозаполнил input {fname}={val} :D")
    return changed


class _RateLimited(Exception):
    pass


_FP_FIELD_NAMES = {
    "fields[summary][ru]": "Название (RU)",
    "fields[summary][en]": "Название (EN)",
    "fields[desc][ru]": "Описание (RU)",
    "fields[desc][en]": "Описание (EN)",
    "fields[platform]": "Платформа",
    "fields[type]": "Тип",
    "server_id": "Сервер/игра",
    "price": "Цена",
}


def _humanize_funpay_err(err: str) -> str:
    if not err:
        return "неизвестная ошибка"
    s = str(err)
    low = s.lower()
    if "много предложений" in low or "подождите" in low or "too many" in low:
        return "Лимит создания лотов FunPay (кулдаун)"
    try:
        m = re.search(r'\{.*\}', s, re.S)
        if m:
            data = json.loads(m.group(0))
            main = (data.get("error") or "").strip()
            parts = []
            for pair in (data.get("errors") or []):
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    fld = _FP_FIELD_NAMES.get(pair[0], pair[0])
                    parts.append(f"{fld}: {pair[1]}")
            if parts:
                return (main + " — " if main and main != "Заполните все поля." else "") + "; ".join(parts)
            if main:
                return main
    except Exception:
        pass
    s = s.strip()
    return s[-160:] if len(s) > 160 else s


def _create_one_lot(subcat_id: int, subcat_obj, map_fields: dict, summary_ru: str,
                    summary_en: str, full_ru: str, full_en: str, input_price: float,
                    game_name: str = "") -> "int | None":
    account = cardinal_instance.account
    try:
        before = {int(l.id) for l in account.get_my_subcategory_lots(int(subcat_id))}
    except Exception:
        before = set()

    template_fields = None
    try:
        my = account.get_my_subcategory_lots(int(subcat_id))
        if my:
            template_fields = account.get_lot_fields(int(my[0].id))
    except Exception as e:
        logger.debug(f"{LP} [AC] no template for sub={subcat_id}: {e}")

    node_form = _node_form(subcat_id)

    if template_fields:
        nf = dict(template_fields.fields)
    else:
        nf = dict(node_form.get("defaults") or {})
        nf.setdefault("query", "")
        nf.setdefault("location", "")
        nf.setdefault("deleted", "")
        nf.setdefault("auto_delivery", "")
        nf.setdefault("secrets", "")
        nf.setdefault("fields[payment_msg][ru]", "")
        nf.setdefault("fields[payment_msg][en]", "")

    nf["offer_id"] = "0"
    nf["node_id"] = str(subcat_id)
    nf["fields[summary][ru]"] = summary_ru
    nf["fields[summary][en]"] = summary_en
    nf["fields[desc][ru]"] = full_ru
    nf["fields[desc][en]"] = full_en
    nf["price"] = str(input_price)
    nf["amount"] = "99999"
    nf["active"] = "on"
    nf["offer_type_id"] = "1"
    nf["csrf_token"] = account.csrf_token
    nf["form_created_at"] = str(int(time.time()))
    for k, v in (map_fields or {}).items():
        if v:
            nf[k] = v

    for k in list(nf.keys()):
        kl = k.lower()
        if "secret" in kl or "auto_delivery" in kl or "autodelivery" in kl or kl == "secrets":
            nf.pop(k, None)
    nf["auto_delivery"] = ""
    nf["secrets"] = ""

    selects = node_form.get("selects") or {}
    for name, opts in selects.items():
        valid = {v for v, _ in opts if v}
        cur = nf.get(name)
        low = name.lower()
        ne = [(v, t) for v, t in opts if v]
        is_platform = ("platform" in low) or _looks_platform(ne)
        is_game_select = ("server" in low or low.endswith("[game]") or low == "game"
                          or "type" in low or "offer" in low) and not is_platform
        if is_game_select or is_platform or (not cur) or (valid and cur not in valid):
            chosen = _pick_option(name, opts, game_name)
            if chosen and chosen != cur:
                nf[name] = chosen
                _dbg(f"sub={subcat_id} подобрал {name}={chosen} (игра: {game_name})")
            elif chosen and not cur:
                nf[name] = chosen
    if "fields[type]" not in selects and not nf.get("fields[type]"):
        nf["fields[type]"] = "Аренда"

    if AC_DEBUG or SETTINGS.get("ac_debug"):
        dump = {k: v for k, v in nf.items() if k not in ("csrf_token",)}
        _dbg(f"_create_one_lot sub={subcat_id} поля лота:\n{json.dumps(dump, ensure_ascii=False, indent=1)[:1500]}")

    saved = False
    last_err = ""
    attempt = 0
    while attempt < 6:
        nf["csrf_token"] = account.csrf_token
        lf = LotFields(0, dict(nf), subcat_obj, account.currency)
        try:
            account.save_lot(lf)
            saved = True
            _dbg(f"save_lot OK sub={subcat_id} (попытка {attempt + 1})")
            break
        except Exception as e:
            msg = str(e)
            last_err = msg
            low_msg = msg.lower()
            if "много предложений" in low_msg or "подождите" in low_msg or "too many" in low_msg:
                _dbg(f"rate-limit FunPay sub={subcat_id}: лимит создания, остановка прогона")
                raise _RateLimited(msg)
            _dbg(f"save_lot ошибка sub={subcat_id} (попытка {attempt + 1}): {msg[-300:]}")
            changed = _fill_missing_from_error(nf, node_form, msg, game_name)
            if ("fields[summary][en]" in msg or "fields[desc][en]" in msg) and not nf.get("fields[summary][en]"):
                nf["fields[summary][en]"] = "RENTAL ACCOUNT INSTANT AUTO DELIVERY"
                nf["fields[desc][en]"] = ("Rent a licensed account with instant automatic delivery "
                                          "right after payment. Steam Guard code on request in chat.")
                changed = True
            attempt += 1
            if not changed:
                logger.warning(f"{LP} [AC] save_lot failed sub={subcat_id}: {msg[-200:]}")
                break

    if not saved:
        return None, _humanize_funpay_err(last_err)

    want = (summary_ru or "").strip()
    try:
        after = account.get_my_subcategory_lots(int(subcat_id))
        new = [l for l in after if int(l.id) not in before]
        if len(new) == 1:
            return int(new[0].id), None
        if len(new) > 1:
            for l in new:
                if (getattr(l, "description", "") or "").strip() == want:
                    return int(l.id), None
            _dbg(f"sub={subcat_id}: >1 новых лота, точного совпадения по названию нет — беру первый")
            return int(new[0].id), None
        for l in after:
            if (getattr(l, "description", "") or "").strip() == want:
                _dbg(f"sub={subcat_id}: lot_id найден по названию (листинг с задержкой)")
                return int(l.id), None
    except Exception as e:
        logger.debug(f"{LP} [AC] post-list fail sub={subcat_id}: {e}")
    return None, "лот создан, но не найден в списке (задержка FunPay)"


def _build_plan(user_id: int, missing_only: bool = True) -> list:
    api = _get_api()
    if not api or not _ac_has_template():
        return []
    tpl = _ac_tpl()
    have = _existing_lot_keys() if missing_only else set()
    items = []
    prods_by_id = {int(p["id"]): p for p in (_products(api) or [])}
    sellable = _all_sellable_games(api)
    _dbg(f"_build_plan: sellable={len(sellable)} missing_only={missing_only} have={len(have)}")
    for sg in sellable:
        prod = prods_by_id.get(sg["product_id"])
        if not prod:
            continue
        for h in _durations_for(prod, tpl):
            if missing_only and (sg["product_id"], h) in have:
                continue
            items.append({
                "product_id": sg["product_id"],
                "product_name": sg["product_name"],
                "game_name": sg["game_name"],
                "subcategory_id": sg["subcategory_id"],
                "fields": sg["fields"],
                "hours": h,
                "source": sg.get("source", "map"),
            })
    _ac_plan[user_id] = {"items": items, "ts": time.time()}
    _dbg(f"_build_plan: план из {len(items)} лотов (цены считаются лениво :D)")
    return items


def _fmt_eta(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"~{max(s, 1)} сек"
    m = s // 60
    if m < 60:
        rem = s % 60
        return f"~{m} мин" + (f" {rem} сек" if rem and m < 5 else "")
    h = m // 60
    mm = m % 60
    return f"~{h} ч" + (f" {mm} мин" if mm else "")


def _ac_progress_edit(chat_id, mid, done, total, created, skipped, failed,
                      title="Создание лотов…", verb="Создано", started=None):
    _ac_running.update({"title": title, "verb": verb, "done": done, "total": total,
                        "created": created, "skipped": skipped, "failed": failed,
                        "started": started})
    if not (chat_id and mid):
        return
    skip_part = f"   ⏭ Пропущено: {skipped}" if skipped else ""
    eta_line = ""
    if started and done < total:
        if done >= 3:
            elapsed = time.time() - started
            remaining = max(0.0, elapsed / done * (total - done))
            eta_line = f"\n⏳ Осталось: <b>{_fmt_eta(remaining)}</b>"
        else:
            eta_line = "\n⏳ Осталось: <b>подсчёт…</b>"
    txt = (f"🚀 <b>{title}</b>\n\n"
           f"<code>{_bar(done, total)}</code>\n\n"
           f"✅ {verb}: {created}{skip_part}   ❌ Ошибок: {failed}{eta_line}\n"
           f"<i>Можно свернуть — процесс идёт в фоне.</i>")
    try:
        bot.edit_message_text(txt, chat_id, mid, parse_mode="HTML")
    except Exception:
        pass


def _autocreate_run(user_id: int, chat_id=None, mid=None):
    created = skipped = failed = 0
    rate_limited = False
    err_counter = Counter()
    err_samples: dict = {}
    try:
        plan = (_ac_plan.get(user_id) or {}).get("items") or []
        total = len(plan)
        cap = int(SETTINGS.get("ac_subcat_cap", 15) or 15)
        existing_lots = {str(m.get("lot_id")) for m in MAPPINGS if m.get("lot_id")}
        sub_counts: dict = {}
        tpl = _ac_tpl()
        api = _get_api()
        prods_by_id = {int(p["id"]): p for p in (_products(api) or [])}
        _dbg(f"_autocreate_run: старт, лотов={total}, cap={cap}")
        started = time.time()
        _ac_progress_edit(chat_id, mid, 0, total, 0, 0, 0, started=started)

        for i, it in enumerate(plan, 1):
            sub_id = it["subcategory_id"]
            if sub_id not in sub_counts:
                try:
                    sub_counts[sub_id] = len(cardinal_instance.account.get_my_subcategory_lots(int(sub_id)))
                except Exception:
                    sub_counts[sub_id] = 0
            if sub_counts[sub_id] >= cap:
                skipped += 1
                _dbg(f"skip {it['game_name']} {it['hours']}ч — лимит раздела {sub_id}")
            else:
                subcat_obj = cardinal_instance.account.get_subcategory(SubCategoryTypes.COMMON, int(sub_id))
                sru, sen, fru, fen = _build_lot_texts(it["game_name"], it["hours"], tpl)
                prod = prods_by_id.get(it["product_id"], {"id": it["product_id"]})
                public_price = _public_price_rub(api, prod, it["hours"], tpl)
                input_price = _lot_input_price(public_price, sub_id)
                _dbg(f"create {it['game_name']} {it['hours']}ч sub={sub_id} "
                     f"pub={public_price} input={input_price}")
                try:
                    lot_id, err = _create_one_lot(int(sub_id), subcat_obj, it.get("fields") or {},
                                                  sru, sen, fru, fen, input_price, it["game_name"])
                except _RateLimited:
                    rate_limited = True
                    break
                if lot_id and str(lot_id) not in existing_lots:
                    MAPPINGS.append({
                        "lot_id": str(lot_id),
                        "subcategory_id": sub_id,
                        "title_keyword": it["game_name"][:40],
                        "product_id": it["product_id"],
                        "product_name": it["product_name"],
                        "hours": it["hours"],
                        "currency": SETTINGS.get("currency", "RUB"),
                        "auto_created": True,
                    })
                    existing_lots.add(str(lot_id))
                    sub_counts[sub_id] += 1
                    created += 1
                    save_mappings()
                else:
                    failed += 1
                    reason = err or "неизвестная ошибка"
                    err_counter[reason] += 1
                    err_samples.setdefault(reason, f"{it['game_name']} ({it['hours']}ч)")
                    _dbg(f"FAILED {it['game_name']} {it['hours']}ч sub={sub_id}: {reason}")
                time.sleep(AC_CREATE_DELAY)
            if i % 3 == 0 or i == total:
                _ac_progress_edit(chat_id, mid, i, total, created, skipped, failed, started=started)
    except Exception as e:
        logger.error(f"{LP} [AC] run error: {e}", exc_info=True)
    finally:
        _ac_running["on"] = False
        _ac_plan.pop(user_id, None)

    if rate_limited:
        done_txt = (
            f"⛔️ <b>FunPay включил лимит на создание лотов.</b>\n\n"
            f"✅ Успели создать: <b>{created}</b>\n"
            f"⏭ Пропущено (лимит раздела): <b>{skipped}</b>\n"
            f"Привязок всего: <b>{len(MAPPINGS)}</b>\n\n"
            f"⏳ Это кулдаун FunPay (обычно <b>~8 часов</b>). Дальше создавать сейчас бессмысленно — "
            f"вернитесь позже и снова нажмите «Создать недостающие»: плагин дозальёт только оставшиеся."
        )
    else:
        done_txt = (
            f"🤖 <b>Создание лотов завершено.</b>\n\n"
            f"✅ Создано: <b>{created}</b>\n"
            f"⏭ Пропущено (лимит раздела): <b>{skipped}</b>\n"
            f"❌ Ошибок: <b>{failed}</b>\n"
            f"Привязок всего: <b>{len(MAPPINGS)}</b>"
        )
        if failed and err_counter:
            done_txt += "\n\n⚠️ <b>Причины ошибок:</b>"
            for reason, cnt in err_counter.most_common(6):
                sample = err_samples.get(reason, "")
                done_txt += f"\n• <b>{cnt}×</b> {_html.escape(reason)}"
                if sample:
                    done_txt += f"\n   <i>напр.: {_html.escape(sample)}</i>"
            extra = len(err_counter) - 6
            if extra > 0:
                done_txt += f"\n…и ещё {extra} тип(ов) ошибок."
    if chat_id and mid:
        try:
            kb = K()
            kb.row(B("◀️ В меню создания", callback_data=f"{P}_ac"))
            bot.edit_message_text(done_txt, chat_id, mid, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    else:
        notify_admins_info(done_txt)


def _reprice_run(user_id: int, chat_id=None, mid=None):
    account = cardinal_instance.account
    api = _get_api()
    tpl = _ac_tpl()
    prods_by_id = {int(p["id"]): p for p in (_products(api) or [])}
    targets = [m for m in MAPPINGS
               if m.get("auto_created") and m.get("lot_id")
               and m.get("product_id") is not None and m.get("hours") is not None]
    total = len(targets)
    updated = unchanged = failed = 0
    rate_limited = False
    started = time.time()

    def _is_rate_limit(msg: str) -> bool:
        low = (msg or "").lower()
        return ("много предложений" in low or "подождите" in low
                or "too many" in low or "слишком часто" in low)

    try:
        _ac_progress_edit(chat_id, mid, 0, total, 0, 0, 0, "Пересчёт цен…", "Обновлено", started=started)
        for i, m in enumerate(targets, 1):
            did_write = False
            try:
                prod = prods_by_id.get(int(m["product_id"]), {"id": m["product_id"]})
                public = _public_price_rub(api, prod, int(m["hours"]), tpl)
                inp = _lot_input_price(public, int(m["subcategory_id"]))
                lf = account.get_lot_fields(int(m["lot_id"]))
                old = lf.fields.get("price")
                try:
                    old_val = float(str(old).replace(",", ".")) if old not in (None, "") else None
                except Exception:
                    old_val = None
                if old_val is not None and abs(round(old_val, 2) - round(inp, 2)) < 0.005:
                    unchanged += 1
                    _dbg(f"reprice SKIP lot={m['lot_id']} {m['hours']}ч цена не изменилась ({old_val})")
                else:
                    lf.fields["price"] = str(inp)
                    lf.fields["csrf_token"] = account.csrf_token
                    account.save_lot(lf)
                    did_write = True
                    updated += 1
                    _dbg(f"reprice lot={m['lot_id']} {m['hours']}ч {old}→{inp}")
            except Exception as e:
                msg = str(e)
                if _is_rate_limit(msg):
                    _dbg(f"reprice rate-limit FunPay на lot={m.get('lot_id')}: остановка прогона")
                    rate_limited = True
                    break
                failed += 1
                _dbg(f"reprice FAIL lot={m.get('lot_id')}: {msg[-200:]}")
            finally:
                if i % 3 == 0 or i == total:
                    _ac_progress_edit(chat_id, mid, i, total, updated, unchanged, failed,
                                      "Пересчёт цен…", "Обновлено", started=started)
            if did_write:
                time.sleep(AC_CREATE_DELAY + random.uniform(0.0, 1.0))
            else:
                time.sleep(0.4 + random.uniform(0.0, 0.4))
    except Exception as e:
        logger.error(f"{LP} [AC] reprice error: {e}", exc_info=True)
    finally:
        _ac_running["on"] = False

    floor = _money(_min_price_floor(tpl))
    if rate_limited:
        done = (f"⛔️ <b>FunPay лимит на изменение цен.</b>\n\n"
                f"✅ Обновлено: <b>{updated}</b>\n"
                f"➖ Без изменений: <b>{unchanged}</b>\n"
                f"❌ Ошибок: <b>{failed}</b>\n\n"
                f"⏳ Это кулдаун FunPay — вернитесь позже и снова нажмите «Пересчитать цены»: "
                f"уже выставленные цены пропустятся, плагин до-обновит только оставшиеся.")
    else:
        done = (f"♻️ <b>Пересчёт цен завершён.</b>\n\n"
                f"✅ Обновлено: <b>{updated}</b>\n"
                f"➖ Без изменений: <b>{unchanged}</b>\n"
                f"❌ Ошибок: <b>{failed}</b>\n\n"
                f"Наценка: <b>{_pct(tpl.get('markup_percent', 0))}%</b>, мин. цена: <b>{floor}₽</b>")
    if chat_id and mid:
        try:
            kb = K()
            kb.row(B("◀️ В меню создания", callback_data=f"{P}_ac"))
            bot.edit_message_text(done, chat_id, mid, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    else:
        notify_admins_info(done)


def _delete_plugin_run(user_id: int, chat_id=None, mid=None):
    account = cardinal_instance.account
    deleted = failed = 0
    targets = [m for m in MAPPINGS if m.get("lot_id")]
    total = len(targets)
    _dbg(f"delete_plugin: к удалению {total} привязанных лотов")
    started = time.time()
    try:
        _ac_progress_edit(chat_id, mid, 0, total, 0, 0, failed, "Удаление лотов…", "Удалено", started=started)
        done_ids = set()
        for i, m in enumerate(targets, 1):
            lot_id = m.get("lot_id")
            try:
                account.delete_lot(int(lot_id))
                deleted += 1
                done_ids.add(str(lot_id))
                _dbg(f"delete OK lot={lot_id} ({m.get('product_name')} {m.get('hours')}ч)")
            except Exception as e:
                msg = str(e)
                if "не найден" in msg.lower() or "not found" in msg.lower() or "offer" in msg.lower():
                    done_ids.add(str(lot_id))
                    _dbg(f"delete lot={lot_id}: уже нет на FunPay, чищу привязку")
                else:
                    failed += 1
                    _dbg(f"delete FAIL lot={lot_id}: {msg[-160:]}")
            time.sleep(AC_DELETE_DELAY)
            if i % 10 == 0 or i == total:
                _ac_progress_edit(chat_id, mid, i, total, deleted, 0, failed,
                                  "Удаление лотов…", "Удалено", started=started)
        MAPPINGS[:] = [m for m in MAPPINGS if str(m.get("lot_id")) not in done_ids]
        save_mappings()
        if done_ids:
            for lid in done_ids:
                HIDDEN.pop(str(lid), None)
            save_hidden()
    except Exception as e:
        logger.error(f"{LP} [AC] delete_plugin error: {e}", exc_info=True)
    finally:
        _ac_running["on"] = False

    done = (f"🗑 <b>Удаление лотов завершено.</b>\n\n"
            f"✅ Удалено: <b>{deleted}</b>\n"
            f"❌ Ошибок: <b>{failed}</b>\n"
            f"🔗 Осталось привязок: <b>{len(MAPPINGS)}</b>")
    if chat_id and mid:
        try:
            kb = K()
            kb.row(B("◀️ В меню создания", callback_data=f"{P}_ac"))
            bot.edit_message_text(done, chat_id, mid, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    else:
        notify_admins_info(done)


PLUGIN_HELP_TEXT = (
    "<b>ℹ️ Справка KOSell Rent</b>\n\n"
    "<b>Команды покупателя (в чат FunPay)</b>\n"
    "• <code>!код логин</code> — Steam Guard код для выданного аккаунта\n"
    "• <code>!прод логин</code> — продление определенного аккаунта если у покупателя несколько арендованных аккаунтов (после повторной оплаты)\n"
    "• <code>!friend</code> или <code>!друг</code> — режим «для друга» (см. ниже)\n\n"
    "<b>Обычная выдача (!friend выключен)</b>\n"
    "• Первый заказ по игре — новый аккаунт.\n"
    "• Повторный заказ по той же игре — продление того же аккаунта.\n"
    "• Время за заказ: <b>часы в привязке лота × кол-во шт (qty)</b>.\n"
    "  Пример: в привязке 1 ч, покупатель взял 3 шт → 1×3 = <b>3 ч</b> на одном аккаунте.\n"
    "  Пример: в привязке 3 ч, покупатель взял 1 шт → <b>3 ч</b> на одном аккаунте.\n"
    "• Лимит KOSell на одну операцию: от 1 до 720 ч. Повторными продлениями суммарно можно больше 720 ч.\n\n"
    "<b>Режим !friend (для друга)</b>\n"
    "• Покупатель пишет <code>!friend</code>, затем оплачивает лот в течение окна (см. параметры).\n"
    "• <b>qty = количество отдельных аккаунтов</b>, а не суммарные часы.\n"
    "• Часы каждого аккаунта — из привязки лота (не умножаются на qty).\n"
    "  Пример: привязка 3 ч, qty=3, !friend вкл → <b>3 аккаунта</b>, каждый на 3 ч.\n"
    "• Нужен один аккаунт на большее время — покупать <b>без</b> !friend.\n"
    "• Нужно несколько аккаунтов друзьям — !friend вкл, qty = число аккаунтов.\n\n"
    "<b>Привязки лотов</b>\n"
    "• Привязка по ID лота — переименование лота не ломает выдачу.\n"
    "• «Часы за 1 шт» в привязке — сколько часов аренды за одну штуку в заказе.\n\n"
    "<b>Частые вопросы</b>\n"
    "• Лот «цена за 1 час», мин. 1 ч, qty=3, !friend вкл → 3 акка на 1 ч — это норма.\n"
    "• Один аккаунт на 3 ч → !friend выкл, qty=1, лот с 3 ч в привязке (или 1 ч × 3 шт без friend).\n"
    "• Несколько активных аккаунтов одной игры — бот спросит, какой продлить (!прод логин)."
)


def tg_info(call):
    kb = K()
    kb.row(_back_btn(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    _edit(call, PLUGIN_HELP_TEXT, kb)


def _update_banner() -> str:
    st = _update_state
    if st.get("pending_restart"):
        return (f"⬆️ <b>Доступна версия v{st.get('latest')}</b> — обновление загружено.\n"
                f"Нажмите «♻️ Перезапустить FPC», чтобы применить.\n\n")
    return ""


def tg_restart(call):
    try:
        bot.answer_callback_query(call.id, "Перезапуск FPC…")
    except Exception:
        pass
    try:
        bot.edit_message_text(f"<b>{LP}</b>\n♻️ Перезапускаю FPC, применяю обновление…",
                              call.message.chat.id, call.message.id, parse_mode="HTML")
    except Exception:
        pass
    try:
        from Utils.cardinal_tools import restart_program
        restart_program()
    except Exception as e:
        logger.error(f"{LP} restart error: {e}")
        try:
            bot.answer_callback_query(call.id, f"Не удалось перезапустить: {e}", show_alert=True)
        except Exception:
            pass


def tg_raise_toggle(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    try:
        cfg = cardinal_instance.MAIN_CFG
        cur = cfg["FunPay"].getboolean("autoRaise")
        cfg["FunPay"]["autoRaise"] = "0" if cur else "1"
        cardinal_instance.save_config(cfg, "configs/_main.cfg")
    except Exception as e:
        logger.error(f"{LP} raise toggle error: {e}")
    try:
        tg_main(call)
    except Exception:
        pass


def _run_status_line() -> str:
    if not _ac_running.get("on"):
        return ""
    rt = _ac_running.get("title", "Процесс")
    rd, rtot = _ac_running.get("done", 0), _ac_running.get("total", 0)
    pct = int(rd * 100 / rtot) if rtot else 0
    rv, rc = _ac_running.get("verb", "Готово"), _ac_running.get("created", 0)
    return f"⏳ <b>{rt}</b> {pct}% · {rv}: {rc}/{rtot}"


def tg_main(call):
    s = SETTINGS
    en = s.get("enabled")
    raise_on = bool(cardinal_instance and cardinal_instance.MAIN_CFG["FunPay"].getboolean("autoRaise"))
    kb = K()
    if _ac_running.get("on"):
        kb.row(B(f"⏳ {_ac_running.get('title','Процесс')} — обновить", callback_data=f"{P}_main"))
    kb.row(B(f"{'🟢 Автовыдача: ВКЛ' if en else '🔴 Автовыдача: ВЫКЛ'}", callback_data=f"{P}_toggle:enabled"))
    kb.row(B(f"{'🟢' if raise_on else '🔴'} Автоподнятие лотов (FPC)", callback_data=f"{P}_raise"))
    kb.row(B(f"{_chk('auto_hide_no_stock')} Скрывать лоты: нет аккаунтов", callback_data=f"{P}_toggle:auto_hide_no_stock"))
    kb.row(B(f"{_chk('auto_hide_zero_balance')} Скрывать лоты: нет баланса", callback_data=f"{P}_toggle:auto_hide_zero_balance"))
    kb.row(B(f"{_chk('auto_refund')} Авто-возврат при сбое выдачи", callback_data=f"{P}_toggle:auto_refund"))
    kb.row(B(f"{_chk('review_bonus_enabled')} Бонус за отзыв", callback_data=f"{P}_toggle:review_bonus_enabled"))
    kb.row(B(f"{_chk('notify_rental_end')} Сообщение об окончании аренды", callback_data=f"{P}_toggle:notify_rental_end"))
    kb.row(B(f"{_chk('notify_sales')} Уведомлять о продажах", callback_data=f"{P}_toggle:notify_sales"))
    if _update_state.get("pending_restart"):
        kb.row(B(f"♻️ Перезапустить FPC (обновление v{_update_state.get('latest')})", callback_data=f"{P}_restart"))
    kb.row(B("🔑 API ключ", callback_data=f"{P}_apikey"), B("💰 Баланс", callback_data=f"{P}_balance"))
    kb.row(B("🔗 Привязки лотов", callback_data=f"{P}_maps:{_page_of('maps')}"))
    kb.row(B("📦 Создание лотов", callback_data=f"{P}_ac"))
    kb.row(B("⚙️ Параметры", callback_data=f"{P}_params"), B("✏️ Тексты", callback_data=f"{P}_texts"))
    kb.row(B("ℹ️ Справка: команды и !friend", callback_data=f"{P}_info"))
    kb.row(_back_btn(f"{CBT.EDIT_PLUGIN}:{UUID}:0"))

    key_set = "✅" if s.get("api_key") else "❌ не задан"
    status = _run_status_line()
    text = (
        f"<b>⚙️ KOSell Rent</b> <i>v{VERSION}</i>\n\n"
        + (_update_banner() or "")
        + (status + "\n\n" if status else "")
        + f"API ключ: {key_set}\n"
        f"Валюта оплаты KOSell: <b>{s.get('currency')}</b>\n"
        f"Привязок лотов: {len(MAPPINGS)}\n"
        f"Сейчас скрыто лотов: {len(HIDDEN)}\n\n"
        f"Команды и режим !friend — кнопка «ℹ️ Справка» ниже."
    )
    _edit(call, text, kb)


def _nav(prefix: str, page: int, total: int, per: int):
    pages = max(1, (total + per - 1) // per)
    row = []
    if page > 0:
        row.append(B("‹", callback_data=f"{prefix}{page-1}"))
    row.append(B(f"{page+1}/{pages}", callback_data=f"{P}_noop"))
    if (page + 1) * per < total:
        row.append(B("›", callback_data=f"{prefix}{page+1}"))
    return (row if len(row) > 1 else []), pages


def tg_noop(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def _chk(key) -> str:
    return "🟢" if SETTINGS.get(key) else "🔴"


def _edit(call, text, kb):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        msg = str(e).lower()
        if "not modified" not in msg:
            try:
                bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def _wiz_msg(chat_id, uid, text, kb):
    w = _ac_wizard.setdefault(uid, {})
    mid = w.get("message_id")
    if mid:
        try:
            bot.edit_message_text(text, chat_id, mid, reply_markup=kb, parse_mode="HTML")
            return mid
        except Exception:
            pass
    m = bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    w["message_id"] = m.message_id
    return m.message_id


def tg_toggle(call):
    key = call.data.split(":", 1)[1]
    SETTINGS[key] = not SETTINGS.get(key, False)
    save_settings()
    tg_main(call)


def tg_apikey(call):
    tg.set_state(call.message.chat.id, call.message.id, call.from_user.id, f"{P}_set_apikey")
    cur = SETTINGS.get("api_key", "")
    masked = f"{cur[:10]}...{cur[-4:]}" if len(cur) > 16 else ("(не задан)" if not cur else cur)
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    _edit(call, f"<b>🔑 API ключ KOSell</b>\n\nТекущий: <code>{_html.escape(masked)}</code>\n\nОтправьте новый ключ:", kb)


def tg_balance(call):
    api = _get_api()
    if not api:
        bot.answer_callback_query(call.id, "❌ API ключ не задан", show_alert=True)
        return
    bal = api.balance()
    if not bal:
        bot.answer_callback_query(call.id, "❌ Ошибка получения баланса", show_alert=True)
        return
    kb = K()
    kb.add(_back_btn(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    text = (
        f"<b>💰 Баланс KOSell</b>\n\n"
        f"Пользователь: <b>{_html.escape(str(bal.get('username', '—')))}</b>\n"
        f"USD: <b>{bal.get('balance_usd', 0)}</b>\n"
        f"RUB: <b>{bal.get('balance_rub', 0)}</b>\n"
    )
    _edit(call, text, kb)


_ui_page: dict = {}


def _save_page(key: str, page):
    try:
        _ui_page[key] = max(0, int(page))
    except Exception:
        _ui_page[key] = 0


def _page_of(key: str) -> int:
    return _ui_page.get(key, 0)


def _lot_link(lot_id) -> str:
    return f"https://funpay.com/lots/offer?id={lot_id}" if lot_id else ""


def tg_maps(call):
    page = int(call.data.split(":")[1]) if ":" in call.data else 0
    if page * 6 >= len(MAPPINGS):
        page = max(0, (len(MAPPINGS) - 1) // 6)
    _save_page("maps", page)
    per = 6
    start = page * per
    chunk = MAPPINGS[start:start + per]
    kb = K()
    for i, m in enumerate(chunk):
        idx = start + i
        label = f"{m.get('product_name', '?')} · {m.get('hours', '?')}ч · лот {m.get('lot_id', '?')}"
        kb.row(B(label[:60], callback_data=f"{P}_map:{idx}"))
    nav, _ = _nav(f"{P}_maps:", page, len(MAPPINGS), per)
    if nav:
        kb.row(*nav)
    kb.row(B("➕ Добавить привязку", callback_data=f"{P}_addmap:0"))
    kb.row(_back_btn(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    _edit(call, f"<b>🔗 Привязки лотов ({len(MAPPINGS)})</b>\n\n"
                f"Каждая привязка: лот FunPay → товар KOSell + кол-во часов за 1 шт.\n"
                f"Один товар можно привязать к нескольким лотам — при отсутствии аккаунтов "
                f"скроются все эти лоты.", kb)


def tg_map_detail(call):
    idx = int(call.data.split(":")[1])
    if idx >= len(MAPPINGS):
        bot.answer_callback_query(call.id, "Не найдено", show_alert=True)
        return
    m = MAPPINGS[idx]
    link = _lot_link(m.get("lot_id"))
    kb = K()
    kb.row(B("⏱ Часы", callback_data=f"{P}_map_hours:{idx}"), B("💱 Валюта", callback_data=f"{P}_map_cur:{idx}"))
    kb.row(B("🗑 Удалить", callback_data=f"{P}_map_del:{idx}"))
    kb.row(_back_btn(f"{P}_maps:{_page_of('maps')}"))
    text = (
        f"<b>🔗 Привязка</b>\n\n"
        f"Товар KOSell: <b>{_html.escape(str(m.get('product_name', '?')))}</b> (id {m.get('product_id')})\n"
        f"Лот FunPay: <code>{m.get('lot_id', '—')}</code>"
        + (f" · <a href=\"{link}\">открыть</a>\n" if link else "\n")
        + f"Подкатегория: <code>{m.get('subcategory_id', '—')}</code>\n"
        f"Ключевое слово: <code>{_html.escape(str(m.get('title_keyword', '—')))}</code>\n"
        f"Часов за 1 шт: <b>{m.get('hours', '?')}</b>\n"
        f"Валюта: <b>{m.get('currency') or SETTINGS.get('currency')}</b>\n"
    )
    _edit(call, text, kb)


def tg_map_hours(call):
    idx = int(call.data.split(":")[1])
    if idx >= len(MAPPINGS):
        bot.answer_callback_query(call.id, "Не найдено", show_alert=True)
        return
    m = MAPPINGS[idx]
    tg.set_state(call.message.chat.id, call.message.id, call.from_user.id, f"{P}_set_hours:{idx}")
    kb = K()
    link = _lot_link(m.get("lot_id"))
    if link:
        kb.row(B("🔗 Открыть лот на FunPay", url=link))
    kb.add(B("❌ Отмена", callback_data=f"{P}_map:{idx}"))
    name = _html.escape(str(m.get("product_name", "?")))
    lot_line = f"<code>{m.get('lot_id', '—')}</code>" + (f" · <a href=\"{link}\">открыть</a>" if link else "")
    _edit(call, f"<b>⏱ Часы аренды</b>\n\n"
                f"Товар: <b>{name}</b>\n"
                f"Лот FunPay: {lot_line}\n"
                f"Текущее значение: <b>{m.get('hours', '?')} ч</b>\n\n"
                f"Сколько часов аренды выдавать за покупку 1 шт этого лота?\n"
                f"Введите число от 3 до 720:", kb)


def tg_map_cur(call):
    idx = int(call.data.split(":")[1])
    m = MAPPINGS[idx]
    m["currency"] = "USD" if (m.get("currency") or SETTINGS.get("currency")) == "RUB" else "RUB"
    save_mappings()
    tg_map_detail(call)


def tg_map_del(call):
    idx = int(call.data.split(":")[1])
    kb = K()
    kb.row(B("✅ Удалить", callback_data=f"{P}_map_dely:{idx}"), B("❌ Нет", callback_data=f"{P}_map:{idx}"))
    _edit(call, "Удалить эту привязку?", kb)


def tg_map_del_yes(call):
    idx = int(call.data.split(":")[1])
    if idx < len(MAPPINGS):
        MAPPINGS.pop(idx)
        save_mappings()
    bot.answer_callback_query(call.id, "✅ Удалено")
    call.data = f"{P}_maps:{_page_of('maps')}"
    tg_maps(call)


_addmap_tmp: dict = {}


def tg_addmap(call):
    page = int(call.data.split(":")[1]) if ":" in call.data else 0
    lots = []
    try:
        if cardinal_instance and cardinal_instance.profile:
            lots = cardinal_instance.profile.get_lots()
    except Exception as e:
        logger.error(f"{LP} get_lots failed: {e}")
    per = 7
    start = page * per
    chunk = lots[start:start + per]
    kb = K()
    for lot in chunk:
        title = (lot.description or str(lot.id))[:45]
        kb.row(B(f"{title} (id {lot.id})"[:60], callback_data=f"{P}_picklot:{lot.id}:{page}"))
    nav, _ = _nav(f"{P}_addmap:", page, len(lots), per)
    if nav:
        kb.row(*nav)
    kb.row(B("✍️ Ввести Lot ID / ссылку вручную", callback_data=f"{P}_addmap_manual"))
    kb.row(_back_btn(f"{P}_maps:{_page_of('maps')}"))
    if not lots:
        _edit(call, "Лоты FunPay не загружены. "
                    "Нажмите «Ввести Lot ID / ссылку вручную».", kb)
    else:
        _edit(call, "<b>➕ Шаг 1/3 — лот FunPay</b>\nВыберите свой лот, к которому привяжем товар KOSell:", kb)


def _load_products_cache(user_id):
    api = _get_api()
    products = api.products() if api else None
    info = _addmap_tmp.setdefault(user_id, {})
    info["_all"] = products if products else []
    info.pop("_filtered", None)
    return products


def _product_list_kb(user_id, page: int):
    info = _addmap_tmp.get(user_id, {})
    products = info.get("_filtered")
    if products is None:
        products = info.get("_all", [])
    per = 8
    start = page * per
    chunk = products[start:start + per]
    kb = K()
    for p in chunk:
        kb.row(B(f"{p.get('name')} · {p.get('available_accounts', 0)} шт"[:60],
                 callback_data=f"{P}_pickprod:{p.get('id')}"))
    nav, _ = _nav(f"{P}_prodlist:", page, len(products), per)
    if nav:
        kb.row(*nav)
    kb.row(B("🔍 Поиск по названию", callback_data=f"{P}_prodsearch"))
    kb.row(B("✍️ Ввести ID товара вручную", callback_data=f"{P}_prodmanual"))
    kb.row(B("❌ Отмена", callback_data=f"{P}_maps:{_page_of('maps')}"))
    if not products:
        if info.get("_filtered") is not None:
            text = ("<b>➕ Шаг 2/3 — товар KOSell</b>\n\nПо запросу ничего не найдено. "
                    "Нажмите «Поиск» снова или введите ID товара вручную.")
        else:
            text = ("<b>➕ Шаг 2/3 — товар KOSell</b>\n\nСписок товаров пуст "
                    "(проверьте API ключ) — можно ввести ID товара вручную.")
    else:
        flt = " (фильтр)" if info.get("_filtered") is not None else ""
        text = (f"<b>➕ Шаг 2/3 — товар KOSell{flt}</b>\n\n"
                f"Это привязка игры с KOSell к лоту FunPay.\n"
                f"Выберите товар из списка или задайте ID вручную:")
    return text, kb


def tg_pick_lot(call):
    parts = call.data.split(":")
    lot_id = parts[1]
    lots = []
    try:
        lots = cardinal_instance.profile.get_lots()
    except Exception:
        pass
    subcat, kw = None, ""
    for lot in lots:
        if str(lot.id) == str(lot_id):
            subcat = getattr(getattr(lot, "subcategory", None), "id", None)
            kw = (lot.description or "")[:40]
            break
    _addmap_tmp[call.from_user.id] = {"lot_id": lot_id, "subcategory_id": subcat, "title_keyword": kw}
    _load_products_cache(call.from_user.id)
    text, kb = _product_list_kb(call.from_user.id, 0)
    _edit(call, text, kb)


def tg_addmap_manual(call):
    tg.set_state(call.message.chat.id, call.message.id, call.from_user.id, f"{P}_manual_lot")
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{P}_maps:{_page_of('maps')}"))
    _edit(call, "<b>➕ Шаг 1/3 — лот FunPay</b>\n\nОтправьте ID лота или ссылку на лот FunPay\n"
                "(напр. <code>63635527</code> или <code>https://funpay.com/lots/offer?id=63635527</code>):", kb)


def tg_prodlist(call):
    page = int(call.data.split(":")[1]) if ":" in call.data else 0
    text, kb = _product_list_kb(call.from_user.id, page)
    _edit(call, text, kb)


def tg_prodsearch(call):
    tg.set_state(call.message.chat.id, call.message.id, call.from_user.id, f"{P}_prodsearch")
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{P}_maps:{_page_of('maps')}"))
    _edit(call, "<b>🔍 Поиск товара KOSell</b>\n\nВведите часть названия игры (напр. <code>counter</code>):", kb)


def tg_prodmanual(call):
    tg.set_state(call.message.chat.id, call.message.id, call.from_user.id, f"{P}_prodmanual")
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{P}_maps:{_page_of('maps')}"))
    _edit(call, "<b>✍️ ID товара KOSell</b>\n\nОтправьте числовой ID товара (из каталога KOSell):", kb)


def _resolve_product_name(user_id, pid) -> str:
    for p in (_addmap_tmp.get(user_id, {}).get("_all") or []):
        if str(p.get("id")) == str(pid):
            return p.get("name") or f"#{pid}"
    return f"#{pid}"


def tg_pick_product(call):
    pid = call.data.split(":")[1]
    info = _addmap_tmp.setdefault(call.from_user.id, {})
    info["product_id"] = int(pid) if str(pid).isdigit() else pid
    info["product_name"] = _resolve_product_name(call.from_user.id, pid)
    tg.set_state(call.message.chat.id, call.message.id, call.from_user.id, f"{P}_set_newhours")
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{P}_maps:{_page_of('maps')}"))
    _edit(call, f"<b>➕ Шаг 3/3 — часы</b>\n\nТовар: <b>{_html.escape(str(info['product_name']))}</b>\n\n"
                f"Сколько часов аренды выдавать за покупку 1 шт лота? Введите число 3-720:", kb)


TEXT_LABELS = {
    "text_delivery": "Выдача аккаунта",
    "text_extension": "Продление аренды",
    "text_friend_activated": "Режим друга включён",
    "text_friend_already": "Режим друга уже активен",
    "text_code": "Steam Guard код",
    "text_code_ask_login": "Код: запрос логина",
    "text_code_not_found": "Код: не найдено",
    "text_ask_which_account": "Какой аккаунт продлить",
    "text_extend_applied": "Продление применено",
    "text_extend_no_pending": "Продление: нет ожидающих",
    "text_review_bonus": "Бонус за отзыв",
    "text_partial": "Частичная выдача (друг)",
    "text_problem": "Сообщение при сбое",
    "text_rental_ended": "Окончание аренды (отзыв)",
}

PARAM_DESC = {
    "friend_minutes": "Сколько минут действует режим «Для друга» после команды !friend (по истечении — снова продление).",
    "review_bonus_hours": "Сколько часов аренды добавлять покупателю за оставленный отзыв (списывается с баланса KOSell).",
    "balance_threshold": "Минимальный баланс KOSell (в выбранной валюте), ниже которого лоты автоматически скрываются.",
    "poll_sec": "Как часто (в секундах) проверять наличие аккаунтов и баланс KOSell для авто-скрытия лотов.",
    "ac_min_price": "Минимальная цена авто-лота в рублях («пол»). Если себестоимость × наценка выходит дешевле — ставится это значение. Применяется при создании и пересчёте цен. Минимум 1₽.",
    "tz_offset_hours": "Часовой пояс для показа времени покупателям (смещение от UTC). 3 = МСК. Допустимы отрицательные (напр. -3). Влияет только на отображение «Действует до», расчёты идут в UTC.",
}


def tg_params(call):
    s = SETTINGS
    kb = K()
    kb.row(B(f"💱 Валюта оплаты KOSell: {s.get('currency')}", callback_data=f"{P}_p_cur"))
    kb.row(B(f"👥 Режим «друг», мин: {s.get('friend_minutes')}", callback_data=f"{P}_p_set:friend_minutes"))
    kb.row(B(f"🎁 Бонус за отзыв, ч: {s.get('review_bonus_hours')}", callback_data=f"{P}_p_set:review_bonus_hours"))
    kb.row(B(f"⭐ Мин. звёзд для бонуса: {s.get('review_bonus_min_stars')}", callback_data=f"{P}_p_stars"))
    kb.row(B(f"💸 Порог баланса: {_money(s.get('balance_threshold', 0))}", callback_data=f"{P}_p_set:balance_threshold"))
    kb.row(B(f"💰 Мин. цена лота, ₽: {_money(s.get('ac_min_price', 15))}", callback_data=f"{P}_p_set:ac_min_price"))
    kb.row(B(f"⏲ Проверка склада/баланса, сек: {s.get('poll_sec')}", callback_data=f"{P}_p_set:poll_sec"))
    kb.row(B(f"🕒 Часовой пояс ({_tz_label(int(s.get('tz_offset_hours', 3) or 0))}): {s.get('tz_offset_hours', 3)}",
             callback_data=f"{P}_p_set:tz_offset_hours"))
    kb.row(_back_btn(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    _edit(call, "<b>⚙️ Параметры</b>\n\n"
                "💱 <b>Валюта</b> — в какой валюте списывать с вашего баланса KOSell за аренду.\n"
                "⭐ <b>Мин. звёзд</b> - минимальное количество звезд для получения бонуса за отзыв.\n"
                "Остальное — нажмите для ввода значения.", kb)


def tg_p_cur(call):
    SETTINGS["currency"] = "USD" if SETTINGS.get("currency") == "RUB" else "RUB"
    save_settings()
    tg_params(call)


def tg_p_stars(call):
    cur = int(SETTINGS.get("review_bonus_min_stars", 5) or 5)
    SETTINGS["review_bonus_min_stars"] = (cur % 5) + 1
    save_settings()
    tg_params(call)


def tg_p_set(call):
    key = call.data.split(":")[1]
    tg.set_state(call.message.chat.id, call.message.id, call.from_user.id, f"{P}_pset:{key}")
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{P}_params"))
    desc = PARAM_DESC.get(key, "")
    cur = SETTINGS.get(key)
    _edit(call, f"<b>✏️ Изменение параметра</b>\n\n{desc}\n\nТекущее значение: <b>{cur}</b>\n\nОтправьте новое число:", kb)


def tg_texts(call):
    page = int(call.data.split(":")[1]) if ":" in call.data else 0
    keys = list(DEFAULT_TEXTS.keys())
    if page * 7 >= len(keys):
        page = max(0, (len(keys) - 1) // 7)
    _save_page("texts", page)
    per = 7
    start = page * per
    chunk = keys[start:start + per]
    kb = K()
    for key in chunk:
        kb.row(B(f"✏️ {TEXT_LABELS.get(key, key)}", callback_data=f"{P}_text:{key}"))
    nav, _ = _nav(f"{P}_texts:", page, len(keys), per)
    if nav:
        kb.row(*nav)
    kb.row(_back_btn(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    _edit(call, "<b>✏️ Тексты для FunPay</b>\n\nВсе сообщения покупателям можно изменить.", kb)


def tg_text_edit(call):
    key = call.data.split(":", 1)[1]
    tg.set_state(call.message.chat.id, call.message.id, call.from_user.id, f"{P}_settext:{key}")
    cur = get_text(key)
    placeholders = re.findall(r"\{(\w+)\}", DEFAULT_TEXTS.get(key, ""))
    ph = ", ".join(f"<code>{{{p}}}</code>" for p in dict.fromkeys(placeholders)) or "нет"
    kb = K()
    kb.row(B("♻️ Сбросить по умолчанию", callback_data=f"{P}_textreset:{key}"))
    kb.row(B("❌ Отмена", callback_data=f"{P}_texts:{_page_of('texts')}"))
    _edit(call, f"<b>✏️ {TEXT_LABELS.get(key, key)}</b>\n\n"
                f"Текущий текст:\n<code>{_html.escape(cur)}</code>\n\n"
                f"Доступные переменные: {ph}\n\nОтправьте новый текст:", kb)


def tg_text_reset(call):
    key = call.data.split(":", 1)[1]
    SETTINGS["texts"][key] = DEFAULT_TEXTS.get(key, "")
    save_settings()
    bot.answer_callback_query(call.id, "♻️ Сброшено")
    call.data = f"{P}_texts:{_page_of('texts')}"
    tg_texts(call)


def tg_msg_dispatch(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = (message.text or "").strip()
    sd = tg.get_state(chat_id, user_id)
    if not sd:
        return
    state = sd.get("state", "") if isinstance(sd, dict) else ""
    if not state.startswith(P):
        return

    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    def done_kb(cb):
        kb = K()
        kb.row(B("◀️ Назад", callback_data=cb))
        return kb

    if state == f"{P}_set_apikey":
        tg.clear_state(chat_id, user_id, True)
        key = _sanitize_key(text)
        back_kb = done_kb(f"{CBT.PLUGIN_SETTINGS}:{UUID}")
        if not key:
            try:
                bot.send_message(chat_id, "❌ Пустой ключ. Отправьте API-ключ ещё раз.", reply_markup=back_kb)
            except Exception:
                pass
            return
        SETTINGS["api_key"] = key
        save_settings()
        bal = None
        for _ in range(2):
            try:
                api = _get_api()
                bal = api.balance() if api else None
            except Exception as e:
                logger.warning(f"{LP} apikey balance check error: {e}")
                bal = None
            if bal:
                break
            time.sleep(1.5)
        if bal:
            extra = f"\nБаланс: {bal.get('balance_rub', 0)} RUB / {bal.get('balance_usd', 0)} USD"
        else:
            extra = ("\n⚠️ Ключ сохранён, но баланс получить не удалось. "
                     "Это может быть временный сбой сети/KOSell — проверьте позже. "
                     "Если повторяется — проверьте правильность ключа.")
        try:
            bot.send_message(chat_id, f"✅ API ключ сохранён (длина {len(key)}).{extra}", reply_markup=back_kb)
        except Exception as e:
            logger.error(f"{LP} apikey send_message error: {e}")

    elif state.startswith(f"{P}_set_hours:"):
        idx = int(state.split(":")[1])
        tg.clear_state(chat_id, user_id, True)
        if text.isdigit() and idx < len(MAPPINGS):
            MAPPINGS[idx]["hours"] = max(KOSELL_MIN_HOURS, min(720, int(text)))
            save_mappings()
            bot.send_message(chat_id, f"✅ Часы: {MAPPINGS[idx]['hours']}", reply_markup=done_kb(f"{P}_map:{idx}"))
        else:
            bot.send_message(chat_id, "❌ Нужно число 3-720.", reply_markup=done_kb(f"{P}_map:{idx}"))

    elif state == f"{P}_manual_lot":
        tg.clear_state(chat_id, user_id, True)
        m = re.search(r"id=(\d+)", text) or re.search(r"(\d{4,})", text)
        if not m:
            bot.send_message(chat_id, "❌ Не нашёл ID. Отправьте число или ссылку вида .../offer?id=12345.",
                             reply_markup=done_kb(f"{P}_maps:{_page_of('maps')}"))
            return
        lot_id = m.group(1)
        _addmap_tmp[user_id] = {"lot_id": lot_id, "subcategory_id": None, "title_keyword": ""}
        _load_products_cache(user_id)
        text_pl, kb_pl = _product_list_kb(user_id, 0)
        bot.send_message(chat_id, f"✅ Лот {lot_id} принят.\n\n" + text_pl, reply_markup=kb_pl, parse_mode="HTML")

    elif state == f"{P}_prodsearch":
        tg.clear_state(chat_id, user_id, True)
        info = _addmap_tmp.setdefault(user_id, {})
        q = text.lower()
        info["_filtered"] = [p for p in (info.get("_all") or []) if q in str(p.get("name", "")).lower()]
        text_pl, kb_pl = _product_list_kb(user_id, 0)
        bot.send_message(chat_id, text_pl, reply_markup=kb_pl, parse_mode="HTML")

    elif state == f"{P}_prodmanual":
        tg.clear_state(chat_id, user_id, True)
        info = _addmap_tmp.setdefault(user_id, {})
        m = re.search(r"\d+", text)
        if not m:
            bot.send_message(chat_id, "❌ Нужен числовой ID товара.", reply_markup=done_kb(f"{P}_maps:{_page_of('maps')}"))
            return
        pid = m.group()
        info["product_id"] = int(pid)
        info["product_name"] = _resolve_product_name(user_id, pid)
        msg = bot.send_message(
            chat_id,
            f"Товар: <b>{_html.escape(str(info['product_name']))}</b>\n\n"
            f"Сколько часов аренды выдавать за покупку 1 шт лота? Введите число 3-720:",
            parse_mode="HTML")
        tg.set_state(chat_id, msg.message_id, user_id, f"{P}_set_newhours")

    elif state == f"{P}_set_newhours":
        tg.clear_state(chat_id, user_id, True)
        info = _addmap_tmp.get(user_id, {})
        if not text.isdigit() or "product_id" not in info:
            bot.send_message(chat_id, "❌ Нужно число 3-720.", reply_markup=done_kb(f"{P}_maps:{_page_of('maps')}"))
            return
        for k in ("_all", "_filtered"):
            info.pop(k, None)
        info["hours"] = max(KOSELL_MIN_HOURS, min(720, int(text)))
        info["currency"] = SETTINGS.get("currency", "RUB")
        MAPPINGS.append({
            "lot_id": info.get("lot_id"),
            "subcategory_id": info.get("subcategory_id"),
            "title_keyword": info.get("title_keyword", ""),
            "product_id": info.get("product_id"),
            "product_name": info.get("product_name", ""),
            "hours": info["hours"],
            "currency": info["currency"],
        })
        save_mappings()
        _addmap_tmp.pop(user_id, None)
        bot.send_message(chat_id, f"✅ Привязка создана: {MAPPINGS[-1]['product_name']} · "
                                  f"{MAPPINGS[-1]['hours']}ч · лот {MAPPINGS[-1]['lot_id']}",
                         reply_markup=done_kb(f"{P}_maps:{_page_of('maps')}"))

    elif state.startswith(f"{P}_pset:"):
        key = state.split(":")[1]
        tg.clear_state(chat_id, user_id, True)
        cur_sym = {"USD": "$", "RUB": "₽"}.get(SETTINGS.get("currency", "RUB"), SETTINGS.get("currency", ""))
        labels = {
            "friend_minutes": "Окно режима «друг» (мин)",
            "review_bonus_hours": "Бонус за отзыв (ч)",
            "balance_threshold": f"Порог баланса ({cur_sym})",
            "poll_sec": "Проверка склада/баланса (сек)",
            "ac_min_price": "Мин. цена лота (₽)",
            "tz_offset_hours": "Часовой пояс (ч)",
        }
        nice = labels.get(key, key)
        float_keys = {"balance_threshold", "ac_min_price"}
        try:
            num = float(text.replace(",", "."))
            if key in float_keys:
                val = round(num, 2)
                if key == "ac_min_price" and val < 1:
                    val = 1.0
            else:
                val = int(num)
                if num != val:
                    raise ValueError("only_int")
                if key == "tz_offset_hours":
                    val = max(-12, min(14, val))
            SETTINGS[key] = val
            save_settings()
            if key == "ac_min_price":
                t = _ac_tpl()
                if t:
                    t["min_price"] = val
                    SETTINGS["ac_template"] = t
                    save_settings()
                try:
                    _price_cache.clear()
                except Exception:
                    pass
            bot.send_message(chat_id, f"✅ <b>{nice}</b>: {_money(val)}",
                             reply_markup=done_kb(f"{P}_params"), parse_mode="HTML")
        except ValueError as ve:
            if str(ve) == "only_int":
                bot.send_message(chat_id, "❌ Здесь нужно целое число.", reply_markup=done_kb(f"{P}_params"))
            else:
                bot.send_message(chat_id, "❌ Нужно число.", reply_markup=done_kb(f"{P}_params"))
        except Exception:
            bot.send_message(chat_id, "❌ Нужно число.", reply_markup=done_kb(f"{P}_params"))

    elif state.startswith(f"{P}_settext:"):
        key = state.split(":", 1)[1]
        tg.clear_state(chat_id, user_id, True)
        SETTINGS["texts"][key] = text
        save_settings()
        bot.send_message(chat_id, f"✅ Текст «{key}» обновлён.", reply_markup=done_kb(f"{P}_texts:{_page_of('texts')}"))

    elif state == f"{P}_ac_w_dcustom":
        tg.clear_state(chat_id, user_id, True)
        hrs = []
        for tok in re.split(r"[,\s]+", text.strip()):
            if tok.isdigit():
                h = max(KOSELL_MIN_HOURS, min(720, int(tok)))
                if h not in hrs:
                    hrs.append(h)
        if hrs:
            _ac_wizard.setdefault(user_id, {})["durations"] = sorted(hrs)
            _ac_w_goto(chat_id, user_id, f"{P}_ac_w_sum_ru",
                       f"<b>Шаг 2/6 — краткое описание (RU)</b>\n\n{AC_PLACEHOLDER_HINT}\n\n"
                       f"Пример:\n<code>🟩 АВТОВЫДАЧА {PH_GAME} STEAM {PH_TIME}</code>")
        else:
            _wiz_msg(chat_id, user_id, "❌ Пример: 3, 24, 72, 168\nОтправьте часы ещё раз.",
                     done_kb(f"{P}_ac"))
            tg.set_state(chat_id, _ac_wizard.get(user_id, {}).get("message_id", message.message_id),
                         user_id, f"{P}_ac_w_dcustom")

    elif state == f"{P}_ac_w_sum_ru":
        tg.clear_state(chat_id, user_id, True)
        _ac_wizard.setdefault(user_id, {})["summary_ru"] = text
        _ac_w_goto(chat_id, user_id, f"{P}_ac_w_sum_en",
                   "<b>Шаг 3/6 — краткое описание (EN)</b>\n\n"
                   f"Тот же смысл на английском. {PH_TIME} / {PH_GAME} работают так же.\n"
                   f"До ~70 символов.")

    elif state == f"{P}_ac_w_sum_en":
        tg.clear_state(chat_id, user_id, True)
        _ac_wizard.setdefault(user_id, {})["summary_en"] = text
        _ac_w_goto(chat_id, user_id, f"{P}_ac_w_desc_ru",
                   f"<b>Шаг 4/6 — подробное описание (RU)</b>\n\n{AC_PLACEHOLDER_HINT}")

    elif state == f"{P}_ac_w_desc_ru":
        tg.clear_state(chat_id, user_id, True)
        _ac_wizard.setdefault(user_id, {})["desc_ru"] = text
        _ac_w_goto(chat_id, user_id, f"{P}_ac_w_desc_en",
                   "<b>Шаг 5/6 — подробное описание (EN)</b>\n\nАнглийская версия описания.")

    elif state == f"{P}_ac_w_desc_en":
        tg.clear_state(chat_id, user_id, True)
        _ac_wizard.setdefault(user_id, {})["desc_en"] = text
        kb = K()
        kb.row(B("📈 Наценка % от себестоимости KOSell", callback_data=f"{P}_ac_w_pmode:markup"))
        kb.row(B("💰 Своя цена ₽ на каждое время", callback_data=f"{P}_ac_w_pmode:fixed"))
        kb.row(B("❌ Отмена", callback_data=f"{P}_ac"))
        _wiz_msg(chat_id, user_id,
                 "<b>Шаг 6/6 — цены</b>\n\n"
                 "• <b>Наценка</b> — берём себестоимость KOSell по каждому времени и добавляем ваш %.\n"
                 "• <b>Своя цена</b> — вы вручную вписываете цену в ₽ для покупателя отдельно "
                 "на каждое время (3 часа, 1 день, …).\n\n"
                 "В обоих случаях комиссия FunPay учитывается автоматически — "
                 "покупатель видит ровно вашу цену.", kb)

    elif state == f"{P}_ac_w_markup":
        tg.clear_state(chat_id, user_id, True)
        try:
            _ac_wizard.setdefault(user_id, {})["markup_percent"] = round(float(text.replace(",", ".")), 2)
        except Exception:
            _wiz_msg(chat_id, user_id, "❌ Нужно число (процент). Отправьте ещё раз, напр. <code>50</code>.",
                     done_kb(f"{P}_ac"))
            tg.set_state(chat_id, _ac_wizard.get(user_id, {}).get("message_id", message.message_id),
                         user_id, f"{P}_ac_w_markup")
            return
        _ac_finish(chat_id, user_id)

    elif state == f"{P}_ac_reprice_pct":
        tg.clear_state(chat_id, user_id, True)
        try:
            pct = round(float(text.replace(",", ".")), 2)
        except Exception:
            _wiz_msg(chat_id, user_id, "❌ Нужно число (процент). Напр. <code>8</code>.", done_kb(f"{P}_ac"))
            tg.set_state(chat_id, _ac_wizard.get(user_id, {}).get("message_id", message.message_id),
                         user_id, f"{P}_ac_reprice_pct")
            return
        t = _ac_tpl()
        t["price_mode"] = "markup"
        t["markup_percent"] = pct
        SETTINGS["ac_template"] = t
        save_settings()
        _price_cache.clear()
        mid = _ac_wizard.get(user_id, {}).get("message_id", message.message_id)
        if not _ac_begin("Пересчёт цен…"):
            _wiz_msg(chat_id, user_id, "Уже идёт другой процесс — дождитесь завершения.", done_kb(f"{P}_ac"))
            return
        _wiz_msg(chat_id, user_id,
                 f"♻️ Запускаю пересчёт по наценке <b>{_pct(pct)}%</b>…\n\n<code>{_bar(0, 1)}</code>", None)
        threading.Thread(target=_reprice_run, args=(user_id, chat_id, mid), daemon=True).start()

    elif state == f"{P}_ac_e_markup":
        tg.clear_state(chat_id, user_id, True)
        try:
            pct = round(float(text.replace(",", ".")), 2)
        except Exception:
            _wiz_msg(chat_id, user_id, "❌ Нужно число (процент). Напр. <code>5</code>.", done_kb(f"{P}_ac_edit"))
            tg.set_state(chat_id, _ac_wizard.get(user_id, {}).get("message_id", message.message_id),
                         user_id, f"{P}_ac_e_markup")
            return
        t = _ac_tpl()
        t["price_mode"] = "markup"
        t["markup_percent"] = pct
        SETTINGS["ac_template"] = t
        save_settings()
        _price_cache.clear()
        _wiz_msg(chat_id, user_id,
                 f"✅ Наценка обновлена: <b>{_pct(pct)}%</b>.\n\nДля уже созданных лотов нажмите "
                 f"«♻️ Пересчитать цены».", done_kb(f"{P}_ac_edit"))

    elif state == f"{P}_ac_e_title":
        tg.clear_state(chat_id, user_id, True)
        ru, en = (text.split("|||", 1) + [""])[:2]
        ru = ru.strip()
        en = en.strip() or _ascii(ru)
        t = _ac_tpl()
        t["summary_ru"] = ru
        t["summary_en"] = en
        SETTINGS["ac_template"] = t
        save_settings()
        _wiz_msg(chat_id, user_id,
                 f"✅ Название обновлено.\n\nRU: <code>{_html.escape(ru)}</code>\n"
                 f"EN: <code>{_html.escape(en)}</code>", done_kb(f"{P}_ac_edit"))

    elif state == f"{P}_ac_e_desc":
        tg.clear_state(chat_id, user_id, True)
        ru, en = (text.split("|||", 1) + [""])[:2]
        ru = ru.strip()
        en = en.strip() or _ascii(ru)
        t = _ac_tpl()
        t["desc_ru"] = ru
        t["desc_en"] = en
        SETTINGS["ac_template"] = t
        save_settings()
        _wiz_msg(chat_id, user_id, "✅ Описание обновлено.", done_kb(f"{P}_ac_edit"))

    elif state == f"{P}_ac_w_fixprice":
        tg.clear_state(chat_id, user_id, True)
        w = _ac_wizard.setdefault(user_id, {})
        durs = w.get("durations") or [3, 24, 72, 168]
        idx = int(w.get("fix_idx", 0))
        try:
            price = float(text.replace(",", "."))
        except Exception:
            _wiz_msg(chat_id, user_id,
                     f"❌ Нужно число. Цена для <b>{_format_kostime(durs[idx])}</b> (₽):",
                     done_kb(f"{P}_ac"))
            tg.set_state(chat_id, w.get("message_id", message.message_id), user_id, f"{P}_ac_w_fixprice")
            return
        w.setdefault("fixed_prices", {})[str(durs[idx])] = price
        w["fix_idx"] = idx + 1
        _ac_ask_fixprice(chat_id, user_id)

    elif state == f"{P}_ac_w_usd":
        tg.clear_state(chat_id, user_id, True)
        try:
            rate = float(text.replace(",", "."))
        except Exception:
            _wiz_msg(chat_id, user_id, "❌ Нужно число (курс). Напр. <code>95</code>.", done_kb(f"{P}_ac"))
            tg.set_state(chat_id, _ac_wizard.get(user_id, {}).get("message_id", message.message_id),
                         user_id, f"{P}_ac_w_usd")
            return
        _ac_wizard.setdefault(user_id, {})["usd_rub"] = rate
        SETTINGS["ac_usd_rub"] = rate
        save_settings()
        _ac_finish(chat_id, user_id)


# ============================================================================
#  АВТОСОЗДАНИЕ ЛОТОВ
# ============================================================================

AC_PLACEHOLDER_HINT = (
    f"<b>Плейсхолдеры</b> (подставляются в каждый лот автоматически):\n"
    f"• <code>{PH_TIME}</code> — время аренды со склонением (3 часа, 1 день, 2 дня…)\n"
    f"• <code>{PH_GAME}</code> — название игры\n\n"
    f"Не указывайте игру и время вручную — только плейсхолдеры.\n\n"
    f"<b>Команды покупателя</b> (добавьте их в подробное описание):\n"
    f"• <code>!код логин</code> — прислать актуальный код Steam Guard для выданного аккаунта.\n"
    f"• <code>!прод логин</code> — продлить аренду этого же аккаунта (после повторной оплаты лота).\n"
    f"• <code>!friend</code> — режим «для друга»: см. ниже.\n\n"
    f"<b>Как работает <code>!friend</code></b> (объясните это покупателю):\n"
    f"По умолчанию, если покупатель оплачивает лот повторно, бот <b>продлевает</b> его текущий "
    f"аккаунт (то же время на том же аккаунте). Команда <code>!friend</code> включает на несколько "
    f"минут режим, при котором следующая оплата выдаёт <b>новый отдельный аккаунт</b>, а не продлевает "
    f"старый. Нужна, когда хотят играть вдвоём (себе и другу) или взять второй аккаунт параллельно.\n"
    f"Порядок: написать <code>!friend</code> → оплатить лот ещё раз в течение окна (по умолчанию "
    f"~10 мин) → бот выдаст второй аккаунт. Окно настраивается в параметрах плагина.\n\n"
    f"<i>{FP_LIMITS_HINT}</i>"
)


def _ac_durations_label(tpl: "dict | None" = None) -> str:
    t = tpl or _ac_tpl()
    ds = t.get("durations") or []
    return ", ".join(f"{h}ч" if h < 48 else f"{h//24}д" for h in ds) or "—"


def _ac_price_label(tpl: "dict | None" = None) -> str:
    t = tpl or _ac_tpl()
    if t.get("price_mode") == "fixed":
        fp = t.get("fixed_prices") or {}
        if fp:
            parts = []
            for h in (t.get("durations") or []):
                v = fp.get(str(h), fp.get(h))
                if v is not None:
                    lbl = f"{h}ч" if h < 48 else f"{h//24}д"
                    parts.append(f"{lbl}={int(v)}₽")
            return "фикс. " + ", ".join(parts) if parts else "фикс."
        return f"фикс. {int(t.get('fixed_public_rub') or 0)}₽"
    return f"наценка {_pct(t.get('markup_percent', 0))}%"


def _ac_count_missing() -> int:
    api = _get_api()
    if not api or not _ac_has_template():
        return 0
    tpl = _ac_tpl()
    have = _existing_lot_keys()
    n = 0
    prods = {int(p["id"]): p for p in (_products(api) or [])}
    for sg in _all_sellable_games(api):
        prod = prods.get(sg["product_id"])
        if not prod:
            continue
        for h in _durations_for(prod, tpl):
            if (sg["product_id"], h) not in have:
                n += 1
    return n


def tg_ac_menu(call):
    try:
        bot.answer_callback_query(call.id, "⏳ Загружаю каталог…")
    except Exception:
        pass
    api = _get_api()
    mp = _fetch_map()
    api_err = ""
    sellable = 0
    if not api:
        api_err = "🔑 <b>API-ключ KOSell не задан.</b> Откройте «🔑 API ключ» и вставьте ключ."
    else:
        prods = _products(api)
        if prods is None:
            api_err = ("⛔️ <b>KOSell API не отвечает или ключ неверный</b> (401 invalid_api_key).\n"
                       "Проверьте ключ в «🔑 API ключ» — без него список игр пуст.")
        else:
            sellable = len(_all_sellable_games(api))
    missing = _ac_count_missing() if (_ac_has_template() and not api_err) else 0
    run_banner = ""
    if _ac_running.get("on"):
        rt = _ac_running.get("title", "Процесс")
        rd, rtot = _ac_running.get("done", 0), _ac_running.get("total", 0)
        rv, rc = _ac_running.get("verb", "Готово"), _ac_running.get("created", 0)
        eta = ""
        st = _ac_running.get("started")
        if st and rd and rd < rtot:
            rem = max(0.0, (time.time() - st) / rd * (rtot - rd))
            eta = f" · осталось {_fmt_eta(rem)}"
        run_banner = (f"⏳ <b>Идёт: {rt}</b>\n<code>{_bar(rd, rtot)}</code>\n"
                      f"{rv}: {rc} из {rtot}{eta}\n\n")
    kb = K()
    if _ac_running.get("on"):
        kb.row(B("🔄 Обновить статус", callback_data=f"{P}_ac"))
    if _ac_has_template():
        tpl = _ac_tpl()
        kb.row(B(f"➕ Создать недостающие ({missing})", callback_data=f"{P}_ac_miss"))
        kb.row(B("✏️ Изменить шаблон", callback_data=f"{P}_ac_edit"),
               B("⚙️ Заново", callback_data=f"{P}_ac_w_start"))
        kb.row(B("👁 Предпросмотр цен", callback_data=f"{P}_ac_prev:0"),
               B("♻️ Пересчитать цены", callback_data=f"{P}_ac_reprice"))
        text = (
            f"<b>📦 Создание лотов KOSell</b>\n\n"
            + (api_err + "\n\n" if api_err else "")
            + f"Шаблон сохранён. Длительности: <b>{_ac_durations_label(tpl)}</b>\n"
            f"Цены: <b>{_ac_price_label(tpl)}</b>\n\n"
            f"Игр на KOSell (карта + «Прочие»): <b>{sellable}</b>\n"
            f"Недостающих лотов: <b>{missing}</b>\n\n"
            f"Повторный запуск создаёт только то, чего ещё нет в привязках."
        )
    else:
        kb.row(B("▶️ Начать мастер настройки", callback_data=f"{P}_ac_w_start"))
        text = (
            f"<b>📦 Создание лотов KOSell</b>\n\n"
            + (api_err + "\n\n" if api_err else "")
            + f"Разовый мастер: задаёте шаблон текстов ({PH_TIME}, {PH_GAME}), "
            f"длительности и цены — затем бот создаёт лоты на FunPay.\n\n"
            f"Игр доступно: <b>{sellable}</b> (своя категория + «Прочие игры»)\n"
            f"Карта игр: <b>{len((mp or {}).get('games', []))}</b> записей"
        )
    dbg = "🟢" if SETTINGS.get("ac_debug") else "⚪️"
    kb.row(B("🔄 Обновить карту", callback_data=f"{P}_ac_refresh"),
           B(f"{dbg} Debug", callback_data=f"{P}_ac_dbg"))
    kb.row(B("🗑 Удалить ВСЕ лоты", callback_data=f"{P}_ac_delall"))
    kb.row(_back_btn(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    text = run_banner + text
    text += "\n\n<i>Данные кэшируются на 15 мин — после «Обновить карту» первый расчёт дольше.</i>"
    _edit(call, text, kb)


def tg_ac_dbg(call):
    SETTINGS["ac_debug"] = not SETTINGS.get("ac_debug")
    save_settings()
    state = "включён" if SETTINGS["ac_debug"] else "выключен"
    bot.answer_callback_query(call.id, f"Debug-режим {state}")
    tg_ac_menu(call)


def tg_ac_w_start(call):
    uid = call.from_user.id
    _ac_wizard[uid] = {"message_id": call.message.message_id}
    tg_ac_w_dur(call)


def tg_ac_w_dur(call):
    kb = K()
    for name, ds in DURATION_PRESETS.items():
        lbl = ", ".join(f"{h}ч" if h < 48 else f"{h//24}д" for h in ds)
        kb.row(B(f"{name}: {lbl}", callback_data=f"{P}_ac_w_dpre:{name}"))
    kb.row(B("✍️ Свой список", callback_data=f"{P}_ac_w_dcustom"))
    kb.row(_back_btn(f"{P}_ac"))
    _edit(call, "<b>Шаг 1/6 — длительности</b>\n\nСколько лотов на одну игру (по одному на каждое время):\n"
                "выберите пресет или введите часы через запятую.", kb)


def tg_ac_w_dpre(call):
    uid = call.from_user.id
    name = call.data.split(":", 1)[1]
    if name in DURATION_PRESETS:
        _ac_wizard.setdefault(uid, {})["durations"] = list(DURATION_PRESETS[name])
    tg.set_state(call.message.chat.id, call.message.message_id, uid, f"{P}_ac_w_sum_ru")
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{P}_ac"))
    _edit(call, f"<b>Шаг 2/6 — краткое описание (RU)</b>\n\n{AC_PLACEHOLDER_HINT}\n\n"
                f"Пример:\n<code>🟩 АВТОВЫДАЧА 🌿 АРЕНДА {PH_GAME} STEAM 🌿 {PH_TIME}</code>", kb)


def tg_ac_w_dcustom(call):
    tg.set_state(call.message.chat.id, call.message.message_id, call.from_user.id, f"{P}_ac_w_dcustom")
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{P}_ac"))
    _edit(call, "<b>✍️ Свой список длительностей</b>\n\nЧасы через запятую: <code>3, 24, 72, 168</code>", kb)


def _ac_w_goto(chat_id, uid, state, text):
    kb = K()
    kb.row(B("❌ Отмена", callback_data=f"{P}_ac"))
    mid = _wiz_msg(chat_id, uid, text, kb)
    tg.set_state(chat_id, mid, uid, state)


def _ac_ask_fixprice(chat_id, uid):
    w = _ac_wizard.setdefault(uid, {})
    durs = w.get("durations") or [3, 24, 72, 168]
    idx = int(w.get("fix_idx", 0))
    if idx >= len(durs):
        _ac_finish(chat_id, uid)
        return
    fp = w.get("fixed_prices") or {}
    done = "\n".join(
        f"  • {_format_kostime(d)} → {int(fp[str(d)])}₽"
        for d in durs[:idx] if str(d) in fp
    )
    h = durs[idx]
    txt = (f"<b>💰 Своя цена — {idx + 1}/{len(durs)}</b>\n\n"
           + (f"Уже задано:\n{done}\n\n" if done else "")
           + f"Цена <b>для покупателя</b> за <b>{_format_kostime(h)}</b> в рублях "
             f"(напр. <code>99</code>).\nКомиссия FunPay учтётся сама.")
    _ac_w_goto(chat_id, uid, f"{P}_ac_w_fixprice", txt)


def _ac_finish(chat_id, uid):
    mid = _ac_wizard.get(uid, {}).get("message_id")
    _ac_save_template_from_wizard(uid)
    n = len(_build_plan(uid, missing_only=True))
    kb = K()
    kb.row(B("👁 Предпросмотр цен", callback_data=f"{P}_ac_prev:0"))
    kb.row(B(f"➕ Создать недостающие ({n})", callback_data=f"{P}_ac_miss"))
    kb.row(B("◀️ В меню создания", callback_data=f"{P}_ac"))
    txt = (f"✅ <b>Шаблон сохранён.</b>\n\n"
           f"Доступно к созданию: <b>{n}</b> недостающих лотов.\n"
           f"Откройте предпросмотр цен или сразу «Создать недостающие».\n\n"
           f"<i>Если игр 0 — проверьте API-ключ KOSell в настройках плагина.</i>")
    if mid:
        try:
            bot.edit_message_text(txt, chat_id, mid, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")


def tg_ac_w_price_mode(call):
    mode = call.data.split(":")[-1]
    uid = call.from_user.id
    w = _ac_wizard.setdefault(uid, {})
    w["price_mode"] = mode
    w["message_id"] = call.message.message_id
    if mode == "markup":
        tg.set_state(call.message.chat.id, call.message.message_id, uid, f"{P}_ac_w_markup")
        kb = K()
        kb.add(B("❌ Отмена", callback_data=f"{P}_ac"))
        _edit(call, "<b>Наценка</b>\n\nБерём себестоимость KOSell по каждому времени и добавляем ваш %.\n"
                    "Отправьте процент, напр. <code>50</code> (= +50% к себестоимости):", kb)
    else:
        w["fix_idx"] = 0
        w["fixed_prices"] = {}
        bot.answer_callback_query(call.id)
        _ac_ask_fixprice(call.message.chat.id, uid)


def _ac_save_template_from_wizard(uid: int):
    w = _ac_wizard.get(uid, {})
    SETTINGS["ac_template"] = {
        "durations": w.get("durations") or [3, 24, 72, 168],
        "summary_ru": w.get("summary_ru", ""),
        "summary_en": w.get("summary_en", ""),
        "desc_ru": w.get("desc_ru", ""),
        "desc_en": w.get("desc_en", ""),
        "price_mode": w.get("price_mode", "markup"),
        "markup_percent": w.get("markup_percent", 50),
        "fixed_public_rub": w.get("fixed_public_rub"),
        "fixed_prices": w.get("fixed_prices") or {},
        "usd_rub": w.get("usd_rub", SETTINGS.get("ac_usd_rub", 95)),
        "min_price": SETTINGS.get("ac_min_price", 15),
    }
    save_settings()
    _ac_wizard.pop(uid, None)


def tg_ac_refresh(call):
    try:
        bot.answer_callback_query(call.id, "⏳ Обновляю карту и каталог…")
    except Exception:
        pass
    _fetch_map(force=True)
    _misc_cache.clear()
    _products_cache.update({"data": None, "ts": 0, "err": False})
    _pub_lots_cache.clear()
    _commission_cache.clear()
    _node_form_cache.clear()
    _price_cache.clear()
    _map_cache["warned"] = False
    _products(_get_api(), force=True)
    tg_ac_menu(call)


def tg_ac_preview(call, missing_only: bool = True):
    parts = call.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if not _ac_has_template():
        bot.answer_callback_query(call.id, "Сначала пройдите мастер", show_alert=True)
        return
    try:
        bot.answer_callback_query(call.id, "⏳ Считаю цены, подождите…")
    except Exception:
        pass
    api = _get_api()
    if not api or _products(api) is None:
        kb = K()
        kb.row(B("🔑 API ключ", callback_data=f"{P}_apikey"))
        kb.row(_back_btn(f"{P}_ac"))
        _edit(call, "⛔️ <b>Нет связи с KOSell.</b>\n\n"
                    "Список игр пуст — API-ключ не задан или неверный (401). "
                    "Проверьте ключ, затем повторите предпросмотр.", kb)
        return
    plan = (_ac_plan.get(call.from_user.id) or {}).get("items")
    if not plan:
        plan = _build_plan(call.from_user.id, missing_only=missing_only)
    tpl = _ac_tpl()
    if not plan:
        kb = K()
        kb.row(_back_btn(f"{P}_ac"))
        _edit(call, "✅ <b>Всё уже создано.</b>\n\n"
                    "Недостающих лотов нет — все пары «игра + время» из шаблона уже есть в привязках.\n"
                    "Появятся новые игры на KOSell или обновите карту — тогда здесь снова будут лоты.", kb)
        return
    games = {}
    for it in plan:
        games.setdefault((it["product_id"], it["game_name"], it["subcategory_id"]), []).append(it)
    keys = list(games.keys())
    per = 4
    start = page * per
    chunk = keys[start:start + per]
    mode_s = _ac_price_label(tpl)
    lines = [
        f"<b>👁 Предпросмотр</b> · лотов: <b>{len(plan)}</b> · стр. {page + 1}/{max(1, (len(keys)+per-1)//per)} · {mode_s}\n",
        f"<i>По каждому времени: себестоимость KOSell → ваша цена покупателю → "
        f"конкуренты на FunPay (5 низших).</i>\n",
    ]
    prods_by_id = {int(p["id"]): p for p in (_products(api) or [])}
    for k in chunk:
        pid, gname, sub_id = k
        prod = prods_by_id.get(pid, {"id": pid})
        its = sorted(games[k], key=lambda x: x["hours"])
        src = its[0].get("source", "")
        lines.append(f"\n🎮 <b>{_html.escape(gname[:42])}</b>"
                     f"{' · прочие' if src == 'misc' else ''}")
        for it in its:
            ht = _format_kostime(it["hours"])
            cost = _kosell_cost_rub(api, prod, it["hours"])
            pub = _public_price_rub(api, prod, it["hours"], tpl)
            low = _fp_lowest_prices_for(sub_id, it["hours"],
                                        gname if src == "misc" else None, 5)
            low_s = (" / ".join(_money(p) for p in low) + "₽") if low else "нет такого времени"
            lines.append(
                f"  <b>{ht}</b>\n"
                f"    KOSell: <b>{_money(cost)}₽</b>  →  "
                f"ваша: <b>{_money(pub)}₽</b>  ·  FP: {low_s}"
            )
    kb = K()
    nav, _ = _nav(f"{P}_ac_prev:", page, len(keys), per)
    if nav:
        kb.row(*nav)
    if plan:
        kb.row(B(f"✅ Создать {len(plan)} лотов", callback_data=f"{P}_ac_confirm"))
    kb.row(_back_btn(f"{P}_ac"))
    _edit(call, "\n".join(lines), kb)


def tg_ac_miss(call):
    if not _ac_has_template():
        bot.answer_callback_query(call.id, "Нет шаблона — пройдите мастер", show_alert=True)
        return
    try:
        bot.answer_callback_query(call.id, "⏳ Считаю недостающие лоты…")
    except Exception:
        pass
    if not _build_plan(call.from_user.id, missing_only=True):
        kb = K()
        kb.row(_back_btn(f"{P}_ac"))
        _edit(call, "✅ <b>Всё уже создано.</b>\n\nНедостающих лотов нет.", kb)
        return
    tg_ac_preview(call)


def tg_ac_confirm(call):
    uid = call.from_user.id
    if not (_ac_plan.get(uid) or {}).get("items"):
        _build_plan(uid, missing_only=True)
    plan = (_ac_plan.get(uid) or {}).get("items") or []
    if not plan:
        bot.answer_callback_query(call.id, "Нечего создавать", show_alert=True)
        return
    if _ac_running["on"]:
        bot.answer_callback_query(call.id, "Уже выполняется", show_alert=True)
        return
    kb = K()
    kb.row(B("🚀 Да, создать", callback_data=f"{P}_ac_go"))
    kb.row(_back_btn(f"{P}_ac_prev:0"))
    _edit(call, f"<b>Подтверждение</b>\n\nСоздать <b>{len(plan)}</b> лотов?\n"
                f"Лимит на раздел: {int(SETTINGS.get('ac_subcat_cap', 15))} лот/игру.", kb)


def tg_ac_go(call):
    uid = call.from_user.id
    plan = (_ac_plan.get(uid) or {}).get("items") or []
    if not plan:
        bot.answer_callback_query(call.id, "Нет плана", show_alert=True)
        return
    if not _ac_begin("Создание лотов…"):
        bot.answer_callback_query(call.id, "Уже идёт другой процесс", show_alert=True)
        return
    eta = max(1, len(plan) * 3 // 60)
    _edit(call, f"🚀 <b>Запускаю создание {len(plan)} лотов</b>\n\n"
                f"<code>{_bar(0, len(plan))}</code>\n\n"
                f"Примерно ~{eta} мин. Прогресс обновляется здесь.", None)
    chat_id = call.message.chat.id
    mid = call.message.id
    threading.Thread(target=_autocreate_run, args=(uid, chat_id, mid), daemon=True).start()


def tg_ac_delall(call):
    if _ac_running["on"]:
        bot.answer_callback_query(call.id, "Уже выполняется", show_alert=True)
        return
    linked = len([m for m in MAPPINGS if m.get("lot_id")])
    kb = K()
    kb.row(B(f"🗑 Удалить лоты плагина + привязки ({linked})", callback_data=f"{P}_ac_del_lots"))
    kb.row(B(f"🔗 Удалить только привязки ({len(MAPPINGS)})", callback_data=f"{P}_ac_del_links"))
    kb.row(B("◀️ Отмена", callback_data=f"{P}_ac"))
    _edit(call,
          "<b>🗑 Удаление лотов / привязок</b>\n\n"
          f"Привязанных лотов: <b>{linked}</b> · всего привязок: <b>{len(MAPPINGS)}</b>\n\n"
          "• <b>Лоты плагина + привязки</b> — удаляет с FunPay все привязанные/созданные плагином "
          "лоты и очищает привязки. Чужие/ручные лоты не трогает.\n"
          "• <b>Только привязки</b> — оставляет лоты на FunPay, но очищает связи в плагине "
          "(после этого автосоздание будет считать их «отсутствующими»).\n\n"
          "Выберите действие:", kb)


def tg_ac_del_links(call):
    if _ac_running["on"]:
        bot.answer_callback_query(call.id, "Уже выполняется", show_alert=True)
        return
    kb = K()
    kb.row(B(f"✅ Да, очистить привязки ({len(MAPPINGS)})", callback_data=f"{P}_ac_del_links_yes"))
    kb.row(B("◀️ Отмена", callback_data=f"{P}_ac_delall"))
    _edit(call, f"🔗 <b>Подтверждение</b>\n\nОчистить <b>{len(MAPPINGS)}</b> привязок в плагине?\n"
                f"Лоты на FunPay <b>останутся</b>, но плагин будет считать их «отсутствующими».", kb)


def tg_ac_del_links_yes(call):
    if _ac_running["on"]:
        bot.answer_callback_query(call.id, "Уже выполняется", show_alert=True)
        return
    n = len(MAPPINGS)
    MAPPINGS.clear()
    save_mappings()
    if HIDDEN:
        HIDDEN.clear()
        save_hidden()
    bot.answer_callback_query(call.id, f"Очищено привязок: {n}")
    kb = K()
    kb.row(B("◀️ В меню создания", callback_data=f"{P}_ac"))
    _edit(call, f"🔗 <b>Привязки очищены.</b>\n\nУдалено связей: <b>{n}</b>. "
                f"Лоты на FunPay остались на месте.", kb)


def tg_ac_del_lots(call):
    if _ac_running["on"]:
        bot.answer_callback_query(call.id, "Уже выполняется", show_alert=True)
        return
    linked = len([m for m in MAPPINGS if m.get("lot_id")])
    if not linked:
        bot.answer_callback_query(call.id, "Нет привязанных лотов", show_alert=True)
        return
    kb = K()
    kb.row(B(f"✅ Да, удалить {linked} лотов", callback_data=f"{P}_ac_del_lots_yes"))
    kb.row(B("◀️ Отмена", callback_data=f"{P}_ac_delall"))
    _edit(call, f"🗑 <b>Подтверждение</b>\n\nБезвозвратно удалить с FunPay <b>{linked}</b> привязанных лотов "
                f"и очистить их привязки?\nЧужие/ручные лоты не затрагиваются.", kb)


def tg_ac_del_lots_yes(call):
    if not any(m.get("lot_id") for m in MAPPINGS):
        bot.answer_callback_query(call.id, "Нет привязанных лотов", show_alert=True)
        return
    if not _ac_begin("Удаление лотов…"):
        bot.answer_callback_query(call.id, "Уже идёт другой процесс", show_alert=True)
        return
    uid = call.from_user.id
    _edit(call, f"🗑 <b>Удаляю лоты плагина…</b>\n\n<code>{_bar(0, 1)}</code>\n\nПрогресс обновляется здесь.", None)
    threading.Thread(target=_delete_plugin_run, args=(uid, call.message.chat.id, call.message.id),
                     daemon=True).start()


def tg_ac_reprice(call):
    uid = call.from_user.id
    if not _ac_has_template():
        bot.answer_callback_query(call.id, "Сначала настройте шаблон", show_alert=True)
        return
    if _ac_running["on"]:
        bot.answer_callback_query(call.id, "Уже выполняется", show_alert=True)
        return
    n = len([m for m in MAPPINGS if m.get("auto_created") and m.get("lot_id")])
    if n == 0:
        bot.answer_callback_query(call.id, "Нет авто-созданных лотов для пересчёта", show_alert=True)
        return
    _ac_wizard[uid] = {"message_id": call.message.message_id}
    t = _ac_tpl()
    cur = _pct(t.get("markup_percent", 0))
    floor = _money(_min_price_floor(t))
    kb = K()
    kb.add(B("❌ Отмена", callback_data=f"{P}_ac"))
    _wiz_msg(call.message.chat.id, uid,
             f"<b>♻️ Пересчёт цен</b>\n\n"
             f"Авто-лотов под управлением: <b>{n}</b>\n"
             f"Текущая наценка: <b>{cur}%</b> · мин. цена: <b>{floor}₽</b>\n\n"
             f"Отправьте <b>новый процент наценки</b> (напр. <code>80</code>) — плагин пересчитает "
             f"себестоимость×наценку и обновит цену во всех этих лотах на FunPay.\n"
             f"<i>Мин. цена остаётся «полом»: всё, что выходит дешевле, ставится в {floor}₽.</i>", kb)
    tg.set_state(call.message.chat.id, call.message.message_id, uid, f"{P}_ac_reprice_pct")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def tg_ac_edit(call):
    if not _ac_has_template():
        bot.answer_callback_query(call.id, "Сначала настройте шаблон", show_alert=True)
        return
    t = _ac_tpl()
    kb = K()
    kb.row(B("📈 Наценка %", callback_data=f"{P}_ac_e:markup"))
    kb.row(B("📝 Название (RU+EN)", callback_data=f"{P}_ac_e:title"))
    kb.row(B("📄 Описание (RU+EN)", callback_data=f"{P}_ac_e:desc"))
    kb.row(_back_btn(f"{P}_ac"))
    txt = (
        f"<b>✏️ Изменить шаблон</b>\n\n"
        f"📈 Наценка: <b>{_pct(t.get('markup_percent', 0))}%</b>\n\n"
        f"📝 Название RU:\n<code>{_html.escape(t.get('summary_ru', '') or '—')}</code>\n"
        f"📝 Название EN:\n<code>{_html.escape(t.get('summary_en', '') or '—')}</code>\n\n"
        f"📄 Описание RU:\n<code>{_html.escape((t.get('desc_ru', '') or '—')[:120])}</code>\n"
        f"📄 Описание EN:\n<code>{_html.escape((t.get('desc_en', '') or '—')[:120])}</code>\n\n"
        f"<i>{PH_TIME} → время, {PH_GAME} → игра. Изменения вступят в силу для новых лотов; "
        f"для уже созданных — нажмите «Пересчитать цены» (для наценки).</i>"
    )
    _edit(call, txt, kb)


def tg_ac_edit_field(call):
    field = call.data.split(":", 1)[1]
    uid = call.from_user.id
    t = _ac_tpl()
    _ac_wizard[uid] = {"message_id": call.message.message_id}
    if field == "markup":
        kb = K()
        kb.add(B("❌ Отмена", callback_data=f"{P}_ac_edit"))
        _wiz_msg(call.message.chat.id, uid,
                 f"<b>📈 Наценка</b>\n\nТекущая: <b>{_pct(t.get('markup_percent', 0))}%</b>\n\n"
                 f"Отправьте новый процент (напр. <code>50</code>). Цены новых лотов будут "
                 f"= себестоимость KOSell × (1 + %).", kb)
        tg.set_state(call.message.chat.id, call.message.message_id, uid, f"{P}_ac_e_markup")
    elif field == "title":
        kb = K()
        kb.add(B("❌ Отмена", callback_data=f"{P}_ac_edit"))
        _wiz_msg(call.message.chat.id, uid,
                 f"<b>📝 Название (краткое описание)</b>\n\n"
                 f"Отправьте в одном сообщении RU и EN через <code>|||</code>:\n"
                 f"<code>🟩 АРЕНДА {PH_GAME} STEAM 🌿 {PH_TIME} ||| RENTAL {PH_GAME} STEAM {PH_TIME}</code>\n\n"
                 f"Без <code>|||</code> — EN сделаю автоматически из RU (латиницей).", kb)
        tg.set_state(call.message.chat.id, call.message.message_id, uid, f"{P}_ac_e_title")
    elif field == "desc":
        kb = K()
        kb.add(B("❌ Отмена", callback_data=f"{P}_ac_edit"))
        _wiz_msg(call.message.chat.id, uid,
                 f"<b>📄 Описание (подробное)</b>\n\n"
                 f"Отправьте в одном сообщении RU и EN через <code>|||</code>:\n"
                 f"<code>Текст RU {PH_GAME} ||| Text EN {PH_GAME}</code>\n\n"
                 f"Без <code>|||</code> — EN сделаю автоматически из RU.", kb)
        tg.set_state(call.message.chat.id, call.message.message_id, uid, f"{P}_ac_e_desc")
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def init(cardinal: "Cardinal"):
    global tg, bot, cardinal_instance
    tg = cardinal.telegram
    bot = cardinal.telegram.bot if cardinal.telegram else None
    cardinal_instance = cardinal

    load_all()
    try:
        _finalize_update_after_restart()
    except Exception as e:
        logger.error(f"{LP} finalize update error: {e}")
    _start_poller()

    if tg and bot:
        handlers = [
            (tg_main, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in c.data),
            (tg_main, lambda c: c.data == f"{P}_main"),
            (tg_raise_toggle, lambda c: c.data == f"{P}_raise"),
            (tg_info, lambda c: c.data == f"{P}_info"),
            (tg_restart, lambda c: c.data == f"{P}_restart"),
            (tg_toggle, lambda c: c.data.startswith(f"{P}_toggle:")),
            (tg_apikey, lambda c: c.data == f"{P}_apikey"),
            (tg_balance, lambda c: c.data == f"{P}_balance"),
            (tg_maps, lambda c: c.data.startswith(f"{P}_maps:")),
            (tg_map_detail, lambda c: c.data.startswith(f"{P}_map:")),
            (tg_map_hours, lambda c: c.data.startswith(f"{P}_map_hours:")),
            (tg_map_cur, lambda c: c.data.startswith(f"{P}_map_cur:")),
            (tg_map_del, lambda c: c.data.startswith(f"{P}_map_del:")),
            (tg_map_del_yes, lambda c: c.data.startswith(f"{P}_map_dely:")),
            (tg_addmap, lambda c: c.data.startswith(f"{P}_addmap:")),
            (tg_addmap_manual, lambda c: c.data == f"{P}_addmap_manual"),
            (tg_pick_lot, lambda c: c.data.startswith(f"{P}_picklot:")),
            (tg_pick_product, lambda c: c.data.startswith(f"{P}_pickprod:")),
            (tg_prodlist, lambda c: c.data.startswith(f"{P}_prodlist:")),
            (tg_prodsearch, lambda c: c.data == f"{P}_prodsearch"),
            (tg_prodmanual, lambda c: c.data == f"{P}_prodmanual"),
            (tg_noop, lambda c: c.data == f"{P}_noop"),
            (tg_params, lambda c: c.data == f"{P}_params"),
            (tg_p_cur, lambda c: c.data == f"{P}_p_cur"),
            (tg_p_stars, lambda c: c.data == f"{P}_p_stars"),
            (tg_p_set, lambda c: c.data.startswith(f"{P}_p_set:")),
            (tg_texts, lambda c: c.data.startswith(f"{P}_texts")),
            (tg_text_edit, lambda c: c.data.startswith(f"{P}_text:")),
            (tg_text_reset, lambda c: c.data.startswith(f"{P}_textreset:")),
            (tg_ac_menu, lambda c: c.data == f"{P}_ac"),
            (tg_ac_w_start, lambda c: c.data == f"{P}_ac_w_start"),
            (tg_ac_edit, lambda c: c.data == f"{P}_ac_edit"),
            (tg_ac_edit_field, lambda c: c.data.startswith(f"{P}_ac_e:")),
            (tg_ac_w_dpre, lambda c: c.data.startswith(f"{P}_ac_w_dpre:")),
            (tg_ac_w_dcustom, lambda c: c.data == f"{P}_ac_w_dcustom"),
            (tg_ac_w_price_mode, lambda c: c.data.startswith(f"{P}_ac_w_pmode:")),
            (tg_ac_refresh, lambda c: c.data == f"{P}_ac_refresh"),
            (tg_ac_dbg, lambda c: c.data == f"{P}_ac_dbg"),
            (tg_ac_miss, lambda c: c.data == f"{P}_ac_miss"),
            (tg_ac_preview, lambda c: c.data.startswith(f"{P}_ac_prev:")),
            (tg_ac_confirm, lambda c: c.data == f"{P}_ac_confirm"),
            (tg_ac_go, lambda c: c.data == f"{P}_ac_go"),
            (tg_ac_reprice, lambda c: c.data == f"{P}_ac_reprice"),
            (tg_ac_delall, lambda c: c.data == f"{P}_ac_delall"),
            (tg_ac_del_lots, lambda c: c.data == f"{P}_ac_del_lots"),
            (tg_ac_del_lots_yes, lambda c: c.data == f"{P}_ac_del_lots_yes"),
            (tg_ac_del_links, lambda c: c.data == f"{P}_ac_del_links"),
            (tg_ac_del_links_yes, lambda c: c.data == f"{P}_ac_del_links_yes"),
        ]

        def _safe(handler):
            def wrapper(call):
                try:
                    handler(call)
                except Exception as e:
                    logger.error(f"{LP} [TG] {handler.__name__}: {e}", exc_info=True)
                    try:
                        bot.answer_callback_query(call.id, "Ошибка, см. логи", show_alert=True)
                    except Exception:
                        pass
            return wrapper

        for handler, cond in handlers:
            tg.cbq_handler(_safe(handler), cond)

        def _msg_filter(m):
            sd = tg.get_state(m.chat.id, m.from_user.id)
            if not sd: 
                return False
            st = sd.get("state", "") if isinstance(sd, dict) else ""
            return st.startswith(P)

        tg.msg_handler(tg_msg_dispatch, func=_msg_filter)

    on_new_order.plugin_uuid = UUID
    on_new_message.plugin_uuid = UUID
    on_last_chat_message_changed.plugin_uuid = UUID

    if on_new_order not in cardinal.new_order_handlers:
        cardinal.new_order_handlers.append(on_new_order)
    if on_new_message not in cardinal.new_message_handlers:
        cardinal.new_message_handlers.append(on_new_message)
    if on_last_chat_message_changed not in cardinal.last_chat_message_changed_handlers:
        cardinal.last_chat_message_changed_handlers.append(on_last_chat_message_changed)

    logger.info(f"{LP} Initialized v{VERSION}, mappings={len(MAPPINGS)}")


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = _stop_poller
