from __future__ import annotations

# === ОБЯЗАТЕЛЬНЫЕ ПОЛЯ FunPay Cardinal (НЕ УДАЛЯТЬ) ===
NAME = "VexBoost AutoSMM"
VERSION = "2.4.4"
DESCRIPTION = "Автонакрутка SMM-услуг для FunPay Cardinal"
CREDITS = "@xei1y"
UUID = "a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e"
SETTINGS_PAGE = False
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
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple, Union

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
SUBMITTED_ORDERS_FILE = f"{STORAGE_DIR}/submitted_orders.json"

# ─────────────────────────────────────────────────────────────────────────────
# Настройки по умолчанию
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS: Dict[str, Any] = {
    "auth_mode": "login",
    "panel_url": "https://vexboost.ru",
    "vexboost_login": "",
    "vexboost_password": "",
    "auth_token": "",
    "cookie_name": "socpanel_session",
    "session_ttl": 5400,
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
        "Отправьте ссылку на аккаунт или пост для накрутки."
    ),
    "confirmation_message": (
        "📋 Проверьте детали заказа:\n\n"
        "🛒 Лот: {lot}\n"
        "🔢 Количество: {amount} шт.\n"
        "🔗 Ссылка: {link}\n\n"
        "✅ Отправьте + для подтверждения\n"
        "❌ Отправьте - для отмены и возврата\n"
        "🔄 Или отправьте новую ссылку"
    ),
    "creating_order_message": "⏳ Создаю заказ, подождите...",
    "order_created_message": (
        "📊 Заказ создан и отправлен SMM–сервису!\n"
        "🆔 ID заказа: {smm_id}\n\n"
        "📋 Команды:\n"
        "⠀∟ #статус {smm_id}\n"
        "⠀∟ #рефилл {smm_id}\n\n"
        "⌛ Время выполнения: от нескольких минут до 48 часов."
    ),
    "order_cancelled_message": "❌ Заказ отменён. Средства будут возвращены.",
    "order_canceled_message": (
        "❌ Заказ #{funpay_id} отменён.\n"
        "Средства будут возвращены."
    ),
    "completion_message": (
        "✅ Заказ #{order_id} выполнен!\n\n"
        "Пожалуйста, перейдите по ссылке и нажмите «Подтвердить выполнение заказа»:\n"
        "🔗 https://funpay.com/orders/{order_id}/\n\n"
        "Спасибо за покупку! 🙏"
    ),
    "pending_hint_message": (
        "⚪️ Отправьте + для подтверждения, - для отмены или новую ссылку."
    ),
    "send_link_first_message": "⚪️ Сначала отправьте ссылку для накрутки.",
    "private_telegram_message": (
        "❌ Закрытые Telegram-каналы/группы не поддерживаются.\n"
        "Используйте публичную ссылку: https://t.me/your_channel"
    ),
    "invalid_link_message": "❌ {error}\nОтправьте корректную ссылку.",
    "error_message": "❌ {error}",
    "status_usage_message": "Использование: #статус ID",
    "status_error_message": "🔴 Не удалось получить статус заказа.",
    "status_message": (
        "📈 Статус заказа {smm_id}\n"
        "⠀∟ 📊 Статус: {status}\n"
        "⠀∟ 🔢 Было: {start_count}\n"
        "⠀∟ 👀 Остаток: {remains}"
    ),
    "refill_usage_message": "Использование: #рефилл ID",
    "refill_success_message": "✅ Запрос на рефилл отправлен!",
    "refill_error_message": (
        "🔴 Ошибка рефилла. Возможно, рефилл ещё недоступен для этой услуги."
    ),
    "partial_paused_message": (
        "⚠️ Заказ #{funpay_id} приостановлен.\n"
        "Остаток: {remains} ед.\n"
        "Обратитесь к продавцу."
    ),
    "partial_continued_message": (
        "📈 Заказ #{funpay_id} продолжен.\n"
        "⏳ Остаток к выполнению: {partial_amount} ед."
    ),
}

MESSAGE_TEMPLATE_LABELS: Dict[str, Tuple[str, str]] = {
    "welcome_message": ("👋 Приветствие", "—"),
    "confirmation_message": ("📋 Подтверждение ссылки", "{lot}, {amount}, {link}"),
    "creating_order_message": ("⏳ Создание заказа", "—"),
    "order_created_message": ("📊 Заказ создан", "{smm_id}, {funpay_id}"),
    "order_cancelled_message": ("❌ Отмена покупателем", "—"),
    "order_canceled_message": ("❌ Отмена SMM", "{funpay_id}"),
    "completion_message": ("✅ Выполнение", "{order_id}"),
    "pending_hint_message": ("💬 Ожидание +/-", "—"),
    "send_link_first_message": ("🔗 Нужна ссылка", "—"),
    "private_telegram_message": ("🔒 Закрытый TG", "—"),
    "invalid_link_message": ("⚠️ Неверная ссылка", "{error}"),
    "error_message": ("⚠️ Ошибка заказа", "{error}"),
    "status_usage_message": ("#статус — справка", "—"),
    "status_error_message": ("#статус — ошибка", "—"),
    "status_message": ("#статус — ответ", "{smm_id}, {status}, {start_count}, {remains}"),
    "refill_usage_message": ("#рефилл — справка", "—"),
    "refill_success_message": ("#рефилл — успех", "—"),
    "refill_error_message": ("#рефилл — ошибка", "—"),
    "partial_paused_message": ("⏸ Partial пауза", "{funpay_id}, {remains}"),
    "partial_continued_message": ("▶️ Partial продолжение", "{funpay_id}, {partial_amount}"),
}


def _format_buyer_template(template_key: str, **kwargs: Any) -> str:
    settings = load_settings()
    template = settings.get(template_key, DEFAULT_SETTINGS.get(template_key, ""))
    if not template:
        template = DEFAULT_SETTINGS.get(template_key, "")
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError):
        result = str(template)
        for key, value in kwargs.items():
            result = result.replace("{" + key + "}", str(value))
        return result


def _template_menu_text() -> str:
    return (
        "📝 <b>Шаблоны сообщений покупателю</b>\n\n"
        "Выберите шаблон для редактирования.\n"
        "Переменные в фигурных скобках подставляются автоматически "
        "(например <code>{smm_id}</code>, <code>{order_id}</code>)."
    )


def _template_edit_prompt(template_key: str) -> str:
    label, placeholders = MESSAGE_TEMPLATE_LABELS[template_key]
    current = load_settings().get(template_key, DEFAULT_SETTINGS.get(template_key, ""))
    return (
        f"✏️ <b>{label}</b>\n\n"
        f"Переменные: <code>{html.escape(placeholders)}</code>\n\n"
        f"<b>Текущий шаблон:</b>\n<pre>{html.escape(str(current))}</pre>\n\n"
        f"Отправьте новый текст сообщения.\n"
        f"Для сброса: <code>/default</code>"
    )


def _templates_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for key, (label, _) in MESSAGE_TEMPLATE_LABELS.items():
        kb.add(InlineKeyboardButton(label, callback_data=f"vb_tpl_edit_{key}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"))
    return kb


def _template_edit_keyboard(template_key: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔄 Сбросить по умолчанию", callback_data=f"vb_tpl_reset_{template_key}"))
    kb.add(InlineKeyboardButton("⬅️ К списку шаблонов", callback_data="vb_templates_menu"))
    return kb


# ─────────────────────────────────────────────────────────────────────────────
# Глобальные переменные состояния
# ─────────────────────────────────────────────────────────────────────────────

pending_confirmations: Dict[int, Dict[str, Any]] = {}
pending_by_buyer: Dict[str, Dict[str, Any]] = {}
_file_lock = threading.RLock()
_session_cache_lock = threading.RLock()
_vexboost_session_cache: Dict[str, Any] = {"session": None, "expires_at": 0.0}
_status_thread_started = False
_fp_order_lock = threading.RLock()
_fp_orders_in_flight: Set[str] = set()
_message_dedup_lock = threading.RLock()
_recent_message_keys: Dict[str, float] = {}
_MESSAGE_DEDUP_TTL = 5.0

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


def _buyer_error_message(error: Any) -> str:
    """Сообщение об ошибке для покупателя без названия сервиса и тех. деталей."""
    text = str(error or "").strip().lower()
    if "invalid link" in text or "ссылк" in text:
        return "Некорректная ссылка. Проверьте и отправьте снова."
    if "quantity" in text or "количеств" in text:
        return "Некорректное количество для этой услуги. Обратитесь к продавцу."
    if "service" in text or "услуг" in text:
        return "Ошибка параметров заказа. Обратитесь к продавцу."
    if "fund" in text or "средств" in text or "баланс" in text:
        return "Заказ временно не может быть выполнен. Обратитесь к продавцу."
    return "Не удалось выполнить заказ. Продавец уведомлён — напишите в чат."


def _buyer_status_label(status: Any) -> str:
    mapping = {
        "pending": "В очереди",
        "in progress": "Выполняется",
        "in_progress": "Выполняется",
        "processing": "Выполняется",
        "completed": "Выполнен",
        "partial": "Частично выполнен",
        "canceled": "Отменён",
        "cancelled": "Отменён",
    }
    raw = str(status or "—").strip()
    return mapping.get(raw.lower(), raw)


def send_fp(c: "Cardinal", chat_id: Any, text: str) -> None:
    """Отправка сообщения покупателю в FunPay (без HTML-разметки)."""
    if not chat_id:
        logger.warning("%s: попытка отправить сообщение без chat_id", LOGGER_PREFIX)
        return
    cleaned = _strip_html(text)
    for token in ("vexboost", "VexBoost", "VEXBOOST", "socpanel"):
        cleaned = cleaned.replace(token, "")
    c.send_message(_normalize_chat_id(chat_id), cleaned)


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


def get_auth_mode() -> str:
    mode = str(load_settings().get("auth_mode", "login")).strip().lower()
    if mode in ("token", "api_key", "login"):
        return mode
    return "login"


def get_vexboost_login() -> str:
    return str(load_settings().get("vexboost_login", "")).strip()


def get_vexboost_password() -> str:
    return str(load_settings().get("vexboost_password", ""))


def get_panel_url() -> str:
    settings = load_settings()
    url = settings.get("panel_url") or settings.get("api_url", DEFAULT_SETTINGS["panel_url"])
    url = str(url).rstrip("/")
    if url.endswith("/api/v2"):
        url = url[:-7]
    return url.rstrip("/")


def get_auth_token() -> str:
    return str(load_settings().get("auth_token", "")).strip()


def get_cookie_name() -> str:
    return str(load_settings().get("cookie_name", "socpanel_session")).strip() or "socpanel_session"


def is_api_configured() -> bool:
    mode = get_auth_mode()
    if mode == "login":
        return bool(get_panel_url() and get_vexboost_login() and get_vexboost_password())
    if mode == "token":
        return bool(get_panel_url() and get_auth_token())
    return bool(get_api_key())


def _invalidate_vexboost_session() -> None:
    with _session_cache_lock:
        _vexboost_session_cache["session"] = None
        _vexboost_session_cache["expires_at"] = 0.0


def _mask_credential(value: str, visible: int = 3) -> str:
    text = str(value or "").strip()
    if not text:
        return "не задан"
    if len(text) <= visible:
        return "*" * len(text)
    return f"{text[:visible]}***"


def _normalize_auth_token(raw: str) -> str:
    token = (raw or "").strip()
    if not token:
        return ""
    if "=" in token and token.lower().startswith(("socpanel_session=", "authtoken=")):
        token = token.split("=", 1)[1].strip()
    return token.strip('"').strip("'")


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


def load_submitted_orders() -> Dict[str, Any]:
    return _load_json(SUBMITTED_ORDERS_FILE, {})


def save_submitted_orders(data: Dict[str, Any]) -> None:
    _save_json(SUBMITTED_ORDERS_FILE, data)


def get_submitted_smm_id(fp_id: str) -> Optional[int]:
    value = load_submitted_orders().get(str(fp_id))
    if value is not None and str(value).isdigit():
        return int(value)
    return None


def mark_submitted_order(fp_id: str, smm_id: int) -> None:
    with _file_lock:
        data = load_submitted_orders()
        data[str(fp_id)] = int(smm_id)
        save_submitted_orders(data)


def _fp_order_already_submitted(fp_id: str) -> bool:
    if get_submitted_smm_id(fp_id) is not None:
        return True
    return _fp_order_exists_in_active(fp_id)


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


def _fp_order_exists_in_active(fp_id: str) -> bool:
    active = load_active_orders()
    return any(str(info.get("order_id")) == fp_id for info in active.values())


def _try_lock_fp_order(fp_id: str) -> bool:
    with _fp_order_lock:
        if fp_id in _fp_orders_in_flight or _fp_order_already_submitted(fp_id):
            return False
        _fp_orders_in_flight.add(fp_id)
        return True


def _unlock_fp_order(fp_id: str) -> None:
    with _fp_order_lock:
        _fp_orders_in_flight.discard(fp_id)


def _is_fp_order_busy(fp_id: str) -> bool:
    with _fp_order_lock:
        if fp_id in _fp_orders_in_flight:
            return True
    return _fp_order_already_submitted(fp_id)


def _should_process_message(chat_id: Any, text: str, message_id: Any = None) -> bool:
    key = f"{chat_id}:{message_id}" if message_id is not None else f"{chat_id}:{hash(text.strip())}"
    now = time.time()
    with _message_dedup_lock:
        stale = [k for k, ts in _recent_message_keys.items() if now - ts > _MESSAGE_DEDUP_TTL]
        for stale_key in stale:
            _recent_message_keys.pop(stale_key, None)
        if key in _recent_message_keys:
            return False
        _recent_message_keys[key] = now
    return True


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
    """Клиент VexBoost: AuthToken (cookie) или стандартный API KEY."""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; VexBoostAutoSMM/2.1)",
        "Accept": "application/json",
    }

    ERROR_MESSAGES = {
        "user_inactive": "API-ключ неактивен. Используйте AuthToken или активируйте API на vexboost.ru",
        "incorrect api key": "Неверный API KEY. Обновите в /vexboost",
        "invalid api key": "Неверный API KEY. Обновите в /vexboost",
        "unauthorized": "Сессия истекла. Плагин попробует войти снова автоматически",
        "invalid_credentials": "Неверный логин или пароль VexBoost",
        "not enough funds": "Недостаточно средств на балансе VexBoost",
        "incorrect service id": "Неверный ID услуги (ID: в лоте)",
        "invalid link": "Некорректная ссылка для этой услуги",
        "quantity out of range": "Количество вне допустимого диапазона услуги",
    }

    STATUS_MAP = {
        "pending": "Pending",
        "in progress": "In progress",
        "in_progress": "In progress",
        "processing": "In progress",
        "progress": "In progress",
        "completed": "Completed",
        "done": "Completed",
        "success": "Completed",
        "partial": "Partial",
        "canceled": "Canceled",
        "cancelled": "Canceled",
        "cancel": "Canceled",
    }

    @staticmethod
    def _get_retry_settings() -> Tuple[int, int]:
        s = load_settings()
        return (
            int(s.get("api_retry_count", 3)),
            int(s.get("api_retry_delay", 2)),
        )

    @classmethod
    def format_error(cls, error: Any) -> str:
        if not error:
            return "Неизвестная ошибка API"
        text = str(error).strip()
        return cls.ERROR_MESSAGES.get(text.lower(), text)

    @classmethod
    def _parse_response(cls, response: requests.Response) -> Optional[Dict[str, Any]]:
        """VexBoost возвращает JSON и при ошибках с HTTP 400 — это НЕ сбой сети."""
        try:
            data = response.json()
        except ValueError:
            logger.warning(
                "%s: не-JSON ответ HTTP %s: %s",
                LOGGER_PREFIX, response.status_code, response.text[:200],
            )
            return None
        if isinstance(data, list):
            return {"services": data}
        if isinstance(data, dict):
            return data
        return {"error": "Некорректный ответ API"}

    @classmethod
    def _normalize_status(cls, status: Any) -> str:
        text = str(status or "Unknown").strip()
        mapped = cls.STATUS_MAP.get(text.lower())
        if mapped:
            return mapped
        if text.lower() in ("pending", "completed", "canceled", "partial"):
            return text.capitalize() if text.lower() != "partial" else "Partial"
        return text or "Unknown"

    @classmethod
    def _request_key(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        from urllib.parse import urlencode

        api_url = get_api_url()
        api_key = get_api_key()
        if not api_key:
            return {"error": "API KEY не задан. /vexboost → API KEY"}
        payload = {"key": api_key, **params}
        action = params.get("action", "")

        # Создание/изменение заказа — только один POST, иначе каждый метод создаёт заказ.
        if action in ("add", "refill", "cancel"):
            response = cls._do_post(api_url, payload)
            if response is None:
                return {"error": "Не удалось связаться с VexBoost: нет ответа"}
            data = cls._parse_response(response)
            if data is None:
                return {"error": f"HTTP {response.status_code}: {response.text[:120]}"}
            if "error" in data:
                data["error"] = cls.format_error(data["error"])
            return data

        retries, delay = cls._get_retry_settings()
        query = urlencode(payload)
        get_url = f"{api_url}?{query}"
        last_error = "Нет ответа от сервера"

        for attempt in range(1, retries + 1):
            for label, response in (
                ("POST", cls._do_post(api_url, payload)),
                ("GET", cls._do_get(get_url)),
                ("GET-params", cls._do_get_params(api_url, payload)),
            ):
                if response is None:
                    continue
                data = cls._parse_response(response)
                if data is not None:
                    if "error" in data:
                        data["error"] = cls.format_error(data["error"])
                    logger.debug(
                        "%s: API %s HTTP %s → %s",
                        LOGGER_PREFIX, label, response.status_code, data,
                    )
                    return data
                last_error = f"HTTP {response.status_code}: {response.text[:120]}"
            if attempt < retries:
                time.sleep(delay * attempt)

        return {"error": f"Не удалось связаться с VexBoost: {last_error}"}

    @classmethod
    def _panel_host(cls) -> str:
        from urllib.parse import urlparse
        return urlparse(get_panel_url()).netloc

    @classmethod
    def _apply_csrf(cls, session: requests.Session) -> None:
        from urllib.parse import unquote

        session.get(f"{get_panel_url()}/api/csrf-cookie", timeout=45)
        xsrf = session.cookies.get("XSRF-TOKEN")
        if xsrf:
            session.headers["X-XSRF-TOKEN"] = unquote(xsrf)

    @classmethod
    def _new_http_session(cls) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            **cls.HEADERS,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Site-Host": cls._panel_host(),
        })
        return session

    @classmethod
    def _session_from_login(cls, force: bool = False) -> Tuple[Optional[requests.Session], str]:
        login = get_vexboost_login()
        password = get_vexboost_password()
        if not login or not password:
            return None, "Логин/пароль не заданы. /vexboost → Логин и Пароль"

        now = time.time()
        ttl = int(load_settings().get("session_ttl", 5400))
        if not force:
            with _session_cache_lock:
                cached = _vexboost_session_cache.get("session")
                if cached is not None and _vexboost_session_cache.get("expires_at", 0) > now:
                    return cached, ""

        session = cls._new_http_session()
        try:
            cls._apply_csrf(session)
            response = session.post(
                f"{get_panel_url()}/api/login",
                json={"login": login, "password": password},
                timeout=45,
            )
        except requests.RequestException as exc:
            return None, f"Ошибка входа: {exc}"

        data = cls._parse_response(response) or {}
        if response.status_code >= 400 or data.get("error"):
            err = cls.format_error(data.get("error", f"HTTP {response.status_code}"))
            _invalidate_vexboost_session()
            return None, err

        cookie_val = session.cookies.get(get_cookie_name())
        if not cookie_val:
            return None, "Вход выполнен, но cookie сессии не получена"

        settings = load_settings()
        with _session_cache_lock:
            _vexboost_session_cache["session"] = session
            _vexboost_session_cache["expires_at"] = now + max(600, ttl)

        logger.info("%s: автовход выполнен", LOGGER_PREFIX)
        return session, ""

    @classmethod
    def _session_from_token(cls) -> Tuple[Optional[requests.Session], str]:
        from urllib.parse import unquote

        token = _normalize_auth_token(get_auth_token())
        if not token:
            return None, "AuthToken не задан. /vexboost → AuthToken"

        session = cls._new_http_session()
        session.cookies.set(get_cookie_name(), unquote(token), domain=cls._panel_host())
        try:
            cls._apply_csrf(session)
        except requests.RequestException as exc:
            return None, f"Ошибка сети (CSRF): {exc}"
        return session, ""

    @classmethod
    def _make_session(cls, force_login: bool = False) -> Tuple[Optional[requests.Session], str]:
        if get_auth_mode() == "login":
            return cls._session_from_login(force=force_login)
        return cls._session_from_token()

    @classmethod
    def _is_mutating_token_request(cls, method: str, path: str) -> bool:
        upper = method.upper()
        normalized = path.lstrip("/").lower()
        if upper not in ("POST", "PUT", "PATCH", "DELETE"):
            return False
        if normalized in ("orders", "api/orders"):
            return True
        return "/orders/" in normalized

    @classmethod
    def _request_token(
        cls,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        retries, delay = cls._get_retry_settings()
        if cls._is_mutating_token_request(method, path):
            retries = 1
        last_error = "Нет ответа от сервера"
        api_path = path.lstrip("/")
        if not api_path.startswith("api/"):
            api_path = f"api/{api_path}"

        force_login = False
        for attempt in range(1, retries + 1):
            session, err = cls._make_session(force_login=force_login)
            if not session:
                return {"error": err}

            url = f"{get_panel_url()}/{api_path}"
            try:
                response = session.request(
                    method.upper(), url,
                    json=json_body, params=params, timeout=45,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < retries:
                    time.sleep(delay * attempt)
                continue

            if response.status_code in (401, 419) and attempt < retries:
                _invalidate_vexboost_session()
                force_login = get_auth_mode() == "login"
                time.sleep(delay)
                continue

            data = cls._parse_response(response)
            if data is None:
                last_error = f"HTTP {response.status_code}: {response.text[:120]}"
                if attempt < retries:
                    time.sleep(delay * attempt)
                continue

            if data.get("error") == "unauthorized" and attempt < retries:
                _invalidate_vexboost_session()
                force_login = get_auth_mode() == "login"
                time.sleep(delay)
                continue

            if "error" in data:
                data["error"] = cls.format_error(data["error"])
            return data

        return {"error": f"Не удалось связаться с VexBoost: {last_error}"}

    @classmethod
    def _request(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        if get_auth_mode() in ("token", "login"):
            return cls._request_token_mode(params)
        return cls._request_key(params)

    @classmethod
    def _request_token_mode(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        action = params.get("action", "")
        if action == "balance":
            return cls._token_balance()
        if action == "services":
            data = cls._request_token("GET", "services")
            if isinstance(data.get("services"), list):
                return data
            if isinstance(data, list):
                return {"services": data}
            return data
        if action == "add":
            return cls._token_create_order(
                int(params["service"]), str(params["link"]), int(params["quantity"]),
            )
        if action == "status":
            return cls._token_order_status(int(params["order"]))
        if action == "refill":
            return cls._token_refill(int(params["order"]))
        if action == "cancel":
            return cls._token_cancel(int(params["order"]))
        return {"error": f"Неизвестное действие: {action}"}

    @classmethod
    def _token_balance(cls) -> Dict[str, Any]:
        data = cls._request_token("GET", "user")
        if "error" in data:
            return data
        for key in ("balance", "wallet", "funds", "amount"):
            if key in data and data[key] is not None:
                return {
                    "balance": data[key],
                    "currency": data.get("currency") or data.get("currency_code") or "RUB",
                }
        user = data.get("user")
        if isinstance(user, dict):
            for key in ("balance", "wallet", "funds"):
                if key in user and user[key] is not None:
                    return {
                        "balance": user[key],
                        "currency": user.get("currency") or "RUB",
                    }
        return {"error": "Не удалось получить баланс из /api/user"}

    @classmethod
    def _extract_created_order_id(cls, data: Dict[str, Any]) -> Optional[int]:
        if not isinstance(data, dict):
            return None
        for key in ("id", "order_id", "orderId"):
            value = data.get(key)
            if value is not None and str(value).isdigit():
                return int(value)
        order = data.get("order")
        if isinstance(order, dict):
            nested = cls._extract_created_order_id(order)
            if nested is not None:
                return nested
        if isinstance(order, (int, str)) and str(order).isdigit():
            return int(order)
        nested_data = data.get("data")
        if isinstance(nested_data, dict):
            return cls._extract_created_order_id(nested_data)
        return None

    @classmethod
    def _token_create_order(cls, service_id: int, link: str, quantity: int) -> Dict[str, Any]:
        body = {"service_id": service_id, "link": link, "quantity": quantity}
        data = cls._request_token("POST", "orders", json_body=body)
        if data.get("error"):
            return data
        order_id = cls._extract_created_order_id(data)
        if order_id is not None:
            logger.info(
                "%s: SMM-заказ создан одним запросом id=%s service=%s qty=%s",
                LOGGER_PREFIX, order_id, service_id, quantity,
            )
            return {"order": order_id}
        return {"error": "Заказ не создан: ID не найден в ответе API"}

    @classmethod
    def _token_order_status(cls, order_id: int) -> Dict[str, Any]:
        data = cls._request_token("GET", f"orders/{order_id}")
        if "error" in data:
            return data
        order = data.get("order") if isinstance(data.get("order"), dict) else data
        status = cls._normalize_status(order.get("status"))
        remains = order.get("remains", order.get("remainder", order.get("rest", 0)))
        charge = order.get("charge", order.get("cost", order.get("price", 0)))
        currency = order.get("currency", order.get("currency_code", "RUB"))
        return {
            "status": status,
            "remains": remains,
            "charge": charge,
            "currency": currency,
        }

    @classmethod
    def _token_refill(cls, order_id: int) -> Dict[str, Any]:
        data = cls._request_token("POST", f"orders/{order_id}/refill")
        if "error" in data:
            return data
        refill_id = data.get("refill") or data.get("id")
        return {"refill": refill_id or True}

    @classmethod
    def _token_cancel(cls, order_id: int) -> Dict[str, Any]:
        data = cls._request_token("DELETE", f"orders/{order_id}")
        if "error" in data:
            return data
        return {"cancel": data.get("cancel", True)}

    @classmethod
    def _do_post(cls, api_url: str, payload: Dict[str, Any]) -> Optional[requests.Response]:
        try:
            return requests.post(
                api_url, data=payload, timeout=45, headers=cls.HEADERS,
            )
        except requests.RequestException as exc:
            logger.warning("%s: POST ошибка сети: %s", LOGGER_PREFIX, exc)
            return None

    @classmethod
    def _do_get(cls, url: str) -> Optional[requests.Response]:
        try:
            return requests.get(url, timeout=45, headers=cls.HEADERS)
        except requests.RequestException as exc:
            logger.warning("%s: GET ошибка сети: %s", LOGGER_PREFIX, exc)
            return None

    @classmethod
    def _do_get_params(cls, api_url: str, payload: Dict[str, Any]) -> Optional[requests.Response]:
        try:
            return requests.get(
                api_url, params=payload, timeout=45, headers=cls.HEADERS,
            )
        except requests.RequestException as exc:
            logger.warning("%s: GET-params ошибка сети: %s", LOGGER_PREFIX, exc)
            return None

    @classmethod
    def get_balance(cls) -> Optional[Tuple[float, str]]:
        data = cls._request({"action": "balance"})
        if "error" in data:
            logger.warning("%s: баланс — %s", LOGGER_PREFIX, data["error"])
            return None
        if "balance" not in data:
            return None
        match = re.search(r"[\d.]+", str(data["balance"]))
        if not match:
            return None
        return float(match.group()), data.get("currency", "RUB")

    @classmethod
    def get_balance_error(cls) -> str:
        data = cls._request({"action": "balance"})
        if "balance" in data:
            return ""
        return data.get("error", "Не удалось получить баланс")

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
    return _format_buyer_template("completion_message", order_id=funpay_order_id)


def _is_private_telegram_link(link: str) -> bool:
    return "t.me" in link and ("/c/" in link or "+" in link)


# ─────────────────────────────────────────────────────────────────────────────
# Обработка нового заказа FunPay
# ─────────────────────────────────────────────────────────────────────────────

def bind_to_new_order(c: "Cardinal", e: NewOrderEvent) -> None:
    try:
        if not is_api_configured():
            logger.warning("%s: VexBoost не настроен (URL/AuthToken или API KEY)", LOGGER_PREFIX)
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

        fp_id = str(order_id)
        if _fp_order_already_submitted(fp_id):
            logger.info("%s: заказ FP#%s уже отправлен в SMM", LOGGER_PREFIX, order_id)
            return

        pay_orders = load_payorders()
        if any(str(o.get("OrderID")) == fp_id for o in pay_orders):
            logger.info("%s: заказ FP#%s уже ожидает ссылку", LOGGER_PREFIX, order_id)
            return
        pay_orders.append(order_entry)
        save_payorders(pay_orders)

        StatisticsManager.record_created(service_id, safe_float(e.order.price))

        settings = load_settings()
        if settings.get("set_alert_smmbalance_new"):
            send_balance_notification(c)

        if chat_id:
            send_fp(c, chat_id, _format_buyer_template("welcome_message", order_id=order_id))

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
        send_fp(c, order["chat_id"], _format_buyer_template("private_telegram_message"))
        return

    order["url"] = link
    display_link = link.replace("https://", "").replace("http://", "")
    send_fp(
        c, order["chat_id"],
        _format_buyer_template(
            "confirmation_message",
            lot=order["Order"],
            amount=order["Amount"],
            link=display_link,
        ),
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
        fp_id = str(order.get("OrderID", ""))
        if not _try_lock_fp_order(fp_id):
            logger.warning(
                "%s: дубликат подтверждения заблокирован FP#%s buyer=%s",
                LOGGER_PREFIX, fp_id, buyer,
            )
            return
        send_fp(c, order["chat_id"], _format_buyer_template("creating_order_message"))
        try:
            _create_vexboost_order(c, order)
        except Exception:
            _unlock_fp_order(fp_id)
            raise
    elif action == "-":
        send_fp(c, chat_id, _format_buyer_template("order_cancelled_message"))
        _remove_pay_order(order["buyer"])
        _refund_order(c, order["OrderID"])


def _create_vexboost_order(c: "Cardinal", order: Dict[str, Any]) -> None:
    settings = load_settings()
    fp_id = str(order.get("OrderID", ""))
    existing_smm_id = get_submitted_smm_id(fp_id)
    if existing_smm_id is not None:
        logger.warning(
            "%s: заказ FP#%s уже создан в SMM как #%s",
            LOGGER_PREFIX, fp_id, existing_smm_id,
        )
        _unlock_fp_order(fp_id)
        return
    if _fp_order_exists_in_active(fp_id):
        logger.warning("%s: заказ FP#%s уже в активных", LOGGER_PREFIX, fp_id)
        _unlock_fp_order(fp_id)
        return

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
        mark_submitted_order(fp_id, smm_id)
        _remove_pay_order(order["buyer"])
        _unlock_fp_order(fp_id)

        status_data = VexBoostAPI.get_order_status(smm_id)
        cost = safe_float(status_data.get("charge", 0)) if status_data else 0.0
        smm_cur = status_data.get("currency", "RUB") if status_data else "RUB"

        send_order_created_notification(c, order, smm_id, cost, smm_cur)

        send_fp(
            c, order["chat_id"],
            _format_buyer_template(
                "order_created_message",
                smm_id=smm_id,
                funpay_id=order["OrderID"],
            ),
        )
        logger.info("%s: VB#%s создан для FP#%s", LOGGER_PREFIX, smm_id, order["OrderID"])
    else:
        error_text = str(result)
        _unlock_fp_order(fp_id)
        send_fp(
            c, order["chat_id"],
            _format_buyer_template("error_message", error=_buyer_error_message(error_text)),
        )
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
        send_fp(c, cid, _format_buyer_template("send_link_first_message"))
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
        if getattr(c, "old_mode_enabled", False):
            return
        msg = e.message
        text = _get_message_text(msg)
        if not _should_process_message(msg.chat_id, text, getattr(msg, "id", None)):
            return
        _process_buyer_message(
            c,
            text,
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
        if not _should_process_message(chat.id, message_text):
            return
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
    send_fp(c, chat_id, _format_buyer_template("pending_hint_message"))


def _cmd_status(c: "Cardinal", chat_id: Any, message_text: str) -> None:
    parts = message_text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        send_fp(c, chat_id, _format_buyer_template("status_usage_message"))
        return
    smm_id = int(parts[1])
    status = VexBoostAPI.get_order_status(smm_id)
    if not status:
        send_fp(c, chat_id, _format_buyer_template("status_error_message"))
        return
    start_count = status.get("start_count", 0)
    display_start = "*" if start_count == 0 else str(start_count)
    send_fp(
        c, chat_id,
        _format_buyer_template(
            "status_message",
            smm_id=smm_id,
            status=_buyer_status_label(status.get("status")),
            start_count=display_start,
            remains=status.get("remains", "—"),
        ),
    )


def _cmd_refill(c: "Cardinal", chat_id: Any, message_text: str) -> None:
    parts = message_text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        send_fp(c, chat_id, _format_buyer_template("refill_usage_message"))
        return
    result = VexBoostAPI.refill_order(int(parts[1]))
    if result is not None:
        send_fp(c, chat_id, _format_buyer_template("refill_success_message"))
    else:
        send_fp(c, chat_id, _format_buyer_template("refill_error_message"))


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
    if not is_api_configured():
        return
    active = load_active_orders()
    if not active:
        return

    settings = load_settings()
    mode = get_auth_mode()
    api_url = get_panel_url() if mode in ("token", "login") else get_api_url()
    api_key = get_auth_token() if mode in ("token", "login") else get_api_key()
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
            _format_buyer_template("order_canceled_message", funpay_id=funpay_id),
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
                _format_buyer_template(
                    "partial_paused_message",
                    funpay_id=funpay_id,
                    remains=partial_amount,
                ),
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
                    _format_buyer_template(
                        "partial_continued_message",
                        funpay_id=funpay_id,
                        partial_amount=partial_amount,
                    ),
                )
    except Exception as exc:
        logger.error("%s: ошибка пересоздания partial: %s", LOGGER_PREFIX, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Встроенный гайд (/vexboost → 📖 Гайд)
# ─────────────────────────────────────────────────────────────────────────────

GUIDE_SECTIONS: Dict[str, Tuple[str, str]] = {
    "start": (
        "🚀 Быстрый старт",
        (
            "<b>1. Установка</b>\n"
            "Скопируйте <code>vexboost_autosmm.py</code> в папку <code>plugins</code> "
            "и перезапустите Cardinal.\n\n"
            "<b>2. Настройка</b>\n"
            "• /vexboost → URL + Логин + Пароль\n"
            "• /vb_balance — проверка связи\n\n"
            "<b>3. Лот FunPay</b>\n"
            "В описании:\n"
            "<code>ID: 1000</code>\n"
            "<code>#Quan: 1</code> — по желанию\n\n"
            "<b>4. Тест</b>\n"
            "Оплата → ссылка → <b>+</b> → 1 заказ в панели.\n\n"
            "✅ Чеклист: Cardinal работает · curl vexboost.ru OK · баланс виден"
        ),
    ),
    "auth": (
        "🔐 Авторизация",
        (
            "<b>Логин + пароль</b> — рекомендуется, 24/7\n"
            "URL → Логин → Пароль → /vb_balance\n\n"
            "<b>AuthToken</b> — временно (~2 ч)\n"
            "Cookie-Editor → <code>socpanel_session</code> → Value\n\n"
            "<b>API KEY</b>\n"
            "API URL + KEY из кабинета панели\n\n"
            "Смена режима: кнопка <b>Режим</b> в главном меню."
        ),
    ),
    "lots": (
        "📦 Лоты FunPay",
        (
            "В <b>описании лота</b> укажите ID услуги из SMM-панели:\n\n"
            "<code>ID: 1000</code>\n\n"
            "Множитель количества (опционально):\n"
            "<code>#Quan: 1000</code>\n\n"
            "Формула: <i>кол-во в панели = шт. лота × #Quan</i>\n"
            "Без #Quan: кол-во = шт. лота.\n\n"
            "Разные лоты — разные <code>ID:</code> в описании."
        ),
    ),
    "flow": (
        "🔄 Процесс заказа",
        (
            "<b>Покупатель:</b>\n"
            "1️⃣ Оплата лота\n"
            "2️⃣ Ссылка на канал/пост\n"
            "3️⃣ Проверка деталей\n"
            "4️⃣ <b>+</b> подтвердить / <b>-</b> отмена\n"
            "5️⃣ Получает ID заказа SMM\n"
            "6️⃣ После выполнения — ссылка на FunPay\n\n"
            "<b>Команды в чате:</b>\n"
            "<code>#статус ID</code> · <code>#рефилл ID</code>\n\n"
            "Название панели покупателю <b>не показывается</b>."
        ),
    ),
    "panel": (
        "🎛 Кнопки панели",
        (
            "<b>Управление</b>\n"
            "📊 Статистика · 💰 Баланс\n"
            "📝 Ожидают ссылку · 📋 Активные\n"
            "📜 История · 🏆 Топ услуг\n\n"
            "<b>Аналитика</b>\n"
            "💎 Прибыль · 📈 График · 📊 Детально\n\n"
            "<b>Сервис</b>\n"
            "🏥 Диагностика — проверка API\n"
            "📝 Шаблоны — тексты для покупателей\n"
            "🛠 Настройки — автовозврат, уведомления\n\n"
            "<b>Команды:</b> /vexboost · /vb_stats · /vb_balance"
        ),
    ),
    "templates": (
        "📝 Шаблоны",
        (
            "📝 Шаблоны → выберите текст → отправьте новый.\n\n"
            "<b>Переменные:</b>\n"
            "<code>{smm_id}</code> — ID в панели\n"
            "<code>{order_id}</code> / <code>{funpay_id}</code>\n"
            "<code>{lot}</code> · <code>{amount}</code> · <code>{link}</code>\n"
            "<code>{error}</code> · <code>{status}</code> · <code>{remains}</code>\n\n"
            "Сброс: <code>/default</code> или кнопка «Сбросить»."
        ),
    ),
    "settings": (
        "⚙️ Настройки",
        (
            "<b>Автовозврат</b> — при ошибке / отмене в панели\n\n"
            "<b>Уведомления TG</b> — новый заказ, ошибка, выполнение, баланс\n\n"
            "<b>Partial</b> — автодозаказ остатка (выкл. по умолчанию)\n\n"
            "<b>Закрытые TG</b> — разрешить t.me/+ и t.me/c/ ссылки\n\n"
            "Переключатели: 🟢 вкл · 🔴 выкл"
        ),
    ),
    "fix": (
        "🔧 Решение проблем",
        (
            "<b>Нет баланса / API</b>\n"
            "🏥 Диагностика → проверьте логин/пароль\n"
            "<code>curl -I https://vexboost.ru</code> с сервера\n\n"
            "<b>FunPay timeout</b>\n"
            "Проблема сети VPS, не плагина. Нужен другой хостинг.\n\n"
            "<b>Заказ не создаётся</b>\n"
            "Баланс · ID в лоте · ссылка https:// · логи Cardinal\n\n"
            "<b>Дубли заказов</b>\n"
            "Обновите до v2.4.3+. Старые — отмените вручную в панели."
        ),
    ),
}


def _guide_menu_text() -> str:
    return (
        f"📖 <b>Гайд {NAME} v{VERSION}</b>\n\n"
        f"Автор: {CREDITS}\n\n"
        f"Автонакрутка SMM через FunPay Cardinal.\n"
        f"Выберите раздел:"
    )


def _guide_section_text(section: str) -> str:
    title, body = GUIDE_SECTIONS.get(section, ("", "Раздел не найден."))
    return f"📖 <b>{title}</b>\n\n{body}"


def _guide_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    labels = (
        ("🚀 Старт", "start"),
        ("🔐 Вход", "auth"),
        ("📦 Лоты", "lots"),
        ("🔄 Заказ", "flow"),
        ("🎛 Панель", "panel"),
        ("📝 Шаблоны", "templates"),
        ("⚙️ Настройки", "settings"),
        ("🔧 Проблемы", "fix"),
    )
    for i in range(0, len(labels), 2):
        left = InlineKeyboardButton(labels[i][0], callback_data=f"vb_guide_{labels[i][1]}")
        if i + 1 < len(labels):
            right = InlineKeyboardButton(
                labels[i + 1][0], callback_data=f"vb_guide_{labels[i + 1][1]}",
            )
            kb.row(left, right)
        else:
            kb.row(left)
    kb.row(InlineKeyboardButton("⬅️ В меню", callback_data="vb_back_main"))
    return kb


def _guide_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("⬅️ К разделам гайда", callback_data="vb_guide"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram-панель управления (/vexboost)
# ─────────────────────────────────────────────────────────────────────────────

def _main_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    mode = get_auth_mode()
    if mode == "login":
        kb.row(
            InlineKeyboardButton("🔗 URL", callback_data="vb_set_panel_url"),
            InlineKeyboardButton("👤 Логин", callback_data="vb_set_login"),
        )
        kb.row(
            InlineKeyboardButton("🔒 Пароль", callback_data="vb_set_password"),
            InlineKeyboardButton("✅ Режим: Логин", callback_data="vb_auth_mode_menu"),
        )
    elif mode == "token":
        kb.row(
            InlineKeyboardButton("🔗 URL", callback_data="vb_set_panel_url"),
            InlineKeyboardButton("🔑 AuthToken", callback_data="vb_set_token"),
        )
        kb.row(
            InlineKeyboardButton("🍪 Режим: AuthToken", callback_data="vb_auth_mode_menu"),
        )
    else:
        kb.row(
            InlineKeyboardButton("🔗 API URL", callback_data="vb_set_url"),
            InlineKeyboardButton("🔐 API KEY", callback_data="vb_set_key"),
        )
        kb.row(
            InlineKeyboardButton("🔑 Режим: API KEY", callback_data="vb_auth_mode_menu"),
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
        InlineKeyboardButton("📝 Шаблоны", callback_data="vb_templates_menu"),
        InlineKeyboardButton("🛠 Настройки", callback_data="vb_settings_menu"),
    )
    kb.row(
        InlineKeyboardButton("📖 Гайд", callback_data="vb_guide"),
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
    refund_err = "🟢" if settings.get("auto_refund_on_error") else "🔴"
    refund_cancel = "🟢" if settings.get("auto_refund_on_cancel") else "🔴"
    mode = get_auth_mode()
    if mode == "login":
        login_display = _mask_credential(get_vexboost_login())
        pwd_set = "задан" if get_vexboost_password() else "не задан"
        auth_block = (
            f"👤 Режим: <b>Логин + пароль</b> (автовход)\n"
            f"🔗 URL: <code>{get_panel_url()}</code>\n"
            f"👤 Логин: <code>{login_display or 'не задан'}</code>\n"
            f"🔒 Пароль: <code>{pwd_set}</code>\n"
        )
    elif mode == "token":
        token = get_auth_token()
        token_display = _mask_credential(token, visible=4)
        auth_block = (
            f"🍪 Режим: <b>AuthToken</b> (cookie)\n"
            f"🔗 URL: <code>{get_panel_url()}</code>\n"
            f"🔑 AuthToken: <code>{token_display}</code>\n"
            f"🍪 Cookie: <code>{get_cookie_name()}</code>\n"
        )
    else:
        key = get_api_key()
        key_display = ("***" + key[-4:]) if len(key) > 4 else "не задан"
        auth_block = (
            f"🔐 Режим: <b>API KEY</b>\n"
            f"🔗 API: <code>{get_api_url()}</code>\n"
            f"🔐 KEY: <code>{key_display}</code>\n"
        )
    return (
        f"⚙️ <b>{NAME} v{VERSION}</b>\n\n"
        f"{auth_block}"
        f"🔄 Автовозврат (ошибка): {refund_err}\n"
        f"🔄 Автовозврат (отмена): {refund_cancel}\n"
        f"⏱ Интервал проверки: <b>{settings.get('status_check_interval', 60)}</b> сек.\n"
        f"💼 Комиссия: <b>{settings.get('commission_percent', 6)}%</b>\n\n"
        f"📋 В описании лота:\n"
        f"<code>ID: 1000</code>\n"
        f"<code>#Quan: 1</code> (опционально)"
    )


def _help_text() -> str:
    return (
        f"ℹ️ <b>Справка {NAME}</b>\n\n"
        f"<b>Логин + пароль (рекомендуется, 24/7):</b>\n"
        f"1. /vexboost → URL панели\n"
        f"2. Логин — email или логин аккаунта панели\n"
        f"3. Пароль — от аккаунта VexBoost\n"
        f"4. /vb_balance — проверка\n\n"
        f"<b>AuthToken (временно, ~2 ч):</b>\n"
        f"Cookie-Editor → <code>socpanel_session</code> → Value\n\n"
        f"<b>Настройка лотов:</b>\n"
        f"В описании лота укажите ID услуги SMM-панели:\n"
        f"<code>ID: 1000</code>\n"
        f"<code>#Quan: 1</code> — множитель количества\n\n"
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
        f"/vb_balance — баланс VexBoost\n\n"
        f"<b>Подробная инструкция:</b>\n"
        f"/vexboost → 📖 Гайд"
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
            err = VexBoostAPI.get_balance_error()
            text = f"🔴 <b>VexBoost:</b> {err or 'Проверьте API KEY в /vexboost'}"
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

            elif call.data == "vb_set_panel_url":
                result = bot.send_message(
                    chat_id,
                    "Введите URL SMM-панели:\n(например https://example-panel.com)",
                )
                tg.set_state(chat_id=chat_id, message_id=result.id,
                             user_id=call.from_user.id, state="vb_panel_url")
                bot.answer_callback_query(call.id)

            elif call.data == "vb_set_token":
                result = bot.send_message(
                    chat_id,
                    "Введите AuthToken из Cookie-Editor:\n"
                    "cookie <code>socpanel_session</code> → поле Value\n\n"
                    "Можно вставить целиком: socpanel_session=ЗНАЧЕНИЕ",
                    parse_mode="HTML",
                )
                tg.set_state(chat_id=chat_id, message_id=result.id,
                             user_id=call.from_user.id, state="vb_auth_token")
                bot.answer_callback_query(call.id)

            elif call.data == "vb_auth_mode_menu":
                mode_kb = InlineKeyboardMarkup(row_width=1)
                mode_kb.add(
                    InlineKeyboardButton("👤 Логин + пароль", callback_data="vb_auth_mode_login"),
                    InlineKeyboardButton("🍪 AuthToken (cookie)", callback_data="vb_auth_mode_token"),
                    InlineKeyboardButton("🔑 API KEY", callback_data="vb_auth_mode_key"),
                    InlineKeyboardButton("⬅️ Назад", callback_data="vb_back_main"),
                )
                bot.edit_message_text(
                    "Выберите способ авторизации VexBoost:",
                    chat_id, msg_id, reply_markup=mode_kb,
                )
                bot.answer_callback_query(call.id)

            elif call.data == "vb_auth_mode_login":
                settings["auth_mode"] = "login"
                _invalidate_vexboost_session()
                save_settings(settings)
                bot.edit_message_text(
                    _settings_summary(settings), chat_id, msg_id,
                    reply_markup=_main_keyboard(), parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "Режим: Логин + пароль")

            elif call.data == "vb_auth_mode_token":
                settings["auth_mode"] = "token"
                _invalidate_vexboost_session()
                save_settings(settings)
                bot.edit_message_text(
                    _settings_summary(settings), chat_id, msg_id,
                    reply_markup=_main_keyboard(), parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "Режим: AuthToken")

            elif call.data == "vb_auth_mode_key":
                settings["auth_mode"] = "api_key"
                _invalidate_vexboost_session()
                save_settings(settings)
                bot.edit_message_text(
                    _settings_summary(settings), chat_id, msg_id,
                    reply_markup=_main_keyboard(), parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "Режим: API KEY")

            elif call.data == "vb_set_login":
                result = bot.send_message(
                    chat_id,
                    "Введите логин VexBoost (email или логин с сайта):",
                )
                tg.set_state(chat_id=chat_id, message_id=result.id,
                             user_id=call.from_user.id, state="vb_panel_login")
                bot.answer_callback_query(call.id)

            elif call.data == "vb_set_password":
                result = bot.send_message(chat_id, "Введите пароль от аккаунта VexBoost:")
                tg.set_state(chat_id=chat_id, message_id=result.id,
                             user_id=call.from_user.id, state="vb_panel_password")
                bot.answer_callback_query(call.id)

            elif call.data == "vb_set_url":
                result = bot.send_message(
                    chat_id, "Введите API URL:\n(например https://panel.example.com/api/v2)",
                )
                tg.set_state(chat_id=chat_id, message_id=result.id,
                             user_id=call.from_user.id, state="vb_api_url")
                bot.answer_callback_query(call.id)

            elif call.data == "vb_set_key":
                result = bot.send_message(chat_id, "Введите API KEY из личного кабинета панели:")
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
                    err = VexBoostAPI.get_balance_error() or "Ошибка API"
                    bot.answer_callback_query(call.id, err[:200], show_alert=True)

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

            elif call.data == "vb_templates_menu":
                bot.edit_message_text(
                    _template_menu_text(), chat_id, msg_id,
                    reply_markup=_templates_keyboard(), parse_mode="HTML",
                )
                bot.answer_callback_query(call.id)

            elif call.data.startswith("vb_tpl_edit_"):
                template_key = call.data.replace("vb_tpl_edit_", "")
                if template_key in MESSAGE_TEMPLATE_LABELS:
                    result = bot.send_message(
                        chat_id,
                        _template_edit_prompt(template_key),
                        parse_mode="HTML",
                        reply_markup=_template_edit_keyboard(template_key),
                    )
                    tg.set_state(
                        chat_id=chat_id, message_id=result.id,
                        user_id=call.from_user.id, state=f"vb_tpl_{template_key}",
                    )
                bot.answer_callback_query(call.id)

            elif call.data.startswith("vb_tpl_reset_"):
                template_key = call.data.replace("vb_tpl_reset_", "")
                if template_key in MESSAGE_TEMPLATE_LABELS:
                    settings[template_key] = DEFAULT_SETTINGS[template_key]
                    save_settings(settings)
                    label = MESSAGE_TEMPLATE_LABELS[template_key][0]
                    bot.answer_callback_query(call.id, f"{label}: сброшено")
                    try:
                        bot.edit_message_text(
                            _template_edit_prompt(template_key), chat_id, msg_id,
                            parse_mode="HTML",
                            reply_markup=_template_edit_keyboard(template_key),
                        )
                    except Exception:
                        pass
                else:
                    bot.answer_callback_query(call.id)

            elif call.data in VB_EXTRA_CALLBACKS:
                VB_EXTRA_CALLBACKS[call.data](cardinal, bot, chat_id, msg_id)

            elif call.data == "vb_guide":
                bot.edit_message_text(
                    _guide_menu_text(), chat_id, msg_id,
                    reply_markup=_guide_menu_keyboard(), parse_mode="HTML",
                )
                bot.answer_callback_query(call.id)

            elif call.data.startswith("vb_guide_"):
                section = call.data.replace("vb_guide_", "")
                if section in GUIDE_SECTIONS:
                    bot.edit_message_text(
                        _guide_section_text(section), chat_id, msg_id,
                        reply_markup=_guide_section_keyboard(), parse_mode="HTML",
                    )
                bot.answer_callback_query(call.id)

            elif call.data == "vb_help":
                bot.edit_message_text(
                    _help_text(), chat_id, msg_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("📖 Полный гайд", callback_data="vb_guide"),
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

        if state == "vb_panel_url":
            settings["panel_url"] = message.text.strip().rstrip("/")
            save_settings(settings)
            bot.reply_to(
                message, f"✅ URL: <code>{settings['panel_url']}</code>", parse_mode="HTML",
            )
        elif state == "vb_panel_login":
            settings["vexboost_login"] = message.text.strip()
            settings["auth_mode"] = "login"
            _invalidate_vexboost_session()
            save_settings(settings)
            bot.reply_to(message, "✅ Логин сохранён.")
        elif state == "vb_panel_password":
            settings["vexboost_password"] = message.text
            settings["auth_mode"] = "login"
            _invalidate_vexboost_session()
            save_settings(settings)
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            bot.reply_to(message, "✅ Пароль сохранён. Проверьте: /vb_balance")
        elif state == "vb_auth_token":
            settings["auth_token"] = _normalize_auth_token(message.text)
            settings["auth_mode"] = "token"
            _invalidate_vexboost_session()
            save_settings(settings)
            bot.reply_to(message, "✅ AuthToken сохранён. Проверьте: /vb_balance")
        elif state == "vb_api_url":
            settings["api_url"] = message.text.strip().rstrip("/")
            save_settings(settings)
            bot.reply_to(message, f"✅ API URL: <code>{settings['api_url']}</code>", parse_mode="HTML")
        elif state == "vb_api_key":
            settings["api_key"] = message.text.strip()
            settings["auth_mode"] = "api_key"
            _invalidate_vexboost_session()
            save_settings(settings)
            bot.reply_to(message, "✅ API KEY сохранён.")
        elif state.startswith("vb_tpl_"):
            template_key = state.replace("vb_tpl_", "")
            if template_key in MESSAGE_TEMPLATE_LABELS:
                label = MESSAGE_TEMPLATE_LABELS[template_key][0]
                if message.text.strip() == "/default":
                    settings[template_key] = DEFAULT_SETTINGS[template_key]
                    bot.reply_to(message, f"✅ Шаблон «{label}» сброшен по умолчанию.")
                else:
                    settings[template_key] = message.text
                    bot.reply_to(
                        message,
                        f"✅ Шаблон «{label}» сохранён.\n\n"
                        f"<pre>{html.escape(message.text)}</pre>",
                        parse_mode="HTML",
                    )
                save_settings(settings)
        tg.clear_state(message.chat.id, message.from_user.id)

    def _has_text_input_state(message):
        base_states = (
            "vb_panel_url", "vb_panel_login", "vb_panel_password",
            "vb_auth_token", "vb_api_url", "vb_api_key",
        )
        if any(tg.check_state(message.chat.id, message.from_user.id, s) for s in base_states):
            return True
        state_data = tg.get_state(message.chat.id, message.from_user.id)
        state = (state_data or {}).get("state", "")
        if state.startswith("vb_tpl_"):
            return state.replace("vb_tpl_", "") in MESSAGE_TEMPLATE_LABELS
        return False

    tg.cbq_handler(handle_callback, lambda c: c.data.startswith("vb_"))
    tg.msg_handler(handle_text_input, func=_has_text_input_state)
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
        send_fp(c, order["chat_id"], _format_buyer_template("invalid_link_message", error=err))
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
        if not is_api_configured():
            mode = get_auth_mode()
            if mode == "login":
                return False, "Задайте URL, логин и пароль (/vexboost)"
            if mode == "token":
                return False, "Задайте URL и AuthToken (/vexboost)"
            return False, "API KEY не задан (/vexboost)"
        balance = VexBoostAPI.get_balance()
        if balance:
            labels = {"login": "Логин", "token": "AuthToken", "api_key": "API KEY"}
            return True, f"{labels.get(get_auth_mode(), 'API')} OK, баланс: {balance[0]:.2f} {balance[1]}"
        err = VexBoostAPI.get_balance_error()
        return False, err or "API не отвечает"

    @staticmethod
    def check_settings() -> Tuple[bool, str]:
        settings = load_settings()
        mode = get_auth_mode()
        if mode == "login":
            if not get_panel_url():
                return False, "Не задан URL"
            if not get_vexboost_login():
                return False, "Не задан логин"
            if not get_vexboost_password():
                return False, "Не задан пароль"
            return True, "Логин-режим настроен"
        if mode == "token":
            if not get_panel_url():
                return False, "Не задан URL"
            if not get_auth_token():
                return False, "Не задан AuthToken"
            return True, "AuthToken-режим настроен"
        if not get_api_key():
            return False, "Не задан API KEY"
        return True, "API KEY-режим настроен"

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

# ─────────────────────────────────────────────────────────────────────────────
# Автор плагина: @xei1y
# ─────────────────────────────────────────────────────────────────────────────
