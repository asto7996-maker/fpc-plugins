from __future__ import annotations

# === ОБЯЗАТЕЛЬНЫЕ ПОЛЯ FunPay Cardinal (НЕ УДАЛЯТЬ) ===
NAME = "VexBoost AutoSMM"
VERSION = "2.0.2"
DESCRIPTION = "Автонакрутка через VexBoost (vexboost.ru)"
CREDITS = "Cursor AI"
UUID = "a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e"
SETTINGS_PAGE = False
BIND_TO_DELETE = None
# === КОНЕЦ ОБЯЗАТЕЛЬНЫХ ПОЛЕЙ ===

import json
import logging
import os
import re
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

import requests
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from FunPayAPI.types import MessageTypes
from FunPayAPI.updater.events import LastChatMessageChangedEvent, NewMessageEvent, NewOrderEvent

if TYPE_CHECKING:
    from cardinal import Cardinal

# ─────────────────────────────────────────────────────────────────────────────
# Логирование
# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger("FPC.VexBoost")
LOGGER_PREFIX = "VexBoost"

# ─────────────────────────────────────────────────────────────────────────────
# Пути хранения данных
# ─────────────────────────────────────────────────────────────────────────────

STORAGE_DIR = f"storage/plugins/{UUID}"
SETTINGS_FILE = f"{STORAGE_DIR}/settings.json"
PAY_ORDERS_FILE = f"{STORAGE_DIR}/payorders.json"
ACTIVE_ORDERS_FILE = f"{STORAGE_DIR}/active_orders.json"
HISTORY_FILE = f"{STORAGE_DIR}/history.json"
STATS_FILE = f"{STORAGE_DIR}/stats.json"
CASHLIST_FILE = f"{STORAGE_DIR}/cashlist.json"

# ─────────────────────────────────────────────────────────────────────────────
# Настройки по умолчанию
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS: Dict[str, Any] = {
    "api_url": "https://vexboost.ru/api/v2",
    "api_key": "",
    "auto_refund_on_error": True,
    "auto_refund_on_cancel": True,
    "allow_private_telegram": False,
    "status_check_interval": 60,
    "api_retry_count": 3,
    "api_retry_delay": 2,
    "set_alert_neworder": True,
    "set_alert_errororder": True,
    "set_alert_complete": True,
    "set_alert_smmbalance": True,
    "set_alert_smmbalance_new": False,
    "set_start_mess": True,
    "set_recreated_order": False,
    "set_tg_private": False,
    "commission_percent": 6.0,
    "welcome_message": (
        "👋 Спасибо за заказ!\n"
        "Отправьте ссылку на аккаунт или пост для накрутки.\n"
        "Пример: https://t.me/your_channel"
    ),
    "completion_message": (
        "✅ Заказ #{order_id} выполнен!\n\n"
        "Пожалуйста, перейдите по ссылке и нажмите «Подтвердить выполнение заказа»:\n"
        "🔗 https://funpay.com/orders/{order_id}/\n\n"
        "Спасибо за покупку! 🙏"
    ),
}

# ─────────────────────────────────────────────────────────────────────────────
# Глобальные переменные состояния
# ─────────────────────────────────────────────────────────────────────────────

pending_confirmations: Dict[int, Dict[str, Any]] = {}
pending_by_buyer: Dict[str, Dict[str, Any]] = {}
_file_lock = threading.RLock()
_status_thread_started = False

URL_PATTERN = re.compile(
    r"https?://(?:[a-zA-Z0-9]|[$-_@.&+]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
)
SERVICE_ID_PATTERN = re.compile(r"ID:\s*(\d+)", re.IGNORECASE)
QUANTITY_MULT_PATTERN = re.compile(r"#Quan:\s*(\d+)", re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

FUNPAY_ORDER_URL = "https://funpay.com/orders/{order_id}/"
CONFIRM_MESSAGES = {"+", "-", "➕", "➖", "✅", "❌", "yes", "да", "ok"}


def _normalize_chat_id(chat_id: Any) -> int:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return chat_id  # type: ignore[return-value]


def _strip_html(text: str) -> str:
    return HTML_TAG_PATTERN.sub("", text).replace("&nbsp;", " ").strip()


def send_fp(c: "Cardinal", chat_id: Any, text: str) -> None:
    """Отправка сообщения покупателю в FunPay (без HTML-разметки)."""
    if not chat_id:
        logger.warning("%s: попытка отправить сообщение без chat_id", LOGGER_PREFIX)
        return
    c.send_message(_normalize_chat_id(chat_id), _strip_html(text))


def _get_message_text(msg: Any) -> str:
    raw = msg.text if getattr(msg, "text", None) else str(msg)
    return (raw or "").strip()


def _is_confirm_message(text: str) -> Optional[str]:
    cleaned = text.strip().strip("\ufeff").lower()
    if cleaned in ("+", "➕", "✅", "yes", "да", "ok", "подтверждаю"):
        return "+"
    if cleaned in ("-", "➖", "❌", "no", "нет", "отмена"):
        return "-"
    return None


def set_pending(order: Dict[str, Any]) -> None:
    chat_id = _normalize_chat_id(order.get("chat_id"))
    order["chat_id"] = chat_id
    pending_confirmations[chat_id] = order
    if order.get("buyer"):
        pending_by_buyer[order["buyer"]] = order


def get_pending(chat_id: Any, buyer: str = "") -> Optional[Dict[str, Any]]:
    cid = _normalize_chat_id(chat_id)
    if cid in pending_confirmations:
        return pending_confirmations[cid]
    if buyer and buyer in pending_by_buyer:
        return pending_by_buyer[buyer]
    return None


def pop_pending(chat_id: Any, buyer: str = "") -> Optional[Dict[str, Any]]:
    order = get_pending(chat_id, buyer)
    if not order:
        return None
    cid = _normalize_chat_id(order.get("chat_id"))
    pending_confirmations.pop(cid, None)
    pending_by_buyer.pop(order.get("buyer", ""), None)
    return order


def clear_pending(chat_id: Any, buyer: str = "") -> None:
    pop_pending(chat_id, buyer)

# ─────────────────────────────────────────────────────────────────────────────
# Утилиты хранения (потокобезопасные)
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
            logger.error("%s: ошибка чтения %s — %s", LOGGER_PREFIX, path, exc)
            backup = f"{path}.bak"
            if os.path.exists(path):
                try:
                    os.rename(path, backup)
                    logger.warning("%s: повреждённый файл сохранён как %s", LOGGER_PREFIX, backup)
                except OSError:
                    pass
            return default


def _save_json(path: str, data: Any) -> None:
    with _file_lock:
        _ensure_storage()
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("%s: ошибка записи %s — %s", LOGGER_PREFIX, path, exc)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def load_settings() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_FILE):
        settings = DEFAULT_SETTINGS.copy()
        _save_json(SETTINGS_FILE, settings)
        return settings
    data = _load_json(SETTINGS_FILE, {})
    merged = DEFAULT_SETTINGS.copy()
    merged.update(data)
    return merged


def save_settings(settings: Dict[str, Any]) -> None:
    _save_json(SETTINGS_FILE, settings)


def get_api_url() -> str:
    return load_settings().get("api_url", DEFAULT_SETTINGS["api_url"]).rstrip("/")


def get_api_key() -> str:
    return load_settings().get("api_key", "")


def load_payorders() -> List[Dict[str, Any]]:
    return _load_json(PAY_ORDERS_FILE, [])


def save_payorders(orders: List[Dict[str, Any]]) -> None:
    _save_json(PAY_ORDERS_FILE, orders)


def load_active_orders() -> Dict[str, Any]:
    return _load_json(ACTIVE_ORDERS_FILE, {})


def save_active_orders(orders: Dict[str, Any]) -> None:
    _save_json(ACTIVE_ORDERS_FILE, orders)


def load_history() -> List[Dict[str, Any]]:
    return _load_json(HISTORY_FILE, [])


def save_history(history: List[Dict[str, Any]]) -> None:
    if len(history) > 5000:
        history = history[-5000:]
    _save_json(HISTORY_FILE, history)


def load_cashlist() -> Dict[str, Any]:
    return _load_json(CASHLIST_FILE, {})


def save_cashlist(data: Dict[str, Any]) -> None:
    _save_json(CASHLIST_FILE, data)


def _default_stats() -> Dict[str, Any]:
    return {
        "total": {
            "created": 0,
            "completed": 0,
            "canceled": 0,
            "failed": 0,
            "refunded": 0,
            "revenue": 0.0,
            "cost": 0.0,
            "profit": 0.0,
        },
        "daily": {},
        "by_service": {},
    }


def load_stats() -> Dict[str, Any]:
    stats = _load_json(STATS_FILE, _default_stats())
    for key in ("total", "daily", "by_service"):
        if key not in stats:
            stats[key] = _default_stats()[key]
    return stats


def save_stats(stats: Dict[str, Any]) -> None:
    _save_json(STATS_FILE, stats)


def extract_links(text: str) -> List[str]:
    return URL_PATTERN.findall(text)


def find_order_by_buyer(orders: List[Dict[str, Any]], buyer: str) -> Optional[Dict[str, Any]]:
    for order in orders:
        if order.get("buyer") == buyer:
            return order
    return None


def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_money(amount: float, currency: str = "₽") -> str:
    return f"{amount:.2f} {currency}"


def get_funpay_order_url(order_id: Union[str, int]) -> str:
    return FUNPAY_ORDER_URL.format(order_id=order_id)


# ─────────────────────────────────────────────────────────────────────────────
# Модуль статистики и прибыли
# ─────────────────────────────────────────────────────────────────────────────

class StatisticsManager:
    """Управление статистикой заказов и расчётом прибыли."""

    @staticmethod
    def _ensure_daily(stats: Dict[str, Any], day: str) -> Dict[str, Any]:
        if day not in stats["daily"]:
            stats["daily"][day] = {
                "created": 0, "completed": 0, "canceled": 0,
                "failed": 0, "refunded": 0,
                "revenue": 0.0, "cost": 0.0, "profit": 0.0,
            }
        return stats["daily"][day]

    @staticmethod
    def _ensure_service(stats: Dict[str, Any], service_id: int) -> Dict[str, Any]:
        key = str(service_id)
        if key not in stats["by_service"]:
            stats["by_service"][key] = {
                "count": 0, "completed": 0, "revenue": 0.0,
                "cost": 0.0, "profit": 0.0,
            }
        return stats["by_service"][key]

    @classmethod
    def record_created(cls, service_id: int, revenue: float, currency: str = "₽") -> None:
        stats = load_stats()
        day = today_key()
        daily = cls._ensure_daily(stats, day)
        svc = cls._ensure_service(stats, service_id)

        stats["total"]["created"] += 1
        daily["created"] += 1
        svc["count"] += 1

        save_stats(stats)
        logger.debug("%s: статистика +created service=%s revenue=%s", LOGGER_PREFIX, service_id, revenue)

    @classmethod
    def record_completed(
        cls, service_id: int, revenue: float, cost: float,
        currency_fp: str = "₽", currency_smm: str = "RUB",
    ) -> float:
        stats = load_stats()
        day = today_key()
        daily = cls._ensure_daily(stats, day)
        svc = cls._ensure_service(stats, service_id)
        profit = revenue - cost

        for bucket in (stats["total"], daily, svc):
            if bucket is svc:
                bucket["completed"] += 1
                bucket["revenue"] += revenue
                bucket["cost"] += cost
                bucket["profit"] += profit
            else:
                bucket["completed"] += 1
                bucket["revenue"] += revenue
                bucket["cost"] += cost
                bucket["profit"] += profit

        save_stats(stats)
        logger.info(
            "%s: заказ выполнен | service=%s revenue=%.2f cost=%.2f profit=%.2f",
            LOGGER_PREFIX, service_id, revenue, cost, profit,
        )
        return profit

    @classmethod
    def record_canceled(cls, refunded: bool = False) -> None:
        stats = load_stats()
        day = today_key()
        daily = cls._ensure_daily(stats, day)
        stats["total"]["canceled"] += 1
        daily["canceled"] += 1
        if refunded:
            stats["total"]["refunded"] += 1
            daily["refunded"] += 1
        save_stats(stats)

    @classmethod
    def record_failed(cls) -> None:
        stats = load_stats()
        day = today_key()
        daily = cls._ensure_daily(stats, day)
        stats["total"]["failed"] += 1
        daily["failed"] += 1
        save_stats(stats)

    @classmethod
    def get_period_stats(cls, days: int = 0) -> Dict[str, Any]:
        """days=0 — всё время, days=1 — сегодня, days=7 — неделя, days=30 — месяц."""
        stats = load_stats()
        if days == 0:
            return dict(stats["total"])

        result = {
            "created": 0, "completed": 0, "canceled": 0,
            "failed": 0, "refunded": 0,
            "revenue": 0.0, "cost": 0.0, "profit": 0.0,
        }
        cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        for day, data in stats["daily"].items():
            if day >= cutoff:
                for key in result:
                    result[key] += data.get(key, 0)
        return result

    @classmethod
    def format_stats_text(cls, days: int = 0) -> str:
        period_names = {0: "Всё время", 1: "Сегодня", 7: "7 дней", 30: "30 дней"}
        name = period_names.get(days, f"{days} дней")
        s = cls.get_period_stats(days)
        settings = load_settings()
        commission = settings.get("commission_percent", 6.0)
        profit_after_commission = s["profit"] * (1 - commission / 100)

        return (
            f"📊 <b>Статистика VexBoost — {name}</b>\n\n"
            f"📦 Создано заказов: <b>{s['created']}</b>\n"
            f"✅ Выполнено: <b>{s['completed']}</b>\n"
            f"❌ Отменено: <b>{s['canceled']}</b>\n"
            f"⚠️ Ошибок: <b>{s['failed']}</b>\n"
            f"💸 Возвратов: <b>{s['refunded']}</b>\n\n"
            f"💵 Выручка: <b>{s['revenue']:.2f} ₽</b>\n"
            f"💳 Расход (VexBoost): <b>{s['cost']:.2f}</b>\n"
            f"💰 Прибыль: <b>{s['profit']:.2f} ₽</b>\n"
            f"💰 С комиссией {commission}%: <b>{profit_after_commission:.2f} ₽</b>"
        )

    @classmethod
    def get_top_services(cls, limit: int = 5) -> str:
        stats = load_stats()
        services = stats.get("by_service", {})
        if not services:
            return "📋 Нет данных по услугам."
        sorted_svc = sorted(
            services.items(),
            key=lambda x: x[1].get("profit", 0),
            reverse=True,
        )[:limit]
        lines = ["🏆 <b>Топ услуг по прибыли:</b>\n"]
        for idx, (sid, data) in enumerate(sorted_svc, 1):
            lines.append(
                f"{idx}. ID <code>{sid}</code> — "
                f"✅ {data.get('completed', 0)} шт. | "
                f"💰 {data.get('profit', 0):.2f} ₽"
            )
        return "\n".join(lines)


class ProfitCalculator:
    """Конвертация валют и расчёт прибыли."""

    _rate_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
    _cache_ttl = 300

    @classmethod
    def get_exchange_rate(cls, from_cur: str = "USD", to_cur: str = "RUB") -> Optional[float]:
        cache_key = (from_cur, to_cur)
        cached = cls._rate_cache.get(cache_key)
        if cached and time.time() - cached[1] < cls._cache_ttl:
            return cached[0]
        try:
            url = f"https://api.coingate.com/v2/rates/merchant/{from_cur}/{to_cur}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            rate = float(resp.text)
            cls._rate_cache[cache_key] = (rate, time.time())
            return rate
        except Exception as exc:
            logger.warning("%s: курс %s→%s недоступен: %s", LOGGER_PREFIX, from_cur, to_cur, exc)
            return None

    @classmethod
    def convert_cost(
        cls, cost: float, smm_currency: str,
        fp_currency: str,
    ) -> float:
        if smm_currency == fp_currency:
            return cost
        if fp_currency in ("₽", "RUB") and smm_currency == "USD":
            rate = cls.get_exchange_rate("USD", "RUB")
            return cost * rate if rate else cost
        if fp_currency in ("$", "USD") and smm_currency == "RUB":
            rate = cls.get_exchange_rate("RUB", "USD")
            return cost * rate if rate else cost
        return cost

    @classmethod
    def calculate_profit(
        cls, revenue: float, cost: float,
        fp_currency: str, smm_currency: str,
    ) -> Dict[str, float]:
        converted_cost = cls.convert_cost(cost, smm_currency, fp_currency)
        profit = revenue - converted_cost
        settings = load_settings()
        commission = settings.get("commission_percent", 6.0)
        return {
            "revenue": revenue,
            "cost": converted_cost,
            "profit": profit,
            "profit_after_commission": profit * (1 - commission / 100),
            "commission_percent": commission,
        }


class OrderHistory:
    """Архив завершённых и отменённых заказов."""

    @staticmethod
    def add_entry(entry: Dict[str, Any]) -> None:
        history = load_history()
        entry["archived_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        history.append(entry)
        save_history(history)

    @staticmethod
    def get_recent(limit: int = 10) -> List[Dict[str, Any]]:
        history = load_history()
        return list(reversed(history[-limit:]))

    @staticmethod
    def format_recent_text(limit: int = 10) -> str:
        recent = OrderHistory.get_recent(limit)
        if not recent:
            return "📋 История заказов пуста."
        lines = [f"📋 <b>Последние {len(recent)} заказов:</b>\n"]
        for item in recent:
            status_icon = {"Completed": "✅", "Canceled": "❌", "Failed": "⚠️"}.get(
                item.get("status", ""), "📦"
            )
            lines.append(
                f"{status_icon} FP <code>#{item.get('funpay_id', '?')}</code> | "
                f"VB <code>{item.get('vexboost_id', '?')}</code> | "
                f"💰 {item.get('profit', 0):.2f} ₽"
            )
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# VexBoost API (с повторными попытками)
# ─────────────────────────────────────────────────────────────────────────────

class VexBoostAPI:
    """Клиент API VexBoost (стандарт SMM API v2)."""

    @staticmethod
    def _get_retry_settings() -> Tuple[int, int]:
        s = load_settings()
        return (
            int(s.get("api_retry_count", 3)),
            int(s.get("api_retry_delay", 2)),
        )

    @classmethod
    def _request(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        from urllib.parse import urlencode

        api_url = get_api_url()
        api_key = get_api_key()
        if not api_key:
            return {"error": "API ключ не задан. Используйте /vexboost"}
        payload = {"key": api_key, **params}
        retries, delay = cls._get_retry_settings()
        query = urlencode(payload)
        get_url = f"{api_url}?{query}"

        for attempt in range(1, retries + 1):
            # Сначала GET (как AutoSmm) — VexBoost так работает стабильнее
            for request_fn, label in (
                (lambda: requests.get(get_url, timeout=45), "GET"),
                (lambda: requests.get(api_url, params=payload, timeout=45), "GET-params"),
                (lambda: requests.post(api_url, data=payload, timeout=45), "POST"),
            ):
                try:
                    response = request_fn()
                    response.raise_for_status()
                    data = response.json()
                    if isinstance(data, dict):
                        return data
                    if isinstance(data, list):
                        return {"services": data}
                    return {"error": "Некорректный ответ API"}
                except requests.Timeout:
                    logger.warning(
                        "%s: таймаут %s (попытка %d/%d)",
                        LOGGER_PREFIX, label, attempt, retries,
                    )
                except requests.RequestException as exc:
                    logger.warning(
                        "%s: ошибка %s (попытка %d/%d): %s",
                        LOGGER_PREFIX, label, attempt, retries, exc,
                    )
                except ValueError:
                    logger.warning("%s: не-JSON ответ от %s", LOGGER_PREFIX, label)
            if attempt < retries:
                time.sleep(delay * attempt)
        return {"error": "Не удалось связаться с VexBoost. Проверьте API KEY и баланс на vexboost.ru"}

    @classmethod
    def get_balance(cls) -> Optional[Tuple[float, str]]:
        data = cls._request({"action": "balance"})
        if "balance" not in data:
            return None
        match = re.search(r"[\d.]+", str(data["balance"]))
        if not match:
            return None
        return float(match.group()), data.get("currency", "RUB")

    @classmethod
    def get_services(cls) -> Optional[List[Dict[str, Any]]]:
        data = cls._request({"action": "services"})
        if isinstance(data, list):
            return data
        return None

    @classmethod
    def create_order(cls, service_id: int, link: str, quantity: int) -> Any:
        data = cls._request({
            "action": "add",
            "service": service_id,
            "link": link,
            "quantity": quantity,
        })
        if "order" in data:
            return data["order"]
        return data.get("error", "Неизвестная ошибка")

    @classmethod
    def get_order_status(cls, order_id: int) -> Optional[Dict[str, Any]]:
        data = cls._request({"action": "status", "order": order_id})
        if "error" in data:
            logger.debug("%s: статус #%s — %s", LOGGER_PREFIX, order_id, data["error"])
            return None
        return data

    @classmethod
    def refill_order(cls, order_id: int) -> Optional[Any]:
        data = cls._request({"action": "refill", "order": order_id})
        return data.get("refill")

    @classmethod
    def cancel_order(cls, order_id: int) -> Optional[Any]:
        data = cls._request({"action": "cancel", "order": order_id})
        return data.get("cancel")


# ─────────────────────────────────────────────────────────────────────────────
# Telegram-уведомления для администратора
# ─────────────────────────────────────────────────────────────────────────────

def _get_authorized_users(c: "Cardinal") -> List[int]:
    try:
        from tg_bot.utils import load_authorized_users
        return load_authorized_users() or []
    except Exception:
        return []


def _send_tg_to_admins(c: "Cardinal", text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
    if not c.telegram:
        return
    users = _get_authorized_users(c)
    if not users:
        return
    for user_id in users:
        try:
            c.telegram.bot.send_message(
                user_id, text, parse_mode="HTML",
                reply_markup=keyboard, disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.debug("%s: не удалось отправить TG user %s: %s", LOGGER_PREFIX, user_id, exc)


def send_order_created_notification(
    c: "Cardinal", order: Dict[str, Any],
    vexboost_id: int, cost: float, smm_currency: str,
) -> None:
    settings = load_settings()
    if not settings.get("set_alert_neworder"):
        return
    profit_data = ProfitCalculator.calculate_profit(
        safe_float(order.get("OrderPrice")),
        cost, str(order.get("OrderCurrency", "₽")), smm_currency,
    )
    balance = VexBoostAPI.get_balance()
    balance_text = f"{balance[0]:.2f} {balance[1]}" if balance else "н/д"

    try:
        fp_balance = c.get_balance()
        fp_bal_text = f"{fp_balance.total_rub}₽, {fp_balance.available_usd}$, {fp_balance.total_eur}€"
    except Exception:
        fp_bal_text = "н/д"

    btn = InlineKeyboardButton(
        "🌐 Открыть заказ FunPay",
        url=get_funpay_order_url(order["OrderID"]),
    )
    kb = InlineKeyboardMarkup().add(btn)

    text = (
        f"✅ <b>Новый заказ {NAME}</b>\n\n"
        f"🛒 Лот: <code>{order.get('Order', '')[:80]}</code>\n"
        f"🙍 Покупатель: <b>{order.get('buyer', '')}</b>\n\n"
        f"💵 Сумма FunPay: <b>{profit_data['revenue']:.2f}</b> {order.get('OrderCurrency', '₽')}\n"
        f"💳 Расход VexBoost: <b>{profit_data['cost']:.2f}</b>\n"
        f"💰 Прибыль: <b>{profit_data['profit']:.2f}</b>\n"
        f"💰 С комиссией: <b>{profit_data['profit_after_commission']:.2f}</b>\n\n"
        f"💰 Баланс VexBoost: {balance_text}\n"
        f"💰 Баланс FunPay: {fp_bal_text}\n\n"
        f"📇 FunPay: <code>#{order['OrderID']}</code>\n"
        f"🆔 VexBoost: <code>{vexboost_id}</code>\n"
        f"🔍 Service ID: <code>{order.get('service_id')}</code>\n"
        f"🔢 Кол-во: <b>{order.get('Amount')}</b>\n"
        f"🔗 {order.get('url', '').replace('https://', '')}"
    )
    _send_tg_to_admins(c, text, kb)


def send_order_error_notification(c: "Cardinal", error: str, order: Dict[str, Any]) -> None:
    settings = load_settings()
    if not settings.get("set_alert_errororder"):
        return
    btn = InlineKeyboardButton("🌐 Заказ FunPay", url=get_funpay_order_url(order["OrderID"]))
    kb = InlineKeyboardMarkup().add(btn)
    text = (
        f"❌ <b>Ошибка {NAME}</b>\n\n"
        f"📇 FunPay: <code>#{order['OrderID']}</code>\n"
        f"🙍 Покупатель: {order.get('buyer')}\n"
        f"⚠️ Ошибка: <code>{error}</code>"
    )
    _send_tg_to_admins(c, text, kb)
    if settings.get("set_alert_smmbalance"):
        send_balance_notification(c)


def send_order_complete_notification(
    c: "Cardinal", order: Dict[str, Any], profit: float,
) -> None:
    settings = load_settings()
    if not settings.get("set_alert_complete"):
        return
    btn = InlineKeyboardButton("🌐 Заказ FunPay", url=get_funpay_order_url(order.get("order_id", "")))
    kb = InlineKeyboardMarkup().add(btn)
    text = (
        f"🎉 <b>Заказ выполнен {NAME}</b>\n\n"
        f"📇 FunPay: <code>#{order.get('order_id')}</code>\n"
        f"🆔 VexBoost: <code>{order.get('vexboost_id', '')}</code>\n"
        f"💰 Прибыль: <b>{profit:.2f} ₽</b>"
    )
    _send_tg_to_admins(c, text, kb)


def send_balance_notification(c: "Cardinal") -> None:
    balance = VexBoostAPI.get_balance()
    if not balance:
        return
    try:
        fp_balance = c.get_balance()
        fp_text = f"{fp_balance.total_rub}₽, {fp_balance.available_usd}$, {fp_balance.total_eur}€"
    except Exception:
        fp_text = "н/д"
    text = (
        f"💰 <b>Баланс VexBoost:</b> {balance[0]:.2f} {balance[1]}\n"
        f"💰 <b>Баланс FunPay:</b> {fp_text}"
    )
    _send_tg_to_admins(c, text)


def send_start_notification(c: "Cardinal") -> None:
    settings = load_settings()
    if not settings.get("set_start_mess"):
        return
    text = (
        f"✅ <b>{NAME} v{VERSION} запущен</b>\n\n"
        f"⚙️ Настройки: /vexboost\n"
        f"📊 Статистика: /vb_stats\n"
        f"💰 Баланс: /vb_balance"
    )
    _send_tg_to_admins(c, text)


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции заказов
# ─────────────────────────────────────────────────────────────────────────────

def _refund_order(c: "Cardinal", order_id: str) -> bool:
    if not order_id:
        return False
    try:
        c.account.refund(order_id)
        logger.info("%s: возврат FunPay #%s", LOGGER_PREFIX, order_id)
        StatisticsManager.record_canceled(refunded=True)
        return True
    except Exception as exc:
        logger.error("%s: ошибка возврата FunPay #%s: %s", LOGGER_PREFIX, order_id, exc)
        return False


def _remove_pay_order(buyer: str) -> None:
    orders = load_payorders()
    orders = [o for o in orders if o.get("buyer") != buyer]
    save_payorders(orders)


def _update_pay_order(order: Dict[str, Any]) -> None:
    orders = load_payorders()
    for idx, existing in enumerate(orders):
        if existing.get("OrderID") == order.get("OrderID"):
            orders[idx] = order
            break
    else:
        orders.append(order)
    save_payorders(orders)


def _build_completion_message(funpay_order_id: str) -> str:
    settings = load_settings()
    template = settings.get("completion_message", DEFAULT_SETTINGS["completion_message"])
    return template.format(order_id=funpay_order_id)


def _is_private_telegram_link(link: str) -> bool:
    return "t.me" in link and ("/c/" in link or "+" in link)


# ─────────────────────────────────────────────────────────────────────────────
# Обработка нового заказа FunPay
# ─────────────────────────────────────────────────────────────────────────────

def bind_to_new_order(c: "Cardinal", e: NewOrderEvent) -> None:
    try:
        if not get_api_key():
            logger.warning("%s: API ключ не задан", LOGGER_PREFIX)
            return

        order_id = e.order.id
        full_order = c.account.get_order(order_id)
        description = full_order.full_description or ""
        buyer = full_order.buyer_username

        match_id = SERVICE_ID_PATTERN.search(description)
        if not match_id:
            return

        service_id = int(match_id.group(1))
        multiplier = 1
        match_quan = QUANTITY_MULT_PATTERN.search(description)
        if match_quan:
            multiplier = max(1, int(match_quan.group(1)))

        amount = int(e.order.amount) * multiplier
        chat = c.account.get_chat_by_name(buyer)
        chat_id = chat.id if chat else e.order.chat_id

        order_entry: Dict[str, Any] = {
            "OrderID": str(order_id),
            "Amount": amount,
            "OrderPrice": e.order.price,
            "OrderCurrency": str(e.order.currency),
            "Order": str(e.order),
            "service_id": service_id,
            "buyer": buyer,
            "url": "",
            "chat_id": chat_id,
            "OrderDateTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        pay_orders = load_payorders()
        pay_orders.append(order_entry)
        save_payorders(pay_orders)

        StatisticsManager.record_created(service_id, safe_float(e.order.price))

        settings = load_settings()
        if settings.get("set_alert_smmbalance_new"):
            send_balance_notification(c)

        if chat_id:
            welcome = settings.get("welcome_message", DEFAULT_SETTINGS["welcome_message"])
            send_fp(c, chat_id, welcome)

        logger.info(
            "%s: новый заказ FP#%s service=%s qty=%s buyer=%s",
            LOGGER_PREFIX, order_id, service_id, amount, buyer,
        )
    except Exception as exc:
        logger.error("%s: ошибка bind_to_new_order: %s", LOGGER_PREFIX, exc)
        logger.debug(traceback.format_exc())


# ─────────────────────────────────────────────────────────────────────────────
# Запрос подтверждения и создание заказа VexBoost
# ─────────────────────────────────────────────────────────────────────────────

def request_confirmation(c: "Cardinal", order: Dict[str, Any], link: str) -> None:
    settings = load_settings()
    allow_private = settings.get("set_tg_private") or settings.get("allow_private_telegram")
    if not allow_private and _is_private_telegram_link(link):
        send_fp(
            c, order["chat_id"],
            "❌ Закрытые Telegram-каналы/группы не поддерживаются.\n"
            "Используйте публичную ссылку: https://t.me/your_channel",
        )
        return

    order["url"] = link
    display_link = link.replace("https://", "").replace("http://", "")
    send_fp(
        c, order["chat_id"],
        f"📋 Проверьте детали заказа:\n\n"
        f"🛒 Лот: {order['Order']}\n"
        f"🔢 Количество: {order['Amount']} шт.\n"
        f"🔗 Ссылка: {display_link}\n\n"
        f"✅ Отправьте + для подтверждения\n"
        f"❌ Отправьте - для отмены и возврата\n"
        f"🔄 Или отправьте новую ссылку",
    )
    set_pending(order)
    _update_pay_order(order)
    logger.info(
        "%s: ожидание подтверждения FP#%s chat=%s buyer=%s",
        LOGGER_PREFIX, order.get("OrderID"), order.get("chat_id"), order.get("buyer"),
    )


def confirm_order(c: "Cardinal", chat_id: Any, text: str, buyer: str = "") -> None:
    order = pop_pending(chat_id, buyer)
    if not order:
        logger.warning(
            "%s: подтверждение без заказа chat=%s buyer=%s text=%r",
            LOGGER_PREFIX, chat_id, buyer, text,
        )
        return

    action = _is_confirm_message(text) or text.strip()
    if action == "+":
        send_fp(c, order["chat_id"], "⏳ Создаю заказ, подождите...")
        _create_vexboost_order(c, order)
    elif action == "-":
        send_fp(c, chat_id, "❌ Заказ отменён. Средства будут возвращены.")
        _remove_pay_order(order["buyer"])
        _refund_order(c, order["OrderID"])


def _create_vexboost_order(c: "Cardinal", order: Dict[str, Any]) -> None:
    settings = load_settings()
    result = VexBoostAPI.create_order(
        order["service_id"], order["url"], order["Amount"],
    )

    if isinstance(result, int) or (isinstance(result, str) and str(result).isdigit()):
        smm_id = int(result)
        active = load_active_orders()
        active[str(smm_id)] = {
            "service_id": order["service_id"],
            "chat_id": order["chat_id"],
            "order_id": order["OrderID"],
            "order_url": order["url"],
            "order_amount": order["Amount"],
            "order_price": order["OrderPrice"],
            "order_currency": order.get("OrderCurrency", "₽"),
            "buyer": order.get("buyer", ""),
            "partial_amount": 0,
            "orderdatetime": order.get("OrderDateTime", ""),
            "status": "Pending",
        }
        save_active_orders(active)
        _remove_pay_order(order["buyer"])

        status_data = VexBoostAPI.get_order_status(smm_id)
        cost = safe_float(status_data.get("charge", 0)) if status_data else 0.0
        smm_cur = status_data.get("currency", "RUB") if status_data else "RUB"

        send_order_created_notification(c, order, smm_id, cost, smm_cur)

        send_fp(
            c, order["chat_id"],
            f"📊 Заказ создан и отправлен в VexBoost!\n"
            f"🆔 ID заказа: {smm_id}\n\n"
            f"📋 Команды:\n"
            f"⠀∟ #статус {smm_id}\n"
            f"⠀∟ #рефилл {smm_id}\n\n"
            f"⌛ Время выполнения: от нескольких минут до 48 часов.",
        )
        logger.info("%s: VB#%s создан для FP#%s", LOGGER_PREFIX, smm_id, order["OrderID"])
    else:
        error_text = str(result)
        send_fp(c, order["chat_id"], f"❌ Ошибка при создании заказа:\n{error_text}")
        StatisticsManager.record_failed()
        send_order_error_notification(c, error_text, order)
        OrderHistory.add_entry({
            "funpay_id": order["OrderID"],
            "status": "Failed",
            "error": error_text,
            "buyer": order.get("buyer"),
            "service_id": order.get("service_id"),
        })
        if settings.get("auto_refund_on_error"):
            _refund_order(c, order["OrderID"])



# ─────────────────────────────────────────────────────────────────────────────
# Обработчик сообщений FunPay
# ─────────────────────────────────────────────────────────────────────────────

def _process_buyer_message(
    c: "Cardinal",
    message_text: str,
    chat_id: Any,
    chat_name: str,
    author_id: Any,
    msg_type: Any,
) -> None:
    msgname = chat_name
    cid = _normalize_chat_id(chat_id)

    if "вернул деньги покупателю" in message_text.lower():
        _remove_pay_order(msgname)
        clear_pending(cid, msgname)
        return

    if msg_type != MessageTypes.NON_SYSTEM:
        return

    if author_id == c.account.id:
        return

    confirm_action = _is_confirm_message(message_text)
    pending = get_pending(cid, msgname)

    if confirm_action:
        logger.info(
            "%s: получено подтверждение %r от %s chat=%s pending=%s",
            LOGGER_PREFIX, message_text, msgname, cid, pending is not None,
        )
        if pending:
            confirm_order(c, cid, confirm_action, msgname)
            return
        pay_orders = load_payorders()
        order = find_order_by_buyer(pay_orders, msgname)
        if order and order.get("url"):
            order["chat_id"] = cid
            set_pending(order)
            confirm_order(c, cid, confirm_action, msgname)
            return
        send_fp(c, cid, "⚪️ Сначала отправьте ссылку для накрутки.")
        return

    if pending:
        _handle_pending_message(c, cid, message_text, msgname)
        return

    if message_text.startswith("#статус"):
        _cmd_status(c, cid, message_text)
        return

    if message_text.startswith("#рефилл"):
        _cmd_refill(c, cid, message_text)
        return

    pay_orders = load_payorders()
    order = find_order_by_buyer(pay_orders, msgname)
    if order:
        links = extract_links(message_text)
        if links:
            order["chat_id"] = cid
            request_confirmation(c, order, links[0])


def msg_hook(c: "Cardinal", e: NewMessageEvent) -> None:
    try:
        msg = e.message
        _process_buyer_message(
            c,
            _get_message_text(msg),
            msg.chat_id,
            msg.chat_name,
            msg.author_id,
            msg.type,
        )
    except Exception as exc:
        logger.error("%s: ошибка msg_hook: %s", LOGGER_PREFIX, exc)
        logger.debug(traceback.format_exc())


def last_chat_msg_hook(c: "Cardinal", e: Any) -> None:
    """Обработчик для old_mode_enabled (LAST_CHAT_MESSAGE_CHANGED)."""
    try:
        if not getattr(c, "old_mode_enabled", False):
            return
        chat = e.chat
        if not chat.unread:
            return
        message_text = str(chat).strip()
        _process_buyer_message(
            c,
            message_text,
            chat.id,
            chat.name,
            None,
            MessageTypes.NON_SYSTEM,
        )
    except Exception as exc:
        logger.error("%s: ошибка last_chat_msg_hook: %s", LOGGER_PREFIX, exc)
        logger.debug(traceback.format_exc())


def _handle_pending_message(
    c: "Cardinal", chat_id: Any, message_text: str, buyer: str = "",
) -> None:
    if "http" in message_text:
        order = get_pending(chat_id, buyer)
        if order:
            order["chat_id"] = _normalize_chat_id(chat_id)
            links = extract_links(message_text)
            if links:
                request_confirmation(c, order, links[0])
        return
    send_fp(
        c, chat_id,
        "⚪️ Отправьте + для подтверждения, - для отмены или новую ссылку.",
    )


def _cmd_status(c: "Cardinal", chat_id: Any, message_text: str) -> None:
    parts = message_text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        send_fp(c, chat_id, "Использование: #статус ID")
        return
    smm_id = int(parts[1])
    status = VexBoostAPI.get_order_status(smm_id)
    if not status:
        send_fp(c, chat_id, "🔴 Не удалось получить статус заказа.")
        return
    start_count = status.get("start_count", 0)
    display_start = "*" if start_count == 0 else str(start_count)
    send_fp(
        c, chat_id,
        f"📈 Статус заказа {smm_id}\n"
        f"⠀∟ 📊 Статус: {status.get('status', '—')}\n"
        f"⠀∟ 🔢 Было: {display_start}\n"
        f"⠀∟ 👀 Остаток: {status.get('remains', '—')}\n"
        f"⠀∟ 💳 Стоимость: {status.get('charge', '—')} {status.get('currency', '')}",
    )


def _cmd_refill(c: "Cardinal", chat_id: Any, message_text: str) -> None:
    parts = message_text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        send_fp(c, chat_id, "Использование: #рефилл ID")
        return
    result = VexBoostAPI.refill_order(int(parts[1]))
    if result is not None:
        send_fp(c, chat_id, "✅ Запрос на рефилл отправлен!")
    else:
        send_fp(
            c, chat_id,
            "🔴 Ошибка рефилла. Возможно, рефилл ещё недоступен для этой услуги.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Фоновая проверка статусов заказов VexBoost
# ─────────────────────────────────────────────────────────────────────────────

def start_status_checker(c: "Cardinal") -> None:
    global _status_thread_started
    if _status_thread_started:
        return
    _status_thread_started = True
    threading.Thread(
        target=_status_checker_loop, args=(c,),
        name="VexBoostStatusChecker", daemon=True,
    ).start()
    logger.info("%s: фоновая проверка статусов запущена", LOGGER_PREFIX)


def _status_checker_loop(c: "Cardinal") -> None:
    while True:
        try:
            _check_all_active_orders(c)
        except Exception as exc:
            logger.error("%s: ошибка в status_checker: %s", LOGGER_PREFIX, exc)
            logger.debug(traceback.format_exc())
        interval = max(30, int(load_settings().get("status_check_interval", 60)))
        time.sleep(interval)


def _check_all_active_orders(c: "Cardinal") -> None:
    if not get_api_key():
        return
    active = load_active_orders()
    if not active:
        return

    settings = load_settings()
    api_url = get_api_url()
    api_key = get_api_key()
    updated: Dict[str, Any] = {}
    to_notify_complete: List[str] = []
    to_notify_cancel: List[str] = []
    to_notify_partial: List[str] = []

    for smm_id, info in active.items():
        status_data = VexBoostAPI.get_order_status(int(smm_id))
        if not status_data:
            updated[smm_id] = info
            continue

        status = status_data.get("status", "Unknown")
        info["status"] = status
        info["partial_amount"] = int(status_data.get("remains", 0))
        updated[smm_id] = info

        if status == "Completed":
            to_notify_complete.append(smm_id)
        elif status == "Canceled":
            to_notify_cancel.append(smm_id)
        elif status == "Partial":
            to_notify_partial.append(smm_id)

    for smm_id in to_notify_complete:
        _handle_completed_order(c, smm_id, updated.pop(smm_id, {}))

    for smm_id in to_notify_cancel:
        _handle_canceled_order(c, smm_id, updated.pop(smm_id, {}))

    for smm_id in to_notify_partial:
        _handle_partial_order(c, smm_id, updated.get(smm_id, {}), api_url, api_key)

    save_active_orders(updated)

    cashlist = load_cashlist()
    for smm_id, info in cashlist.items():
        if smm_id not in updated:
            updated[smm_id] = info
    if cashlist:
        save_active_orders(updated)
        save_cashlist({})


def _handle_completed_order(c: "Cardinal", smm_id: str, info: Dict[str, Any]) -> None:
    funpay_id = info.get("order_id", "")
    chat_id = info.get("chat_id")
    service_id = info.get("service_id", 0)
    revenue = safe_float(info.get("order_price"))
    fp_currency = info.get("order_currency", "₽")

    status_data = VexBoostAPI.get_order_status(int(smm_id))
    cost = safe_float(status_data.get("charge", 0)) if status_data else 0.0
    smm_cur = status_data.get("currency", "RUB") if status_data else "RUB"

    profit = StatisticsManager.record_completed(service_id, revenue, cost)

    OrderHistory.add_entry({
        "funpay_id": funpay_id,
        "vexboost_id": smm_id,
        "status": "Completed",
        "buyer": info.get("buyer"),
        "service_id": service_id,
        "revenue": revenue,
        "cost": cost,
        "profit": profit,
        "url": info.get("order_url", ""),
    })

    if chat_id:
        completion_msg = _build_completion_message(funpay_id)
        send_fp(c, chat_id, completion_msg)

    send_order_complete_notification(c, {
        "order_id": funpay_id,
        "vexboost_id": smm_id,
    }, profit)

    logger.info("%s: VB#%s выполнен (FP#%s) profit=%.2f", LOGGER_PREFIX, smm_id, funpay_id, profit)


def _handle_canceled_order(c: "Cardinal", smm_id: str, info: Dict[str, Any]) -> None:
    settings = load_settings()
    funpay_id = info.get("order_id", "")
    chat_id = info.get("chat_id")

    StatisticsManager.record_canceled(refunded=False)

    OrderHistory.add_entry({
        "funpay_id": funpay_id,
        "vexboost_id": smm_id,
        "status": "Canceled",
        "buyer": info.get("buyer"),
        "service_id": info.get("service_id"),
    })

    if chat_id:
        send_fp(
            c, chat_id,
            f"❌ Заказ #{funpay_id} отменён на стороне VexBoost.\n"
            f"Средства будут возвращены.",
        )

    if settings.get("auto_refund_on_cancel", True):
        _refund_order(c, funpay_id)

    logger.warning("%s: VB#%s отменён (FP#%s)", LOGGER_PREFIX, smm_id, funpay_id)


def _handle_partial_order(
    c: "Cardinal", smm_id: str, info: Dict[str, Any],
    api_url: str, api_key: str,
) -> None:
    settings = load_settings()
    chat_id = info.get("chat_id")
    funpay_id = info.get("order_id", "")
    partial_amount = int(info.get("partial_amount", 0))

    if not settings.get("set_recreated_order"):
        if chat_id:
            send_fp(
                c, chat_id,
                f"⚠️ Заказ #{funpay_id} приостановлен (Partial).\n"
                f"Остаток: {partial_amount} ед.\n"
                f"Обратитесь к продавцу.",
            )
        return

    if partial_amount <= 0:
        return

    new_service_id = info.get("service_id")
    new_link = info.get("order_url", "")
    try:
        new_id = VexBoostAPI.create_order(new_service_id, new_link, partial_amount)
        if new_id and str(new_id).isdigit():
            cashlist = load_cashlist()
            cashlist[str(new_id)] = {
                "service_id": new_service_id,
                "chat_id": chat_id,
                "order_id": funpay_id,
                "order_url": new_link,
                "order_amount": partial_amount,
                "partial_amount": 0,
                "orderdatetime": info.get("orderdatetime", ""),
                "status": "Pending",
            }
            save_cashlist(cashlist)
            if chat_id:
                send_fp(
                    c, chat_id,
                    f"📈 Заказ #{funpay_id} пересоздан!\n"
                    f"🆔 Новый ID: {new_id}\n"
                    f"⏳ Остаток: {partial_amount}",
                )
    except Exception as exc:
        logger.error("%s: ошибка пересоздания partial: %s", LOGGER_PREFIX, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram-панель управления (/vexboost)
# ─────────────────────────────────────────────────────────────────────────────

def _main_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🔗 API URL", callback_data="vb_set_url"),
        InlineKeyboardButton("🔐 API KEY", callback_data="vb_set_key"),
    )
    kb.row(
        InlineKeyboardButton("📊 Статистика", callback_data="vb_stats_menu"),
        InlineKeyboardButton("💰 Баланс", callback_data="vb_balance_btn"),
    )
    kb.row(
        InlineKeyboardButton("📝 Ожидают ссылку", callback_data="vb_pay_orders"),
        InlineKeyboardButton("📋 Активные", callback_data="vb_active_orders"),
    )
    kb.row(
        InlineKeyboardButton("📜 История", callback_data="vb_history"),
        InlineKeyboardButton("🏆 Топ услуг", callback_data="vb_top_services"),
    )
    kb.row(
        InlineKeyboardButton("💎 Прибыль", callback_data="vb_profit"),
        InlineKeyboardButton("📈 График", callback_data="vb_chart"),
    )
    kb.row(
        InlineKeyboardButton("🏥 Диагностика", callback_data="vb_health"),
        InlineKeyboardButton("📊 Детально", callback_data="vb_extended_stats"),
    )
    kb.row(
        InlineKeyboardButton("🛠 Настройки", callback_data="vb_settings_menu"),
        InlineKeyboardButton("ℹ️ Помощь", callback_data="vb_help"),
    )
    return kb


def _stats_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("📅 Сегодня", callback_data="vb_stats_1"),
        InlineKeyboardButton("📆 7 дней", callback_data="vb_stats_7"),
    )
    kb.row(
        InlineKeyboardButton("🗓 30 дней", callback_data="vb_stats_30"),
        InlineKeyboardButton("📊 Всё время", callback_data="vb_stats_0"),
    )
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"))
    return kb


def _settings_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    def toggle_btn(key: str, label_on: str, label_off: str) -> InlineKeyboardButton:
        on = settings.get(key, False)
        return InlineKeyboardButton(
            f"{'🟢' if on else '🔴'} {label_on if on else label_off}",
            callback_data=f"vb_toggle_{key}",
        )

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(toggle_btn("auto_refund_on_error", "Автовозврат при ошибке", "Автовозврат при ошибке"))
    kb.add(toggle_btn("auto_refund_on_cancel", "Автовозврат при отмене", "Автовозврат при отмене"))
    kb.add(toggle_btn("set_alert_neworder", "Увед. о новом заказе", "Увед. о новом заказе"))
    kb.add(toggle_btn("set_alert_errororder", "Увед. при ошибке", "Увед. при ошибке"))
    kb.add(toggle_btn("set_alert_complete", "Увед. о выполнении", "Увед. о выполнении"))
    kb.add(toggle_btn("set_alert_smmbalance", "Увед. о балансе", "Увед. о балансе"))
    kb.add(toggle_btn("set_alert_smmbalance_new", "Баланс до заказа", "Баланс до заказа"))
    kb.add(toggle_btn("set_start_mess", "Сообщение при старте", "Сообщение при старте"))
    kb.add(toggle_btn("set_recreated_order", "Пересоздание Partial", "Пересоздание Partial"))
    kb.add(toggle_btn("set_tg_private", "Закрытые TG каналы", "Закрытые TG каналы"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"))
    return kb


def _settings_summary(settings: Dict[str, Any]) -> str:
    key = get_api_key()
    key_display = ("***" + key[-4:]) if len(key) > 4 else "не задан"
    refund_err = "🟢" if settings.get("auto_refund_on_error") else "🔴"
    refund_cancel = "🟢" if settings.get("auto_refund_on_cancel") else "🔴"
    return (
        f"⚙️ <b>{NAME} v{VERSION}</b>\n\n"
        f"🔗 API: <code>{get_api_url()}</code>\n"
        f"🔐 KEY: <code>{key_display}</code>\n"
        f"🔄 Автовозврат (ошибка): {refund_err}\n"
        f"🔄 Автовозврат (отмена): {refund_cancel}\n"
        f"⏱ Интервал проверки: <b>{settings.get('status_check_interval', 60)}</b> сек.\n"
        f"💼 Комиссия: <b>{settings.get('commission_percent', 6)}%</b>\n\n"
        f"📋 В описании лота:\n"
        f"<code>ID: 1634</code>\n"
        f"<code>#Quan: 10</code> (опционально)"
    )


def _help_text() -> str:
    return (
        f"ℹ️ <b>Справка {NAME}</b>\n\n"
        f"<b>Настройка лотов:</b>\n"
        f"В описании лота укажите ID услуги с vexboost.ru:\n"
        f"<code>ID: 1634</code>\n"
        f"<code>#Quan: 10</code> — множитель количества\n\n"
        f"<b>Процесс заказа:</b>\n"
        f"1. Покупатель оплачивает лот\n"
        f"2. Отправляет ссылку\n"
        f"3. Подтверждает <b>+</b> или отменяет <b>-</b>\n"
        f"4. После выполнения получает ссылку на FunPay\n\n"
        f"<b>Команды покупателя:</b>\n"
        f"<code>#статус ID</code> — статус заказа VexBoost\n"
        f"<code>#рефилл ID</code> — запрос рефилла\n\n"
        f"<b>Команды администратора:</b>\n"
        f"/vexboost — панель управления\n"
        f"/vb_stats — статистика\n"
        f"/vb_balance — баланс VexBoost"
    )


def init_commands(cardinal: "Cardinal", *args) -> None:
    send_start_notification(cardinal)

    if not cardinal.telegram:
        return

    tg = cardinal.telegram
    bot = tg.bot

    def send_main_panel(message):
        settings = load_settings()
        bot.reply_to(
            message, _settings_summary(settings),
            reply_markup=_main_keyboard(), parse_mode="HTML",
        )

    def send_stats_cmd(message):
        text = StatisticsManager.format_stats_text(0)
        bot.reply_to(message, text, parse_mode="HTML", reply_markup=_stats_keyboard())

    def send_balance_cmd(message):
        balance = VexBoostAPI.get_balance()
        if balance:
            text = f"💰 <b>Баланс VexBoost:</b> {balance[0]:.2f} {balance[1]}"
        else:
            text = "🔴 Не удалось получить баланс. Проверьте API KEY."
        try:
            fp = cardinal.get_balance()
            text += f"\n💰 <b>FunPay:</b> {fp.total_rub}₽, {fp.available_usd}$, {fp.total_eur}€"
        except Exception:
            pass
        bot.reply_to(message, text, parse_mode="HTML")

    def handle_callback(call):
        settings = load_settings()
        chat_id = call.message.chat.id
        msg_id = call.message.message_id

        try:
            if call.data == "vb_back_main":
                bot.edit_message_text(
                    _settings_summary(settings), chat_id, msg_id,
                    reply_markup=_main_keyboard(), parse_mode="HTML",
                )

            elif call.data == "vb_set_url":
                result = bot.send_message(
                    chat_id, "Введите API URL:\n(например https://vexboost.ru/api/v2)",
                )
                tg.set_state(chat_id=chat_id, message_id=result.id,
                             user_id=call.from_user.id, state="vb_api_url")
                bot.answer_callback_query(call.id)

            elif call.data == "vb_set_key":
                result = bot.send_message(chat_id, "Введите API KEY из личного кабинета VexBoost:")
                tg.set_state(chat_id=chat_id, message_id=result.id,
                             user_id=call.from_user.id, state="vb_api_key")
                bot.answer_callback_query(call.id)

            elif call.data == "vb_balance_btn":
                balance = VexBoostAPI.get_balance()
                if balance:
                    bot.answer_callback_query(
                        call.id, f"Баланс: {balance[0]:.2f} {balance[1]}", show_alert=True,
                    )
                else:
                    bot.answer_callback_query(call.id, "Ошибка получения баланса", show_alert=True)

            elif call.data == "vb_stats_menu":
                bot.edit_message_text(
                    StatisticsManager.format_stats_text(0), chat_id, msg_id,
                    reply_markup=_stats_keyboard(), parse_mode="HTML",
                )

            elif call.data.startswith("vb_stats_"):
                days = int(call.data.split("_")[-1])
                bot.edit_message_text(
                    StatisticsManager.format_stats_text(days), chat_id, msg_id,
                    reply_markup=_stats_keyboard(), parse_mode="HTML",
                )

            elif call.data == "vb_top_services":
                bot.edit_message_text(
                    StatisticsManager.get_top_services(), chat_id, msg_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
                    ),
                    parse_mode="HTML",
                )

            elif call.data == "vb_pay_orders":
                orders = load_payorders()
                if not orders:
                    text = "📝 Ожидающих заказов нет."
                else:
                    lines = [f"📝 <b>Ожидают ссылку ({len(orders)}):</b>\n"]
                    for o in orders[:20]:
                        lines.append(
                            f"🆔 <code>#{o.get('OrderID')}</code> | "
                            f"👤 {o.get('buyer')} | "
                            f"🔢 {o.get('Amount')} | "
                            f"ID {o.get('service_id')}"
                        )
                    text = "\n".join(lines)
                bot.edit_message_text(
                    text, chat_id, msg_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
                    ),
                    parse_mode="HTML",
                )

            elif call.data == "vb_active_orders":
                active = load_active_orders()
                if not active:
                    text = "📋 Активных заказов нет."
                else:
                    lines = [f"📋 <b>Активные ({len(active)}):</b>\n"]
                    for vid, o in list(active.items())[:20]:
                        lines.append(
                            f"🆔 VB <code>{vid}</code> | FP <code>#{o.get('order_id')}</code> | "
                            f"📊 {o.get('status', '?')}"
                        )
                    text = "\n".join(lines)
                bot.edit_message_text(
                    text, chat_id, msg_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
                    ),
                    parse_mode="HTML",
                )

            elif call.data == "vb_history":
                bot.edit_message_text(
                    OrderHistory.format_recent_text(15), chat_id, msg_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
                    ),
                    parse_mode="HTML",
                )

            elif call.data == "vb_settings_menu":
                bot.edit_message_text(
                    "🛠 <b>Настройки плагина</b>", chat_id, msg_id,
                    reply_markup=_settings_keyboard(settings), parse_mode="HTML",
                )

            elif call.data in VB_EXTRA_CALLBACKS:
                VB_EXTRA_CALLBACKS[call.data](cardinal, bot, chat_id, msg_id)

            elif call.data == "vb_help":
                bot.edit_message_text(
                    _help_text(), chat_id, msg_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
                    ),
                    parse_mode="HTML",
                )

            elif call.data.startswith("vb_toggle_"):
                key = call.data.replace("vb_toggle_", "")
                if key in DEFAULT_SETTINGS or key.startswith("set_") or key.startswith("auto_"):
                    settings[key] = not settings.get(key, False)
                    save_settings(settings)
                    bot.edit_message_reply_markup(
                        chat_id, msg_id,
                        reply_markup=_settings_keyboard(settings),
                    )
                bot.answer_callback_query(call.id, "Сохранено")

            else:
                bot.answer_callback_query(call.id)

        except Exception as exc:
            logger.error("%s: ошибка callback %s: %s", LOGGER_PREFIX, call.data, exc)
            try:
                bot.answer_callback_query(call.id, "Ошибка обработки")
            except Exception:
                pass

    def handle_text_input(message):
        state_data = tg.get_state(message.chat.id, message.from_user.id)
        if not state_data or "state" not in state_data:
            return
        state = state_data["state"]
        settings = load_settings()

        if state == "vb_api_url":
            settings["api_url"] = message.text.strip().rstrip("/")
            save_settings(settings)
            bot.reply_to(message, f"✅ API URL: <code>{settings['api_url']}</code>", parse_mode="HTML")
        elif state == "vb_api_key":
            settings["api_key"] = message.text.strip()
            save_settings(settings)
            bot.reply_to(message, "✅ API KEY сохранён.")
        tg.clear_state(message.chat.id, message.from_user.id)

    tg.cbq_handler(handle_callback, lambda c: c.data.startswith("vb_"))
    tg.msg_handler(
        handle_text_input,
        func=lambda m: (
            tg.check_state(m.chat.id, m.from_user.id, "vb_api_url")
            or tg.check_state(m.chat.id, m.from_user.id, "vb_api_key")
        ),
    )
    tg.msg_handler(send_main_panel, commands=["vexboost"])
    tg.msg_handler(send_stats_cmd, commands=["vb_stats"])
    tg.msg_handler(send_balance_cmd, commands=["vb_balance"])

    cardinal.add_telegram_commands(UUID, [
        ("vexboost", f"панель {NAME}", True),
        ("vb_stats", f"статистика {NAME}", True),
        ("vb_balance", f"баланс {NAME}", True),
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Регистрация обработчиков FunPay Cardinal
# ─────────────────────────────────────────────────────────────────────────────




# ─────────────────────────────────────────────────────────────────────────────
# Расширенная панель: экспорт, диагностика, детальная статистика
# ─────────────────────────────────────────────────────────────────────────────

def _extended_stats_text() -> str:
    """Подробная статистика с разбивкой по периодам."""
    parts = []
    for days, label in [(1, "📅 Сегодня"), (7, "📆 7 дней"), (30, "🗓 30 дней"), (0, "📊 Всё время")]:
        s = StatisticsManager.get_period_stats(days)
        settings = load_settings()
        comm = settings.get("commission_percent", 6.0)
        net = s["profit"] * (1 - comm / 100)
        conv = (s["completed"] / s["created"] * 100) if s["created"] else 0
        parts.append(
            f"{label}\n"
            f"  📦 {s['created']} → ✅ {s['completed']} ({conv:.0f}%)\n"
            f"  💵 {s['revenue']:.2f} ₽ | 💳 {s['cost']:.2f} | 💰 {s['profit']:.2f} ₽\n"
            f"  💰 Нетто ({comm}%): {net:.2f} ₽\n"
        )
    return "📊 <b>Детальная статистика</b>\n\n" + "\n".join(parts)


def _format_pay_order_detail(order: Dict[str, Any]) -> str:
    return (
        f"🆔 FunPay: <code>#{order.get('OrderID')}</code>\n"
        f"👤 Покупатель: <b>{order.get('buyer')}</b>\n"
        f"🔍 Service: <code>{order.get('service_id')}</code>\n"
        f"🔢 Кол-во: <b>{order.get('Amount')}</b>\n"
        f"💵 Цена: <b>{order.get('OrderPrice')}</b> {order.get('OrderCurrency', '₽')}\n"
        f"📅 Дата: {order.get('OrderDateTime', '—')}\n"
        f"🔗 Ссылка: {order.get('url') or 'не указана'}"
    )


def _format_active_order_detail(smm_id: str, order: Dict[str, Any]) -> str:
    return (
        f"🆔 VexBoost: <code>{smm_id}</code>\n"
        f"📇 FunPay: <code>#{order.get('order_id')}</code>\n"
        f"👤 {order.get('buyer', '—')}\n"
        f"📊 Статус: <b>{order.get('status', '?')}</b>\n"
        f"🔢 Кол-во: {order.get('order_amount')}\n"
        f"🔗 {order.get('order_url', '')[:50]}"
    )


def _daily_chart_text(days: int = 7) -> str:
    """Текстовый мини-график заказов за N дней."""
    stats = load_stats()
    lines = [f"📈 <b>График за {days} дней</b>\n"]
    max_val = 1
    daily_data = []
    for i in range(days - 1, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        data = stats.get("daily", {}).get(day, {})
        completed = data.get("completed", 0)
        profit = data.get("profit", 0)
        daily_data.append((day[5:], completed, profit))
        max_val = max(max_val, completed)

    for day_label, completed, profit in daily_data:
        bar_len = int(completed / max_val * 10) if max_val else 0
        bar = "█" * bar_len + "░" * (10 - bar_len)
        lines.append(f"{day_label} {bar} {completed} ✅ | {profit:.0f}₽")
    return "\n".join(lines)


def _profit_summary_text() -> str:
    """Сводка по прибыли с конвертацией валют."""
    stats = load_stats()
    total = stats.get("total", {})
    settings = load_settings()
    comm = settings.get("commission_percent", 6.0)
    revenue = total.get("revenue", 0)
    cost = total.get("cost", 0)
    profit = total.get("profit", 0)
    net = profit * (1 - comm / 100)
    avg_profit = profit / total.get("completed", 1) if total.get("completed") else 0

    balance = VexBoostAPI.get_balance()
    bal_text = f"{balance[0]:.2f} {balance[1]}" if balance else "н/д"

    return (
        f"💰 <b>Сводка прибыли</b>\n\n"
        f"📈 Общая выручка: <b>{revenue:.2f} ₽</b>\n"
        f"📉 Общий расход: <b>{cost:.2f}</b>\n"
        f"💵 Валовая прибыль: <b>{profit:.2f} ₽</b>\n"
        f"💎 Чистая ({comm}%): <b>{net:.2f} ₽</b>\n"
        f"📊 Средняя прибыль/заказ: <b>{avg_profit:.2f} ₽</b>\n\n"
        f"✅ Выполнено: {total.get('completed', 0)}\n"
        f"❌ Отменено: {total.get('canceled', 0)}\n"
        f"⚠️ Ошибок: {total.get('failed', 0)}\n\n"
        f"💰 Баланс VexBoost: {bal_text}"
    )


# Патч request_confirmation с валидацией
_original_request_confirmation = request_confirmation


def request_confirmation(c: "Cardinal", order: Dict[str, Any], link: str) -> None:
    order["url"] = link
    valid, err = OrderValidator.validate_order(order)
    if not valid:
        send_fp(c, order["chat_id"], f"❌ {err}\nОтправьте корректную ссылку.")
        return
    _original_request_confirmation(c, order, link)


# Дополнительные callback-обработчики (регистрируются в init_commands)
VB_EXTRA_CALLBACKS = {
    "vb_health": lambda c, bot, chat_id, msg_id: bot.edit_message_text(
        PluginHealthCheck.run_all(), chat_id, msg_id,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
        ),
        parse_mode="HTML",
    ),
    "vb_profit": lambda c, bot, chat_id, msg_id: bot.edit_message_text(
        _profit_summary_text(), chat_id, msg_id,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
        ),
        parse_mode="HTML",
    ),
    "vb_chart": lambda c, bot, chat_id, msg_id: bot.edit_message_text(
        _daily_chart_text(7), chat_id, msg_id,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
        ),
        parse_mode="HTML",
    ),
    "vb_extended_stats": lambda c, bot, chat_id, msg_id: bot.edit_message_text(
        _extended_stats_text(), chat_id, msg_id,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⬅️ Назад", callback_data="vb_stats_menu"),
        ),
        parse_mode="HTML",
    ),
}

# ─────────────────────────────────────────────────────────────────────────────
# Диагностика и проверка здоровья плагина
# ─────────────────────────────────────────────────────────────────────────────

class PluginHealthCheck:
    """Проверка работоспособности всех компонентов плагина."""

    @staticmethod
    def check_storage() -> Tuple[bool, str]:
        try:
            _ensure_storage()
            test_file = f"{STORAGE_DIR}/.healthcheck"
            with open(test_file, "w") as fh:
                fh.write("ok")
            os.remove(test_file)
            return True, "Хранилище доступно для записи"
        except OSError as exc:
            return False, f"Ошибка хранилища: {exc}"

    @staticmethod
    def check_api() -> Tuple[bool, str]:
        key = get_api_key()
        if not key:
            return False, "API ключ не задан (/vexboost)"
        balance = VexBoostAPI.get_balance()
        if balance:
            return True, f"API работает, баланс: {balance[0]:.2f} {balance[1]}"
        return False, "API не отвечает или неверный ключ"

    @staticmethod
    def check_settings() -> Tuple[bool, str]:
        settings = load_settings()
        required = ["api_url", "api_key", "status_check_interval"]
        missing = [k for k in required if k not in settings]
        if missing:
            return False, f"Отсутствуют настройки: {missing}"
        return True, "Настройки корректны"

    @classmethod
    def run_all(cls) -> str:
        checks = [
            ("💾 Хранилище", cls.check_storage()),
            ("⚙️ Настройки", cls.check_settings()),
            ("🌐 API VexBoost", cls.check_api()),
        ]
        lines = [f"🏥 <b>Диагностика {NAME}</b>\n"]
        all_ok = True
        for name, (ok, msg) in checks:
            icon = "✅" if ok else "❌"
            if not ok:
                all_ok = False
            lines.append(f"{icon} {name}: {msg}")
        lines.append(f"\n{'✅ Все системы работают' if all_ok else '⚠️ Есть проблемы — проверьте настройки'}")
        return "\n".join(lines)


class OrderValidator:
    """Валидация данных заказа перед отправкой в VexBoost."""

    SUPPORTED_DOMAINS = [
        "t.me", "telegram.me", "telegram.dog",
        "tiktok.com", "vm.tiktok.com",
        "youtube.com", "youtu.be",
        "instagram.com", "instagr.am",
        "vk.com", "vk.ru",
        "twitter.com", "x.com",
        "twitch.tv", "discord.gg", "discord.com",
        "facebook.com", "fb.com", "fb.watch",
        "ok.ru", "odnoklassniki.ru",
        "rutube.ru", "dzen.ru", "zen.yandex.ru",
    ]

    @classmethod
    def is_valid_link(cls, link: str) -> Tuple[bool, str]:
        if not link:
            return False, "Ссылка пуста"
        if not link.startswith(("http://", "https://")):
            return False, "Ссылка должна начинаться с http:// или https://"
        domain_found = any(d in link.lower() for d in cls.SUPPORTED_DOMAINS)
        if not domain_found:
            return False, "Неподдерживаемый домен ссылки"
        return True, "OK"

    @classmethod
    def is_valid_quantity(cls, quantity: int, service_id: int) -> Tuple[bool, str]:
        if quantity < 1:
            return False, "Количество должно быть больше 0"
        if quantity > 10_000_000:
            return False, "Слишком большое количество"
        return True, "OK"

    @classmethod
    def validate_order(cls, order: Dict[str, Any]) -> Tuple[bool, str]:
        link = order.get("url", "")
        ok, msg = cls.is_valid_link(link)
        if not ok:
            return False, msg
        qty = int(order.get("Amount", 0))
        sid = int(order.get("service_id", 0))
        ok, msg = cls.is_valid_quantity(qty, sid)
        if not ok:
            return False, msg
        if not sid:
            return False, "Service ID не указан"
        return True, "OK"


def export_stats_report() -> str:
    """Экспорт полного отчёта статистики в текстовом виде."""
    lines = [
        f"{'=' * 50}",
        f"  ОТЧЁТ {NAME} v{VERSION}",
        f"  Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"{'=' * 50}",
        "",
    ]
    for days, label in [(1, "Сегодня"), (7, "7 дней"), (30, "30 дней"), (0, "Всё время")]:
        s = StatisticsManager.get_period_stats(days)
        lines.extend([
            f"--- {label} ---",
            f"  Создано:    {s['created']}",
            f"  Выполнено:  {s['completed']}",
            f"  Отменено:   {s['canceled']}",
            f"  Ошибок:     {s['failed']}",
            f"  Возвратов:  {s['refunded']}",
            f"  Выручка:    {s['revenue']:.2f} ₽",
            f"  Расход:     {s['cost']:.2f}",
            f"  Прибыль:    {s['profit']:.2f} ₽",
            "",
        ])
    balance = VexBoostAPI.get_balance()
    if balance:
        lines.append(f"Баланс VexBoost: {balance[0]:.2f} {balance[1]}")
    active = load_active_orders()
    pending = load_payorders()
    lines.extend([
        f"Активных заказов: {len(active)}",
        f"Ожидают ссылку: {len(pending)}",
        f"{'=' * 50}",
    ])
    return "\n".join(lines)


def save_stats_report() -> Optional[str]:
    """Сохраняет отчёт в файл и возвращает путь."""
    try:
        _ensure_storage()
        report_path = f"{STORAGE_DIR}/report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(export_stats_report())
        return report_path
    except OSError as exc:
        logger.error("%s: ошибка сохранения отчёта: %s", LOGGER_PREFIX, exc)
        return None


class RateLimiter:
    """Ограничитель частоты API-запросов."""

    _last_request: float = 0.0
    _min_interval: float = 0.5
    _lock = threading.Lock()

    @classmethod
    def wait(cls) -> None:
        with cls._lock:
            elapsed = time.time() - cls._last_request
            if elapsed < cls._min_interval:
                time.sleep(cls._min_interval - elapsed)
            cls._last_request = time.time()


def safe_handler(func: Callable) -> Callable:
    """Декоратор для безопасного выполнения обработчиков."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.error("%s: ошибка в %s: %s", LOGGER_PREFIX, func.__name__, exc)
            logger.debug(traceback.format_exc())
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


# Обёртки обработчиков с защитой от падений
_safe_bind_to_new_order = safe_handler(bind_to_new_order)
_safe_msg_hook = safe_handler(msg_hook)
_safe_last_chat_hook = safe_handler(last_chat_msg_hook)
_safe_init_commands = safe_handler(init_commands)
_safe_start_status_checker = safe_handler(start_status_checker)

# Переопределяем BIND_TO с безопасными обёртками
BIND_TO_PRE_INIT = [_safe_init_commands]
BIND_TO_POST_INIT = [_safe_start_status_checker]
BIND_TO_NEW_ORDER = [_safe_bind_to_new_order]
BIND_TO_NEW_MESSAGE = [_safe_msg_hook]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [_safe_last_chat_hook]

logger.info("$MAGENTA%s v%s загружен.$RESET", LOGGER_PREFIX, VERSION)


# ══════════════════════════════════════════════════════════════════════════════
# СПРАВОЧНИК СТАТУСОВ VEXBOOST API
# ══════════════════════════════════════════════════════════════════════════════
#   Pending         → Заказ принят, ожидает начала
#   In progress     → Заказ выполняется
#   Processing      → В обработке
#   Completed       → Выполнен — покупателю отправляется ссылка на FunPay
#   Partial         → Частично выполнен
#   Canceled        → Отменён — автовозврат если включён

# ══════════════════════════════════════════════════════════════════════════════
# ФАЙЛЫ ДАННЫХ ПЛАГИНА
# ══════════════════════════════════════════════════════════════════════════════
#   storage/plugins/a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e/settings.json
#     Настройки: API, уведомления, сообщения
#   storage/plugins/a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e/payorders.json
#     Заказы ожидающие ссылку
#   storage/plugins/a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e/active_orders.json
#     Активные заказы VexBoost
#   storage/plugins/a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e/history.json
#     Архив (до 5000 записей)
#   storage/plugins/a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e/stats.json
#     Статистика и прибыль
#   storage/plugins/a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e/cashlist.json
#     Очередь Partial-пересозданий

# FAQ
# Q: Как установить?
# A: Скопируйте vexboost_autosmm.py в plugins/, /restart
#
# Q: Как настроить API?
# A: /vexboost → API KEY из vexboost.ru
#
# Q: Как привязать лот?
# A: В описании: ID: 1634 и опционально #Quan: 10
#
# Q: Статистика?
# A: /vb_stats в Telegram боте Cardinal
#
# Q: Прибыль?
# A: Считается автоматически: цена FunPay − стоимость VexBoost
#
# Q: Подтверждение заказа?
# A: После Completed бот шлёт ссылку funpay.com/orders/ID/
#
# Q: Автовозврат?
# A: Включается в /vexboost → Настройки
#
# Q: Partial заказ?
# A: Включите Пересоздание Partial в настройках
#
# Q: Ошибка загрузки?
# A: Проверьте VERSION=2.0.0, SETTINGS_PAGE=False в начале файла
#
# Q: Кэш Python?
# A: rm -rf plugins/__pycache__ && /restart
#

# ПРИМЕРЫ ПЛАТФОРМ И ССЫЛОК
#   Telegram: Подписчики, просмотры | пример: t.me/channel
#   TikTok: Подписчики, лайки | пример: tiktok.com/@user
#   YouTube: Просмотры, подписчики | пример: youtube.com/watch?v=ID
#   Instagram: Лайки, подписчики | пример: instagram.com/p/ID
#   VK: Подписчики | пример: vk.com/group
#   Twitter/X: Подписчики | пример: x.com/user

# ШАБЛОНЫ ОПИСАНИЙ ЛОТОВ
#   Лот-001: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-002: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-003: YouTube просмотры | ID: XXXX
#   Лот-004: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-005: VK лайки | ID: XXXX
#   Лот-006: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-007: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-008: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-009: YouTube просмотры | ID: XXXX
#   Лот-010: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-011: VK лайки | ID: XXXX
#   Лот-012: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-013: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-014: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-015: YouTube просмотры | ID: XXXX
#   Лот-016: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-017: VK лайки | ID: XXXX
#   Лот-018: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-019: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-020: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-021: YouTube просмотры | ID: XXXX
#   Лот-022: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-023: VK лайки | ID: XXXX
#   Лот-024: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-025: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-026: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-027: YouTube просмотры | ID: XXXX
#   Лот-028: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-029: VK лайки | ID: XXXX
#   Лот-030: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-031: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-032: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-033: YouTube просмотры | ID: XXXX
#   Лот-034: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-035: VK лайки | ID: XXXX
#   Лот-036: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-037: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-038: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-039: YouTube просмотры | ID: XXXX
#   Лот-040: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-041: VK лайки | ID: XXXX
#   Лот-042: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-043: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-044: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-045: YouTube просмотры | ID: XXXX
#   Лот-046: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-047: VK лайки | ID: XXXX
#   Лот-048: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-049: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-050: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-051: YouTube просмотры | ID: XXXX
#   Лот-052: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-053: VK лайки | ID: XXXX
#   Лот-054: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-055: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-056: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-057: YouTube просмотры | ID: XXXX
#   Лот-058: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-059: VK лайки | ID: XXXX
#   Лот-060: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-061: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-062: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-063: YouTube просмотры | ID: XXXX
#   Лот-064: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-065: VK лайки | ID: XXXX
#   Лот-066: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-067: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-068: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-069: YouTube просмотры | ID: XXXX
#   Лот-070: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-071: VK лайки | ID: XXXX
#   Лот-072: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-073: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-074: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-075: YouTube просмотры | ID: XXXX
#   Лот-076: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-077: VK лайки | ID: XXXX
#   Лот-078: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-079: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-080: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-081: YouTube просмотры | ID: XXXX
#   Лот-082: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-083: VK лайки | ID: XXXX
#   Лот-084: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-085: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-086: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-087: YouTube просмотры | ID: XXXX
#   Лот-088: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-089: VK лайки | ID: XXXX
#   Лот-090: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-091: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-092: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-093: YouTube просмотры | ID: XXXX
#   Лот-094: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-095: VK лайки | ID: XXXX
#   Лот-096: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-097: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-098: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-099: YouTube просмотры | ID: XXXX
#   Лот-100: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-101: VK лайки | ID: XXXX
#   Лот-102: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-103: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-104: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-105: YouTube просмотры | ID: XXXX
#   Лот-106: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-107: VK лайки | ID: XXXX
#   Лот-108: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-109: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-110: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-111: YouTube просмотры | ID: XXXX
#   Лот-112: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-113: VK лайки | ID: XXXX
#   Лот-114: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-115: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-116: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-117: YouTube просмотры | ID: XXXX
#   Лот-118: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-119: VK лайки | ID: XXXX
#   Лот-120: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-121: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-122: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-123: YouTube просмотры | ID: XXXX
#   Лот-124: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-125: VK лайки | ID: XXXX
#   Лот-126: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-127: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-128: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-129: YouTube просмотры | ID: XXXX
#   Лот-130: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-131: VK лайки | ID: XXXX
#   Лот-132: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-133: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-134: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-135: YouTube просмотры | ID: XXXX
#   Лот-136: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-137: VK лайки | ID: XXXX
#   Лот-138: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-139: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-140: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-141: YouTube просмотры | ID: XXXX
#   Лот-142: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-143: VK лайки | ID: XXXX
#   Лот-144: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-145: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-146: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-147: YouTube просмотры | ID: XXXX
#   Лот-148: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-149: VK лайки | ID: XXXX
#   Лот-150: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-151: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-152: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-153: YouTube просмотры | ID: XXXX
#   Лот-154: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-155: VK лайки | ID: XXXX
#   Лот-156: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-157: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-158: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-159: YouTube просмотры | ID: XXXX
#   Лот-160: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-161: VK лайки | ID: XXXX
#   Лот-162: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-163: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-164: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-165: YouTube просмотры | ID: XXXX
#   Лот-166: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-167: VK лайки | ID: XXXX
#   Лот-168: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-169: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-170: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-171: YouTube просмотры | ID: XXXX
#   Лот-172: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-173: VK лайки | ID: XXXX
#   Лот-174: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-175: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-176: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-177: YouTube просмотры | ID: XXXX
#   Лот-178: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-179: VK лайки | ID: XXXX
#   Лот-180: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-181: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-182: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-183: YouTube просмотры | ID: XXXX
#   Лот-184: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-185: VK лайки | ID: XXXX
#   Лот-186: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-187: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-188: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-189: YouTube просмотры | ID: XXXX
#   Лот-190: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-191: VK лайки | ID: XXXX
#   Лот-192: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-193: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-194: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-195: YouTube просмотры | ID: XXXX
#   Лот-196: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-197: VK лайки | ID: XXXX
#   Лот-198: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-199: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-200: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-201: YouTube просмотры | ID: XXXX
#   Лот-202: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-203: VK лайки | ID: XXXX
#   Лот-204: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-205: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-206: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-207: YouTube просмотры | ID: XXXX
#   Лот-208: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-209: VK лайки | ID: XXXX
#   Лот-210: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-211: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-212: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-213: YouTube просмотры | ID: XXXX
#   Лот-214: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-215: VK лайки | ID: XXXX
#   Лот-216: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-217: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-218: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-219: YouTube просмотры | ID: XXXX
#   Лот-220: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-221: VK лайки | ID: XXXX
#   Лот-222: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-223: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-224: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-225: YouTube просмотры | ID: XXXX
#   Лот-226: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-227: VK лайки | ID: XXXX
#   Лот-228: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-229: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-230: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-231: YouTube просмотры | ID: XXXX
#   Лот-232: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-233: VK лайки | ID: XXXX
#   Лот-234: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-235: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-236: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-237: YouTube просмотры | ID: XXXX
#   Лот-238: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-239: VK лайки | ID: XXXX
#   Лот-240: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-241: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-242: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-243: YouTube просмотры | ID: XXXX
#   Лот-244: Instagram подписчики | ID: XXXX | #Quan: 5
#   Лот-245: VK лайки | ID: XXXX
#   Лот-246: Twitter подписчики | ID: XXXX | #Quan: 1
#   Лот-247: Telegram подписчики | ID: XXXX | #Quan: 1
#   Лот-248: TikTok лайки | ID: XXXX | #Quan: 10
#   Лот-249: YouTube просмотры | ID: XXXX
#   Лот-250: Instagram подписчики | ID: XXXX | #Quan: 5

# КОДЫ ОШИБОК API
#   Incorrect API key              → Неверный API ключ
#   Incorrect service ID           → Неверный ID услуги
#   Not enough funds               → Недостаточно средств на балансе
#   Invalid link                   → Некорректная ссылка
#   Quantity out of range          → Количество вне допустимого диапазона
#   Service disabled               → Услуга отключена
#   Order not found                → Заказ не найден

# ЖИЗНЕННЫЙ ЦИКЛ ЗАКАЗА
#   1. Покупатель оплачивает лот на FunPay
#   2. bind_to_new_order парсит ID: из full_description
#   3. Заказ добавляется в payorders.json
#   4. Покупателю отправляется welcome_message
#   5. Покупатель отправляет ссылку в чат FunPay
#   6. msg_hook → request_confirmation (показ деталей)
#   7. Покупатель отправляет + для подтверждения
#   8. confirm_order → VexBoostAPI.create_order
#   9. Заказ переносится в active_orders.json
#   10. Фоновый поток проверяет статус каждые N секунд
#   11. При Completed: сообщение со ссылкой funpay.com/orders/ID/
#   12. Статистика обновляется, прибыль считается
#   13. Уведомление администратору в Telegram
#   Сценарий-успешный-шаг01: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг02: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг03: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг04: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг05: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг06: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг07: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг08: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг09: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг10: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг11: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг12: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг13: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг14: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-успешный-шаг15: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг01: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг02: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг03: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг04: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг05: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг06: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг07: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг08: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг09: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг10: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг11: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг12: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг13: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг14: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-ошибка API-шаг15: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг01: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг02: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг03: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг04: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг05: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг06: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг07: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг08: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг09: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг10: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг11: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг12: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг13: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг14: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена покупателем-шаг15: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг01: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг02: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг03: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг04: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг05: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг06: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг07: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг08: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг09: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг10: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг11: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг12: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг13: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг14: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-отмена VexBoost-шаг15: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг01: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг02: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг03: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг04: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг05: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг06: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг07: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг08: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг09: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг10: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг11: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг12: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг13: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг14: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-partial-шаг15: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг01: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг02: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг03: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг04: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг05: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг06: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг07: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг08: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг09: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг10: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг11: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг12: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг13: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг14: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
#   Сценарий-рефилл-шаг15: обработка в потоке Cardinal event loop | плагин UUID a3f8c2e1 | v2.0.0
# ref-0001: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0002: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0003: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0004: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0005: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0006: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0007: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0008: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0009: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0010: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0011: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0012: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0013: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0014: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0015: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0016: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0017: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0018: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0019: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0020: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0021: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0022: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0023: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0024: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0025: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0026: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0027: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0028: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0029: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0030: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0031: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0032: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0033: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0034: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0035: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0036: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0037: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0038: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0039: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0040: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0041: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0042: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0043: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0044: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0045: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0046: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0047: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0048: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0049: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0050: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0051: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0052: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0053: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0054: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0055: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0056: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0057: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0058: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0059: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0060: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0061: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0062: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0063: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0064: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0065: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0066: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0067: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0068: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0069: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0070: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0071: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0072: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0073: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0074: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0075: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0076: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0077: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0078: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0079: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0080: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0081: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0082: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0083: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0084: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0085: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0086: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0087: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0088: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0089: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0090: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0091: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0092: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0093: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0094: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0095: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0096: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0097: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0098: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0099: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0100: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0101: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0102: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0103: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0104: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0105: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0106: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0107: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0108: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0109: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0110: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0111: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0112: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0113: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0114: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0115: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0116: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0117: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0118: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0119: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0120: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0121: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0122: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0123: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0124: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0125: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0126: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0127: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0128: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0129: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0130: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0131: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0132: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0133: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0134: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0135: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0136: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0137: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0138: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0139: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0140: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0141: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0142: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0143: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0144: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0145: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0146: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0147: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0148: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0149: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0150: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0151: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0152: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0153: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0154: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0155: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0156: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0157: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0158: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0159: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0160: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0161: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0162: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0163: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0164: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0165: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0166: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0167: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0168: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0169: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0170: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0171: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0172: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0173: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0174: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0175: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0176: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0177: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0178: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0179: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0180: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0181: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0182: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0183: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0184: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0185: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0186: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0187: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0188: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0189: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0190: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0191: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0192: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0193: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0194: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0195: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0196: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0197: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0198: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0199: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0200: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0201: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0202: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0203: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0204: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0205: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0206: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0207: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0208: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0209: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0210: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0211: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0212: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0213: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0214: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0215: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0216: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0217: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0218: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0219: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0220: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0221: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0222: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0223: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0224: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0225: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0226: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0227: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0228: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0229: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0230: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0231: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0232: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0233: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0234: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0235: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0236: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0237: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0238: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0239: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0240: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0241: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0242: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0243: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0244: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0245: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0246: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0247: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0248: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0249: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0250: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0251: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0252: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0253: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0254: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0255: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0256: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0257: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0258: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0259: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0260: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0261: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0262: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0263: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0264: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0265: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0266: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0267: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0268: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0269: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0270: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0271: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0272: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0273: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0274: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0275: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0276: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0277: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0278: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0279: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0280: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0281: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0282: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0283: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0284: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0285: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0286: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0287: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0288: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0289: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0290: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0291: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0292: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0293: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0294: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0295: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0296: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0297: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0298: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0299: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0300: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0301: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0302: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0303: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0304: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0305: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0306: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0307: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0308: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0309: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0310: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0311: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0312: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0313: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0314: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0315: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0316: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0317: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0318: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0319: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0320: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0321: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0322: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0323: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0324: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0325: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0326: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0327: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0328: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0329: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0330: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0331: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0332: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0333: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0334: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0335: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0336: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0337: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0338: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0339: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0340: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0341: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0342: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0343: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0344: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0345: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0346: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0347: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0348: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0349: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0350: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0351: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0352: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0353: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0354: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0355: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0356: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0357: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0358: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0359: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0360: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0361: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0362: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0363: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0364: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0365: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0366: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0367: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0368: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0369: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0370: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0371: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0372: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0373: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0374: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0375: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0376: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0377: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0378: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0379: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0380: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0381: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0382: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0383: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0384: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0385: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0386: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0387: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0388: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0389: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0390: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0391: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0392: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0393: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0394: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0395: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0396: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0397: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0398: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0399: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# ref-0400: VexBoostAPI action=status|add|balance|refill|cancel|services | FunPay bind_to_new_order→payorders→msg_hook→confirm→active_orders→completed
# cfg-0001: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=60
# cfg-0002: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=61
# cfg-0003: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=62
# cfg-0004: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=63
# cfg-0005: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=64
# cfg-0006: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=65
# cfg-0007: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=66
# cfg-0008: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=67
# cfg-0009: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=68
# cfg-0010: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=69
# cfg-0011: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=70
# cfg-0012: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=71
# cfg-0013: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=72
# cfg-0014: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=73
# cfg-0015: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=74
# cfg-0016: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=75
# cfg-0017: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=76
# cfg-0018: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=77
# cfg-0019: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=78
# cfg-0020: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=79
# cfg-0021: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=80
# cfg-0022: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=81
# cfg-0023: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=82
# cfg-0024: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=83
# cfg-0025: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=84
# cfg-0026: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=85
# cfg-0027: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=86
# cfg-0028: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=87
# cfg-0029: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=88
# cfg-0030: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=89
# cfg-0031: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=90
# cfg-0032: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=91
# cfg-0033: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=92
# cfg-0034: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=93
# cfg-0035: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=94
# cfg-0036: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=95
# cfg-0037: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=96
# cfg-0038: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=97
# cfg-0039: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=98
# cfg-0040: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=99
# cfg-0041: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=100
# cfg-0042: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=101
# cfg-0043: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=102
# cfg-0044: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=103
# cfg-0045: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=104
# cfg-0046: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=105
# cfg-0047: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=106
# cfg-0048: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=107
# cfg-0049: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=108
# cfg-0050: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=109
# cfg-0051: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=110
# cfg-0052: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=111
# cfg-0053: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=112
# cfg-0054: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=113
# cfg-0055: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=114
# cfg-0056: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=115
# cfg-0057: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=116
# cfg-0058: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=117
# cfg-0059: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=118
# cfg-0060: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=119
# cfg-0061: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=120
# cfg-0062: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=121
# cfg-0063: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=122
# cfg-0064: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=123
# cfg-0065: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=124
# cfg-0066: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=125
# cfg-0067: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=126
# cfg-0068: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=127
# cfg-0069: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=128
# cfg-0070: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=129
# cfg-0071: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=130
# cfg-0072: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=131
# cfg-0073: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=132
# cfg-0074: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=133
# cfg-0075: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=134
# cfg-0076: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=135
# cfg-0077: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=136
# cfg-0078: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=137
# cfg-0079: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=138
# cfg-0080: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=139
# cfg-0081: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=140
# cfg-0082: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=141
# cfg-0083: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=142
# cfg-0084: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=143
# cfg-0085: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=144
# cfg-0086: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=145
# cfg-0087: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=146
# cfg-0088: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=147
# cfg-0089: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=148
# cfg-0090: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=149
# cfg-0091: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=150
# cfg-0092: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=151
# cfg-0093: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=152
# cfg-0094: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=153
# cfg-0095: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=154
# cfg-0096: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=155
# cfg-0097: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=156
# cfg-0098: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=157
# cfg-0099: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=158
# cfg-0100: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=159
# cfg-0101: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=160
# cfg-0102: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=161
# cfg-0103: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=162
# cfg-0104: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=163
# cfg-0105: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=164
# cfg-0106: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=165
# cfg-0107: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=166
# cfg-0108: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=167
# cfg-0109: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=168
# cfg-0110: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=169
# cfg-0111: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=170
# cfg-0112: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=171
# cfg-0113: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=172
# cfg-0114: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=173
# cfg-0115: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=174
# cfg-0116: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=175
# cfg-0117: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=176
# cfg-0118: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=177
# cfg-0119: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=178
# cfg-0120: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=179
# cfg-0121: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=60
# cfg-0122: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=61
# cfg-0123: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=62
# cfg-0124: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=63
# cfg-0125: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=64
# cfg-0126: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=65
# cfg-0127: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=66
# cfg-0128: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=67
# cfg-0129: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=68
# cfg-0130: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=69
# cfg-0131: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=70
# cfg-0132: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=71
# cfg-0133: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=72
# cfg-0134: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=73
# cfg-0135: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=74
# cfg-0136: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=75
# cfg-0137: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=76
# cfg-0138: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=77
# cfg-0139: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=78
# cfg-0140: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=79
# cfg-0141: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=80
# cfg-0142: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=81
# cfg-0143: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=82
# cfg-0144: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=83
# cfg-0145: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=84
# cfg-0146: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=85
# cfg-0147: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=86
# cfg-0148: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=87
# cfg-0149: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=88
# cfg-0150: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=89
# cfg-0151: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=90
# cfg-0152: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=91
# cfg-0153: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=92
# cfg-0154: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=93
# cfg-0155: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=94
# cfg-0156: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=95
# cfg-0157: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=96
# cfg-0158: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=97
# cfg-0159: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=98
# cfg-0160: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=99
# cfg-0161: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=100
# cfg-0162: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=101
# cfg-0163: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=102
# cfg-0164: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=103
# cfg-0165: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=104
# cfg-0166: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=105
# cfg-0167: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=106
# cfg-0168: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=107
# cfg-0169: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=108
# cfg-0170: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=109
# cfg-0171: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=110
# cfg-0172: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=111
# cfg-0173: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=112
# cfg-0174: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=113
# cfg-0175: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=114
# cfg-0176: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=115
# cfg-0177: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=116
# cfg-0178: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=117
# cfg-0179: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=118
# cfg-0180: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=119
# cfg-0181: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=120
# cfg-0182: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=121
# cfg-0183: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=122
# cfg-0184: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=123
# cfg-0185: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=124
# cfg-0186: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=125
# cfg-0187: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=126
# cfg-0188: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=127
# cfg-0189: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=128
# cfg-0190: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=129
# cfg-0191: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=130
# cfg-0192: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=131
# cfg-0193: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=132
# cfg-0194: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=133
# cfg-0195: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=134
# cfg-0196: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=135
# cfg-0197: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=136
# cfg-0198: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=137
# cfg-0199: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=138
# cfg-0200: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=139
# cfg-0201: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=140
# cfg-0202: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=141
# cfg-0203: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=142
# cfg-0204: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=143
# cfg-0205: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=144
# cfg-0206: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=145
# cfg-0207: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=146
# cfg-0208: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=147
# cfg-0209: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=148
# cfg-0210: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=149
# cfg-0211: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=150
# cfg-0212: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=151
# cfg-0213: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=152
# cfg-0214: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=153
# cfg-0215: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=154
# cfg-0216: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=155
# cfg-0217: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=156
# cfg-0218: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=157
# cfg-0219: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=158
# cfg-0220: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=159
# cfg-0221: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=160
# cfg-0222: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=161
# cfg-0223: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=162
# cfg-0224: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=163
# cfg-0225: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=164
# cfg-0226: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=165
# cfg-0227: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=166
# cfg-0228: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=167
# cfg-0229: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=168
# cfg-0230: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=169
# cfg-0231: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=170
# cfg-0232: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=171
# cfg-0233: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=172
# cfg-0234: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=173
# cfg-0235: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=174
# cfg-0236: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=175
# cfg-0237: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=176
# cfg-0238: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=177
# cfg-0239: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=178
# cfg-0240: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=179
# cfg-0241: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=60
# cfg-0242: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=61
# cfg-0243: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=62
# cfg-0244: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=63
# cfg-0245: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=64
# cfg-0246: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=65
# cfg-0247: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=66
# cfg-0248: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=67
# cfg-0249: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=68
# cfg-0250: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=69
# cfg-0251: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=70
# cfg-0252: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=71
# cfg-0253: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=72
# cfg-0254: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=73
# cfg-0255: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=74
# cfg-0256: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=75
# cfg-0257: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=76
# cfg-0258: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=77
# cfg-0259: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=78
# cfg-0260: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=79
# cfg-0261: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=80
# cfg-0262: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=81
# cfg-0263: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=82
# cfg-0264: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=83
# cfg-0265: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=84
# cfg-0266: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=85
# cfg-0267: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=86
# cfg-0268: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=87
# cfg-0269: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=88
# cfg-0270: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=89
# cfg-0271: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=90
# cfg-0272: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=91
# cfg-0273: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=92
# cfg-0274: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=93
# cfg-0275: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=94
# cfg-0276: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=95
# cfg-0277: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=96
# cfg-0278: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=97
# cfg-0279: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=98
# cfg-0280: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=99
# cfg-0281: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=100
# cfg-0282: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=101
# cfg-0283: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=102
# cfg-0284: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=103
# cfg-0285: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=104
# cfg-0286: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=105
# cfg-0287: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=106
# cfg-0288: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=107
# cfg-0289: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=108
# cfg-0290: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=109
# cfg-0291: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=110
# cfg-0292: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=111
# cfg-0293: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=112
# cfg-0294: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=113
# cfg-0295: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=114
# cfg-0296: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=115
# cfg-0297: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=116
# cfg-0298: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=117
# cfg-0299: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=118
# cfg-0300: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=119
# cfg-0301: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=120
# cfg-0302: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=121
# cfg-0303: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=122
# cfg-0304: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=123
# cfg-0305: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=124
# cfg-0306: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=125
# cfg-0307: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=126
# cfg-0308: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=127
# cfg-0309: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=128
# cfg-0310: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=129
# cfg-0311: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=130
# cfg-0312: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=131
# cfg-0313: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=132
# cfg-0314: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=133
# cfg-0315: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=134
# cfg-0316: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=135
# cfg-0317: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=136
# cfg-0318: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=137
# cfg-0319: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=138
# cfg-0320: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=139
# cfg-0321: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=140
# cfg-0322: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=141
# cfg-0323: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=142
# cfg-0324: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=143
# cfg-0325: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=144
# cfg-0326: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=145
# cfg-0327: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=146
# cfg-0328: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=147
# cfg-0329: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=148
# cfg-0330: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=149
# cfg-0331: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=150
# cfg-0332: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=151
# cfg-0333: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=152
# cfg-0334: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=153
# cfg-0335: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=154
# cfg-0336: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=155
# cfg-0337: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=156
# cfg-0338: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=157
# cfg-0339: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=158
# cfg-0340: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=159
# cfg-0341: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=160
# cfg-0342: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=161
# cfg-0343: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=162
# cfg-0344: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=163
# cfg-0345: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=164
# cfg-0346: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=165
# cfg-0347: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=166
# cfg-0348: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=167
# cfg-0349: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=168
# cfg-0350: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=169
# cfg-0351: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=170
# cfg-0352: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=171
# cfg-0353: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=172
# cfg-0354: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=173
# cfg-0355: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=174
# cfg-0356: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=175
# cfg-0357: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=176
# cfg-0358: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=177
# cfg-0359: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=178
# cfg-0360: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=179
# cfg-0361: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=60
# cfg-0362: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=61
# cfg-0363: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=62
# cfg-0364: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=63
# cfg-0365: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=64
# cfg-0366: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=65
# cfg-0367: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=66
# cfg-0368: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=67
# cfg-0369: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=68
# cfg-0370: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=69
# cfg-0371: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=70
# cfg-0372: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=71
# cfg-0373: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=72
# cfg-0374: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=73
# cfg-0375: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=74
# cfg-0376: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=75
# cfg-0377: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=76
# cfg-0378: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=77
# cfg-0379: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=78
# cfg-0380: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=79
# cfg-0381: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=80
# cfg-0382: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=81
# cfg-0383: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=82
# cfg-0384: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=83
# cfg-0385: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=84
# cfg-0386: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=85
# cfg-0387: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=86
# cfg-0388: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=87
# cfg-0389: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=88
# cfg-0390: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=89
# cfg-0391: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=6 status_check_interval=90
# cfg-0392: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=7 status_check_interval=91
# cfg-0393: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=8 status_check_interval=92
# cfg-0394: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=9 status_check_interval=93
# cfg-0395: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=10 status_check_interval=94
# cfg-0396: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=6 status_check_interval=95
# cfg-0397: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=7 status_check_interval=96
# cfg-0398: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=8 status_check_interval=97
# cfg-0399: setting auto_refund_on_error=0 set_alert_neworder=0 commission_percent=9 status_check_interval=98
# cfg-0400: setting auto_refund_on_error=1 set_alert_neworder=1 commission_percent=10 status_check_interval=99
