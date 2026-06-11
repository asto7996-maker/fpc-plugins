from __future__ import annotations

# === ОБЯЗАТЕЛЬНЫЕ ПОЛЯ FunPay Cardinal ===
NAME = "MM2 Auto Delivery"
VERSION = "1.0.0"
DESCRIPTION = "Автоматическая выдача предметов Murder Mystery 2 после заказов FunPay"
CREDITS = "Cursor AI"
UUID = "f4d0f0f1-8f3f-4cb2-bd9b-3d7c1b6f6a11"
SETTINGS_PAGE = False
BIND_TO_DELETE = None
# === КОНЕЦ ОБЯЗАТЕЛЬНЫХ ПОЛЕЙ ===

import asyncio
import contextlib
import dataclasses
import enum
import html
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from FunPayAPI.types import MessageTypes
    from FunPayAPI.updater.events import LastChatMessageChangedEvent, NewMessageEvent, NewOrderEvent
except Exception:  # pragma: no cover - Cardinal provides these at runtime.
    MessageTypes = None
    LastChatMessageChangedEvent = Any  # type: ignore
    NewMessageEvent = Any  # type: ignore
    NewOrderEvent = Any  # type: ignore

try:
    from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
except Exception:  # pragma: no cover - telebot is provided by Cardinal.
    InlineKeyboardButton = None  # type: ignore
    InlineKeyboardMarkup = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Логирование и базовые пути
# ─────────────────────────────────────────────────────────────────────────────

LOGGER_NAME = "FPC.MM2AutoDelivery"
logger = logging.getLogger(LOGGER_NAME)
LOGGER_PREFIX = "MM2AutoDelivery"

STORAGE_DIR = os.path.join("storage", "plugins", UUID)
SETTINGS_FILE = os.path.join(STORAGE_DIR, "settings.json")
DB_FILE = os.path.join(STORAGE_DIR, "mm2_delivery.sqlite3")
LOG_FILE = os.path.join(STORAGE_DIR, "mm2_delivery.log")


def utc_now() -> str:
    """Возвращает ISO-время без микросекунд для SQLite/JSON."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_storage_dir() -> None:
    os.makedirs(STORAGE_DIR, exist_ok=True)


def setup_file_logging() -> None:
    """Подключает файловый лог один раз, не дублируя handler при перезагрузке."""
    ensure_storage_dir()
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == os.path.abspath(LOG_FILE):
            return
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(file_handler)


setup_file_logging()


# ─────────────────────────────────────────────────────────────────────────────
# Тексты и настройки
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "roblox_security_cookie": "",
    "roblox_bot_user_id": 0,
    "roblox_bot_username": "",
    "roblox_profile_url": "",
    "roblox_vip_server_url": "",
    "roblox_place_id": 142823291,
    "expected_game_instance_id": "",
    "use_public_server_if_no_vip": False,
    "delivery_timeout_minutes": 12,
    "presence_poll_seconds": 15,
    "friend_accept_timeout_minutes": 6,
    "trade_timeout_seconds": 60,
    "trade_retry_count": 3,
    "trade_retry_delay_seconds": 10,
    "message_antispam_seconds": 45,
    "roblox_http_timeout_seconds": 20,
    "roblox_http_retries": 3,
    "roblox_http_retry_delay_seconds": 2,
    "admin_funpay_chat_id": "",
    "admin_notifications_to_telegram": True,
    "admin_notifications_to_funpay": False,
    "telegram_mirror_funpay_messages": True,
    "seller_funpay_usernames": [],
    "ignore_orders_where_buyer_is_me": True,
    "require_mm2_category_match": True,
    "mm2_category_keywords": [
        "mm2",
        "murder mystery",
        "murder mystery 2",
        "murder mystery x",
        "roblox mm2",
    ],
    "pause_on_auth_error": True,
    "auto_start_delivery_after_friend_request": True,
    "delivery_queue_workers": 1,
    "inventory_sync": {
        "enabled": True,
        "auto_create_lot_mapping": True,
        "lot_id_start": 1001,
        "source_order": ["browser", "roblox_collectibles"],
        "manual_import_separator": "\n",
        "browser_scan_url": "",
        "browser_scan_wait_seconds": 15,
        "selectors": {
            "inventory_open_button": "",
            "inventory_items": "[data-mm2='inventory-item']",
            "inventory_item_name": "[data-mm2='inventory-item-name']",
            "inventory_item_quantity": "[data-mm2='inventory-item-quantity']",
            "inventory_next_page": "",
        },
        "roblox_collectibles_enabled": True,
    },
    "buyer_server_join_enabled": True,
    "buyer_join_keywords": [
        "/joinme",
        "/join",
        "join me",
        "зайди ко мне",
        "иди ко мне",
        "ко мне",
    ],
    "lot_id_patterns": [
        r"(?:MM2|Murder\s*Mystery\s*2)?\s*(?:ID|Lot|Лот|Номер)\s*[:#№-]?\s*(\d{1,12})",
        r"#MM2-(\d{1,12})",
        r"\[MM2:(\d{1,12})\]",
    ],
    "delay_keywords": [
        "позже",
        "отложи",
        "завтра",
        "потом",
        "не сейчас",
        "/delay",
    ],
    "ready_keywords": [
        "/ready",
        "готов",
        "готово",
        "можно",
        "я тут",
    ],
    "buyer_menu_keywords": [
        "/menu",
        "/help",
        "меню",
        "помощь",
        "команды",
    ],
    "buyer_back_keywords": [
        "/back",
        "назад",
        "вернуться",
    ],
    "nickname_min_len": 3,
    "nickname_max_len": 20,
    "browser": {
        "enabled": False,
        "headless": False,
        "slow_mo_ms": 50,
        "launch_timeout_ms": 120000,
        "join_wait_seconds": 45,
        "selectors": {
            "profile_join_button": "button:has-text('Join')",
            "vip_join_button": "a:has-text('Join')",
            "trade_request_button": "[data-mm2='trade-request']",
            "trade_search_input": "[data-mm2='trade-search']",
            "trade_player_row": "[data-mm2='trade-player-row']",
            "inventory_search_input": "[data-mm2='inventory-search']",
            "inventory_item": "[data-mm2='inventory-item']",
            "trade_offer_slot": "[data-mm2='trade-offer-slot']",
            "trade_accept_button": "[data-mm2='trade-accept']",
            "trade_confirm_button": "[data-mm2='trade-confirm']",
            "trade_success_marker": "[data-mm2='trade-success']",
            "trade_privacy_marker": "[data-mm2='trade-privacy-error']",
            "trade_declined_marker": "[data-mm2='trade-declined']",
        },
    },
    "messages": {
        "welcome": (
            "Спасибо за покупку! Пожалуйста, отправьте ваш точный никнейм Roblox в ответ на это сообщение.\n"
            "Если нужна подсказка по выдаче, напишите /menu."
        ),
        "unknown_lot": (
            "Не удалось определить предмет по описанию лота. "
            "Администратор уже получил уведомление, заказ будет обработан вручную."
        ),
        "invalid_nick_format": (
            "Ник Roblox может содержать только латинские буквы, цифры и подчёркивание, "
            "длина от {min_len} до {max_len} символов. Отправьте ник ещё раз."
        ),
        "nick_not_found": "Пользователь Roblox с ником {nickname} не найден. Проверьте написание и отправьте ник заново.",
        "friend_sent": "Запрос в друзья отправлен на ник {nickname}. Примите его и зайдите в игру.",
        "already_friends": "Мы уже в друзьях с {nickname}. Зайдите в игру, я начну передачу предмета.",
        "friends_limit": "У вас достигнут лимит друзей (200/200). Очистите список и напишите свой ник ещё раз.",
        "friend_privacy": (
            "Roblox не разрешил отправить запрос в друзья. Проверьте настройки приватности "
            "или добавьте бота вручную: {profile_url}"
        ),
        "waiting_join": (
            "Жду вас в игре для передачи предмета {item_name}. "
            "Примите дружбу и зайдите на VIP-сервер: {server_url}\n"
            "Если хотите, чтобы я зашёл на ваш сервер, напишите /joinme. Меню команд: /menu."
        ),
        "join_fallback": (
            "Я не смог передать вам предмет. Пожалуйста, нажмите кнопку 'Join' в моём профиле Roblox "
            "({profile_url}) или зайдите ко мне на VIP-сервер: {server_url}"
        ),
        "joinme_need_nick": "Сначала отправьте ваш точный ник Roblox, чтобы я понял, к кому присоединяться.",
        "joinme_no_presence": "Я пока не вижу вас в MM2. Зайдите на сервер Murder Mystery 2 и напишите /joinme ещё раз.",
        "joinme_private_unknown": (
            "Вижу, что вы в MM2, но Roblox не отдал вместимость сервера. "
            "Пробую присоединиться к вам. Если сервер полный, перейдите на другой сервер и напишите /joinme."
        ),
        "joinme_joining": (
            "Вижу ваш сервер MM2 ({playing}/{max_players}). Пробую присоединиться к вам и передать {item_name}. "
            "Пожалуйста, оставайтесь в игре и не уходите AFK."
        ),
        "joinme_server_full": (
            "Ваш сервер заполнен ({playing}/{max_players}). Смените сервер и напишите /joinme ещё раз. "
            "Если это ваш VIP-сервер, удалите/попросите выйти одного игрока, чтобы я смог зайти."
        ),
        "trade_started": "Вижу вас в игре. Начинаю трейд предмета {item_name}. Пожалуйста, не уходите AFK.",
        "trade_timeout": "Трейд не был принят за 60 секунд. Я попробую ещё раз, когда вы будете готовы.",
        "trade_privacy": (
            "Не удалось открыть трейд: у вас отключены трейды в настройках приватности Roblox. "
            "Откройте Settings -> Privacy -> Who can trade with me и разрешите трейды."
        ),
        "trade_declined": "Трейд был отклонён. Если вы готовы принять предмет, напишите /ready.",
        "completed": (
            "Спасибо за заказ! Предмет {item_name} передан. "
            "Пожалуйста, подтвердите выполнение заказа на FunPay."
        ),
        "buyer_menu_main": (
            "Меню выдачи MM2:\n"
            "1. Отправить ник Roblox\n"
            "2. Позвать бота на мой сервер (/joinme)\n"
            "3. Отложить выдачу (/delay)\n"
            "4. Я готов (/ready)\n"
            "5. Статус заказа\n\n"
            "Напишите цифру 1-5 или команду. Для возврата напишите /back."
        ),
        "buyer_menu_nick": (
            "Отправьте ваш точный Roblox username одним сообщением. "
            "Не display name, а именно username из профиля."
        ),
        "buyer_menu_joinme": (
            "Чтобы бот зашёл к вам: зайдите в Murder Mystery 2 и напишите /joinme. "
            "Если сервер полный, бот попросит сменить сервер или освободить слот."
        ),
        "buyer_status": (
            "Статус заказа: {state}\n"
            "Предмет: {item_name}\n"
            "Roblox: {roblox_username}\n"
            "Если нужна помощь, напишите /menu."
        ),
        "delayed": "Передача отложена. Как будете готовы, напишите '/ready'.",
        "ready": "Возобновляю передачу. Проверьте дружбу с ботом и зайдите в игру.",
        "auth_paused": (
            "Автовыдача временно приостановлена: сессия Roblox-бота устарела. "
            "Администратор уже получил уведомление."
        ),
        "manual_required": (
            "Автоматическая выдача не завершилась. Администратор проверит заказ вручную. "
            "Ваш предмет: {item_name}."
        ),
    },
}


def deep_merge(defaults: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = dict(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings_file() -> Dict[str, Any]:
    ensure_storage_dir()
    if not os.path.exists(SETTINGS_FILE):
        save_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, dict):
            raise ValueError("settings root is not a JSON object")
        merged = deep_merge(DEFAULT_SETTINGS, loaded)
        if merged != loaded:
            save_json(SETTINGS_FILE, merged)
        return merged
    except Exception as exc:
        logger.error("%s: не удалось прочитать settings.json: %s", LOGGER_PREFIX, exc)
        broken_name = SETTINGS_FILE + f".broken-{int(time.time())}"
        with contextlib.suppress(Exception):
            os.replace(SETTINGS_FILE, broken_name)
        save_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)


def save_settings_file(settings: Dict[str, Any]) -> None:
    save_json(SETTINGS_FILE, deep_merge(DEFAULT_SETTINGS, settings))


def update_setting_path(path: str, value: Any) -> Dict[str, Any]:
    settings = load_settings_file()
    cursor = settings
    parts = path.split(".")
    for part in parts[:-1]:
        current = cursor.get(part)
        if not isinstance(current, dict):
            current = {}
            cursor[part] = current
        cursor = current
    cursor[parts[-1]] = value
    save_settings_file(settings)
    return settings


def toggle_setting_path(path: str) -> Dict[str, Any]:
    settings = load_settings_file()
    cursor = settings
    parts = path.split(".")
    for part in parts[:-1]:
        current = cursor.get(part)
        if not isinstance(current, dict):
            current = {}
            cursor[part] = current
        cursor = current
    cursor[parts[-1]] = not bool(cursor.get(parts[-1], False))
    save_settings_file(settings)
    return settings


def get_setting_path(settings: Dict[str, Any], path: str, default: Any = None) -> Any:
    cursor: Any = settings
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def mask_secret(value: str, visible: int = 4) -> str:
    value = str(value or "")
    if not value:
        return "не задан"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def save_json(path: str, payload: Any) -> None:
    ensure_storage_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


@dataclasses.dataclass
class PluginSettings:
    enabled: bool
    roblox_security_cookie: str
    roblox_bot_user_id: int
    roblox_bot_username: str
    roblox_profile_url: str
    roblox_vip_server_url: str
    roblox_place_id: int
    expected_game_instance_id: str
    use_public_server_if_no_vip: bool
    delivery_timeout_minutes: int
    presence_poll_seconds: int
    friend_accept_timeout_minutes: int
    trade_timeout_seconds: int
    trade_retry_count: int
    trade_retry_delay_seconds: int
    message_antispam_seconds: int
    roblox_http_timeout_seconds: int
    roblox_http_retries: int
    roblox_http_retry_delay_seconds: int
    admin_funpay_chat_id: str
    admin_notifications_to_telegram: bool
    admin_notifications_to_funpay: bool
    telegram_mirror_funpay_messages: bool
    seller_funpay_usernames: List[str]
    ignore_orders_where_buyer_is_me: bool
    require_mm2_category_match: bool
    mm2_category_keywords: List[str]
    pause_on_auth_error: bool
    auto_start_delivery_after_friend_request: bool
    delivery_queue_workers: int
    inventory_sync: Dict[str, Any]
    buyer_server_join_enabled: bool
    buyer_join_keywords: List[str]
    lot_id_patterns: List[str]
    delay_keywords: List[str]
    ready_keywords: List[str]
    buyer_menu_keywords: List[str]
    buyer_back_keywords: List[str]
    nickname_min_len: int
    nickname_max_len: int
    browser: Dict[str, Any]
    messages: Dict[str, str]

    @classmethod
    def load(cls, extra_config: Optional[Dict[str, Any]] = None) -> "PluginSettings":
        data = load_settings_file()
        if extra_config:
            data = deep_merge(data, extra_config)
        return cls(
            enabled=bool(data.get("enabled", True)),
            roblox_security_cookie=str(data.get("roblox_security_cookie", "")),
            roblox_bot_user_id=int(data.get("roblox_bot_user_id") or 0),
            roblox_bot_username=str(data.get("roblox_bot_username", "")),
            roblox_profile_url=str(data.get("roblox_profile_url", "")),
            roblox_vip_server_url=str(data.get("roblox_vip_server_url", "")),
            roblox_place_id=int(data.get("roblox_place_id") or 0),
            expected_game_instance_id=str(data.get("expected_game_instance_id", "")),
            use_public_server_if_no_vip=bool(data.get("use_public_server_if_no_vip", False)),
            delivery_timeout_minutes=max(1, int(data.get("delivery_timeout_minutes") or 12)),
            presence_poll_seconds=max(5, int(data.get("presence_poll_seconds") or 15)),
            friend_accept_timeout_minutes=max(1, int(data.get("friend_accept_timeout_minutes") or 6)),
            trade_timeout_seconds=max(20, int(data.get("trade_timeout_seconds") or 60)),
            trade_retry_count=max(1, int(data.get("trade_retry_count") or 3)),
            trade_retry_delay_seconds=max(1, int(data.get("trade_retry_delay_seconds") or 10)),
            message_antispam_seconds=max(5, int(data.get("message_antispam_seconds") or 45)),
            roblox_http_timeout_seconds=max(5, int(data.get("roblox_http_timeout_seconds") or 20)),
            roblox_http_retries=max(1, int(data.get("roblox_http_retries") or 3)),
            roblox_http_retry_delay_seconds=max(1, int(data.get("roblox_http_retry_delay_seconds") or 2)),
            admin_funpay_chat_id=str(data.get("admin_funpay_chat_id", "")),
            admin_notifications_to_telegram=bool(data.get("admin_notifications_to_telegram", True)),
            admin_notifications_to_funpay=bool(data.get("admin_notifications_to_funpay", False)),
            telegram_mirror_funpay_messages=bool(data.get("telegram_mirror_funpay_messages", True)),
            seller_funpay_usernames=[str(x).lower() for x in data.get("seller_funpay_usernames", [])],
            ignore_orders_where_buyer_is_me=bool(data.get("ignore_orders_where_buyer_is_me", True)),
            require_mm2_category_match=bool(data.get("require_mm2_category_match", True)),
            mm2_category_keywords=[str(x).lower() for x in data.get("mm2_category_keywords", [])],
            pause_on_auth_error=bool(data.get("pause_on_auth_error", True)),
            auto_start_delivery_after_friend_request=bool(data.get("auto_start_delivery_after_friend_request", True)),
            delivery_queue_workers=max(1, int(data.get("delivery_queue_workers") or 1)),
            inventory_sync=dict(data.get("inventory_sync", {})),
            buyer_server_join_enabled=bool(data.get("buyer_server_join_enabled", True)),
            buyer_join_keywords=[str(x).lower() for x in data.get("buyer_join_keywords", [])],
            lot_id_patterns=[str(x) for x in data.get("lot_id_patterns", [])],
            delay_keywords=[str(x).lower() for x in data.get("delay_keywords", [])],
            ready_keywords=[str(x).lower() for x in data.get("ready_keywords", [])],
            buyer_menu_keywords=[str(x).lower() for x in data.get("buyer_menu_keywords", [])],
            buyer_back_keywords=[str(x).lower() for x in data.get("buyer_back_keywords", [])],
            nickname_min_len=max(1, int(data.get("nickname_min_len") or 3)),
            nickname_max_len=max(3, int(data.get("nickname_max_len") or 20)),
            browser=dict(data.get("browser", {})),
            messages=dict(data.get("messages", {})),
        )

    def message(self, key: str, **kwargs: Any) -> str:
        template = self.messages.get(key) or DEFAULT_SETTINGS["messages"].get(key, "")
        safe_kwargs = {k: "" if v is None else v for k, v in kwargs.items()}
        try:
            return template.format(**safe_kwargs)
        except Exception:
            logger.warning("%s: ошибка форматирования шаблона %s", LOGGER_PREFIX, key)
            return template


# ─────────────────────────────────────────────────────────────────────────────
# Модели состояний
# ─────────────────────────────────────────────────────────────────────────────

class OrderState(str, enum.Enum):
    NEW = "NEW"
    WAITING_NICK = "WAITING_NICK"
    WAITING_FRIEND = "WAITING_FRIEND"
    WAITING_JOIN = "WAITING_JOIN"
    TRADING = "TRADING"
    DELAYED = "DELAYED"
    COMPLETED = "COMPLETED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    FAILED = "FAILED"


class DeliveryOutcome(str, enum.Enum):
    SUCCESS = "SUCCESS"
    BUYER_NOT_IN_SERVER = "BUYER_NOT_IN_SERVER"
    TRADE_TIMEOUT = "TRADE_TIMEOUT"
    TRADE_PRIVACY_DISABLED = "TRADE_PRIVACY_DISABLED"
    TRADE_DECLINED = "TRADE_DECLINED"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    AUTH_ERROR = "AUTH_ERROR"
    AUTOMATION_UNAVAILABLE = "AUTOMATION_UNAVAILABLE"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class FriendRequestOutcome(str, enum.Enum):
    SENT = "SENT"
    ALREADY_FRIENDS = "ALREADY_FRIENDS"
    BUYER_FRIEND_LIMIT = "BUYER_FRIEND_LIMIT"
    PRIVACY_BLOCKED = "PRIVACY_BLOCKED"
    AUTH_ERROR = "AUTH_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


TERMINAL_STATES = {OrderState.COMPLETED, OrderState.MANUAL_REVIEW, OrderState.FAILED}


@dataclasses.dataclass
class ItemMapping:
    lot_id: str
    item_name: str
    category: str = ""
    stock: int = 0
    enabled: bool = True
    notes: str = ""


@dataclasses.dataclass
class InventoryItem:
    item_name: str
    quantity: int = 1
    category: str = ""
    source: str = "unknown"
    external_id: str = ""
    lot_id: str = ""
    raw: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class InventorySyncResult:
    ok: bool
    source: str
    items: List[InventoryItem] = dataclasses.field(default_factory=list)
    created_mappings: int = 0
    updated_inventory: int = 0
    errors: List[str] = dataclasses.field(default_factory=list)
    hints: List[str] = dataclasses.field(default_factory=list)

    def summary_text(self) -> str:
        lines = [
            "Синхронизация инвентаря MM2",
            f"Статус: {'OK' if self.ok else 'ERROR'}",
            f"Источник: {self.source}",
            f"Найдено предметов: {len(self.items)}",
            f"Создано/обновлено ID лотов: {self.created_mappings}",
            f"Обновлено строк инвентаря: {self.updated_inventory}",
        ]
        if self.errors:
            lines.append("")
            lines.append("Проблемы:")
            lines.extend(f"- {err}" for err in self.errors[:8])
        if self.hints:
            lines.append("")
            lines.append("Как исправить:")
            lines.extend(f"- {hint}" for hint in self.hints[:8])
        return "\n".join(lines)


@dataclasses.dataclass
class DeliveryOrder:
    order_id: str
    chat_id: str
    buyer: str
    lot_id: str
    item_name: str
    state: OrderState
    roblox_username: str = ""
    roblox_user_id: int = 0
    retry_count: int = 0
    trade_attempts: int = 0
    last_error: str = ""
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)
    created_at: str = dataclasses.field(default_factory=utc_now)
    updated_at: str = dataclasses.field(default_factory=utc_now)


@dataclasses.dataclass
class RobloxUser:
    user_id: int
    username: str
    display_name: str = ""
    banned: bool = False


@dataclasses.dataclass
class RobloxPresence:
    user_id: int
    presence_type: int
    last_location: str = ""
    place_id: int = 0
    root_place_id: int = 0
    game_id: str = ""

    @property
    def in_game(self) -> bool:
        return self.presence_type == 2


@dataclasses.dataclass
class RobloxServerCapacity:
    game_id: str
    playing: int = 0
    max_players: int = 0
    source: str = "unknown"

    @property
    def known(self) -> bool:
        return bool(self.game_id and self.max_players > 0)

    @property
    def is_full(self) -> bool:
        return self.known and self.playing >= self.max_players

    @property
    def has_space(self) -> bool:
        return self.known and self.playing < self.max_players


@dataclasses.dataclass
class HttpResult:
    status: int
    headers: Dict[str, str]
    body: str
    json_data: Any = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclasses.dataclass
class FriendRequestResult:
    outcome: FriendRequestOutcome
    message: str = ""


@dataclasses.dataclass
class TradeResult:
    outcome: DeliveryOutcome
    message: str = ""


def normalize_item_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def clean_inventory_item_name(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    value = re.sub(r"^\[[^\]]+\]\s*", "", value)
    value = re.sub(r"\s+x\d+$", "", value, flags=re.I)
    return value.strip()


def parse_inventory_card_text(text: str) -> Tuple[str, int]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return "", 1
    joined = " ".join(lines)
    qty = 1
    for candidate in reversed(lines + [joined]):
        match = re.search(r"(?:x|×)\s*(\d+)|(\d+)\s*(?:шт|pcs|pc)", candidate, flags=re.I)
        if match:
            qty = max(1, int(match.group(1) or match.group(2)))
            break
    name = lines[0]
    if len(lines) > 1 and re.fullmatch(r"(?:x|×)?\s*\d+\s*(?:шт|pcs|pc)?", name, flags=re.I):
        name = lines[1]
    return clean_inventory_item_name(name), qty


def dedupe_inventory_items(items: Sequence[InventoryItem]) -> List[InventoryItem]:
    merged: Dict[str, InventoryItem] = {}
    for item in items:
        name = clean_inventory_item_name(item.item_name)
        if not name:
            continue
        key = normalize_item_name(name)
        existing = merged.get(key)
        if existing:
            existing.quantity += max(0, int(item.quantity or 0))
            if not existing.external_id and item.external_id:
                existing.external_id = item.external_id
            continue
        item.item_name = name
        item.quantity = max(1, int(item.quantity or 1))
        merged[key] = item
    return list(merged.values())


# ─────────────────────────────────────────────────────────────────────────────
# SQLite: таблица лотов, заказы, события, дедупликация сообщений
# ─────────────────────────────────────────────────────────────────────────────

class SQLiteStore:
    def __init__(self, db_path: str = DB_FILE) -> None:
        ensure_storage_dir()
        self.db_path = db_path
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS item_mapping (
                    lot_id TEXT PRIMARY KEY,
                    item_name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    stock INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inventory_items (
                    item_name TEXT PRIMARY KEY,
                    lot_id TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    category TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    external_id TEXT NOT NULL DEFAULT '',
                    raw TEXT NOT NULL DEFAULT '{}',
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_inventory_lot_id ON inventory_items(lot_id);

                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    buyer TEXT NOT NULL,
                    lot_id TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    roblox_username TEXT NOT NULL DEFAULT '',
                    roblox_user_id INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    trade_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_orders_chat_state ON orders(chat_id, state);
                CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
                CREATE INDEX IF NOT EXISTS idx_orders_buyer_state ON orders(buyer, state);

                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_dedupe (
                    message_key TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outgoing_antispam (
                    scope TEXT PRIMARY KEY,
                    last_sent_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plugin_flags (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def seed_examples_if_empty(self) -> None:
        """Добавляет примеры маппинга, чтобы администратор видел формат базы."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM item_mapping").fetchone()
            if row and int(row["c"]) > 0:
                return
            now = utc_now()
            examples = [
                ("1001", "Harvester", "gun", 0, 1, "Пример: замените на свой лот FunPay"),
                ("1002", "Icebreaker", "knife", 0, 1, "Пример: замените на свой лот FunPay"),
                ("1003", "Corrupt", "knife", 0, 1, "Пример: замените на свой лот FunPay"),
            ]
            conn.executemany(
                """
                INSERT INTO item_mapping(lot_id, item_name, category, stock, enabled, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(lot_id, item, category, stock, enabled, notes, now, now) for lot_id, item, category, stock, enabled, notes in examples],
            )
            logger.info("%s: создан пример таблицы соответствия MM2-лотов", LOGGER_PREFIX)

    def get_mapping(self, lot_id: str) -> Optional[ItemMapping]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM item_mapping WHERE lot_id = ?", (str(lot_id),)).fetchone()
            if not row:
                return None
            return ItemMapping(
                lot_id=str(row["lot_id"]),
                item_name=str(row["item_name"]),
                category=str(row["category"] or ""),
                stock=int(row["stock"] or 0),
                enabled=bool(row["enabled"]),
                notes=str(row["notes"] or ""),
            )

    def upsert_mapping(self, item: ItemMapping) -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO item_mapping(lot_id, item_name, category, stock, enabled, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lot_id) DO UPDATE SET
                    item_name = excluded.item_name,
                    category = excluded.category,
                    stock = excluded.stock,
                    enabled = excluded.enabled,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (item.lot_id, item.item_name, item.category, item.stock, int(item.enabled), item.notes, now, now),
            )

    def get_mapping_by_item_name(self, item_name: str) -> Optional[ItemMapping]:
        normalized = normalize_item_name(item_name)
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM item_mapping").fetchall()
        for row in rows:
            if normalize_item_name(str(row["item_name"])) == normalized:
                return ItemMapping(
                    lot_id=str(row["lot_id"]),
                    item_name=str(row["item_name"]),
                    category=str(row["category"] or ""),
                    stock=int(row["stock"] or 0),
                    enabled=bool(row["enabled"]),
                    notes=str(row["notes"] or ""),
                )
        return None

    def next_lot_id(self, start: int = 1001) -> str:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT lot_id FROM item_mapping").fetchall()
        used: Set[int] = set()
        for row in rows:
            with contextlib.suppress(Exception):
                used.add(int(str(row["lot_id"])))
        candidate = max(1, int(start))
        while candidate in used:
            candidate += 1
        return str(candidate)

    def list_mappings(self) -> List[ItemMapping]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM item_mapping ORDER BY CAST(lot_id AS INTEGER), lot_id").fetchall()
        return [
            ItemMapping(
                lot_id=str(row["lot_id"]),
                item_name=str(row["item_name"]),
                category=str(row["category"] or ""),
                stock=int(row["stock"] or 0),
                enabled=bool(row["enabled"]),
                notes=str(row["notes"] or ""),
            )
            for row in rows
        ]

    def delete_mapping(self, lot_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM item_mapping WHERE lot_id = ?", (str(lot_id),))
            return cur.rowcount > 0

    def adjust_stock(self, lot_id: str, delta: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE item_mapping
                SET stock = CASE WHEN stock + ? < 0 THEN 0 ELSE stock + ? END,
                    updated_at = ?
                WHERE lot_id = ?
                """,
                (int(delta), int(delta), utc_now(), str(lot_id)),
            )

    def upsert_inventory_item(self, item: InventoryItem, auto_mapping: bool = True, lot_id_start: int = 1001) -> InventoryItem:
        existing_mapping = self.get_mapping_by_item_name(item.item_name)
        if existing_mapping:
            item.lot_id = existing_mapping.lot_id
            self.upsert_mapping(
                ItemMapping(
                    lot_id=existing_mapping.lot_id,
                    item_name=existing_mapping.item_name,
                    category=existing_mapping.category or item.category or ItemNameHelper().suggest_category(item.item_name),
                    stock=max(0, int(item.quantity or 0)),
                    enabled=existing_mapping.enabled,
                    notes=existing_mapping.notes or f"Обновлено из инвентаря ({item.source})",
                )
            )
        elif auto_mapping:
            item.lot_id = item.lot_id or self.next_lot_id(lot_id_start)
            self.upsert_mapping(
                ItemMapping(
                    lot_id=item.lot_id,
                    item_name=item.item_name,
                    category=item.category or ItemNameHelper().suggest_category(item.item_name),
                    stock=max(0, int(item.quantity or 0)),
                    enabled=True,
                    notes=f"Авто из инвентаря ({item.source})",
                )
            )
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inventory_items(item_name, lot_id, quantity, category, source, external_id, raw, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_name) DO UPDATE SET
                    lot_id = excluded.lot_id,
                    quantity = excluded.quantity,
                    category = excluded.category,
                    source = excluded.source,
                    external_id = excluded.external_id,
                    raw = excluded.raw,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    item.item_name,
                    item.lot_id,
                    max(0, int(item.quantity or 0)),
                    item.category,
                    item.source,
                    item.external_id,
                    json.dumps(item.raw or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return item

    def upsert_inventory_items(self, items: Sequence[InventoryItem], auto_mapping: bool = True, lot_id_start: int = 1001) -> Tuple[int, int]:
        updated = 0
        created_mappings = 0
        for item in dedupe_inventory_items(items):
            had_mapping = self.get_mapping_by_item_name(item.item_name) is not None
            stored = self.upsert_inventory_item(item, auto_mapping=auto_mapping, lot_id_start=lot_id_start)
            updated += 1
            if stored.lot_id and not had_mapping:
                created_mappings += 1
        return updated, created_mappings

    def list_inventory_items(self, limit: int = 200) -> List[InventoryItem]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT item_name, lot_id, quantity, category, source, external_id, raw
                FROM inventory_items
                ORDER BY CAST(NULLIF(lot_id, '') AS INTEGER), item_name
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        result: List[InventoryItem] = []
        for row in rows:
            try:
                raw = json.loads(row["raw"] or "{}")
            except Exception:
                raw = {}
            result.append(
                InventoryItem(
                    item_name=str(row["item_name"]),
                    lot_id=str(row["lot_id"] or ""),
                    quantity=int(row["quantity"] or 0),
                    category=str(row["category"] or ""),
                    source=str(row["source"] or ""),
                    external_id=str(row["external_id"] or ""),
                    raw=raw if isinstance(raw, dict) else {},
                )
            )
        return result

    def get_recent_events(self, order_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT state, event_type, message, data, created_at
                FROM order_events
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(order_id), int(limit)),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row["data"] or "{}")
            except Exception:
                data = {}
            events.append(
                {
                    "state": str(row["state"]),
                    "event_type": str(row["event_type"]),
                    "message": str(row["message"] or ""),
                    "data": data,
                    "created_at": str(row["created_at"]),
                }
            )
        return events

    def create_order(self, order: DeliveryOrder) -> bool:
        now = utc_now()
        order.created_at = order.created_at or now
        order.updated_at = now
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO orders(
                        order_id, chat_id, buyer, lot_id, item_name, state,
                        roblox_username, roblox_user_id, retry_count, trade_attempts,
                        last_error, metadata, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.order_id,
                        str(order.chat_id),
                        order.buyer,
                        order.lot_id,
                        order.item_name,
                        order.state.value,
                        order.roblox_username,
                        int(order.roblox_user_id),
                        int(order.retry_count),
                        int(order.trade_attempts),
                        order.last_error,
                        json.dumps(order.metadata, ensure_ascii=False),
                        order.created_at,
                        order.updated_at,
                    ),
                )
                self.add_event(order.order_id, order.state, "created", f"Создан заказ для {order.item_name}", order.metadata, conn)
                return True
            except sqlite3.IntegrityError:
                return False

    def get_order(self, order_id: str) -> Optional[DeliveryOrder]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (str(order_id),)).fetchone()
        return self._row_to_order(row) if row else None

    def get_active_order_by_chat(self, chat_id: Any, buyer: str = "") -> Optional[DeliveryOrder]:
        states = tuple(s.value for s in OrderState if s not in TERMINAL_STATES)
        placeholders = ",".join("?" for _ in states)
        params: List[Any] = [str(chat_id), *states]
        sql = f"SELECT * FROM orders WHERE chat_id = ? AND state IN ({placeholders}) ORDER BY created_at DESC LIMIT 1"
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if row:
                return self._row_to_order(row)
            if buyer:
                params = [str(buyer), *states]
                sql = f"SELECT * FROM orders WHERE buyer = ? AND state IN ({placeholders}) ORDER BY created_at DESC LIMIT 1"
                row = conn.execute(sql, params).fetchone()
        return self._row_to_order(row) if row else None

    def list_orders_by_states(self, states: Sequence[OrderState], limit: int = 100) -> List[DeliveryOrder]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        params: List[Any] = [s.value for s in states]
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM orders WHERE state IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_order(row) for row in rows if row]

    def update_order(self, order: DeliveryOrder, event_type: str = "updated", message: str = "") -> None:
        order.updated_at = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE orders SET
                    chat_id = ?, buyer = ?, lot_id = ?, item_name = ?, state = ?,
                    roblox_username = ?, roblox_user_id = ?, retry_count = ?, trade_attempts = ?,
                    last_error = ?, metadata = ?, updated_at = ?
                WHERE order_id = ?
                """,
                (
                    str(order.chat_id),
                    order.buyer,
                    order.lot_id,
                    order.item_name,
                    order.state.value,
                    order.roblox_username,
                    int(order.roblox_user_id),
                    int(order.retry_count),
                    int(order.trade_attempts),
                    order.last_error,
                    json.dumps(order.metadata, ensure_ascii=False),
                    order.updated_at,
                    order.order_id,
                ),
            )
            self.add_event(order.order_id, order.state, event_type, message, order.metadata, conn)

    def transition_order(
        self,
        order: DeliveryOrder,
        new_state: OrderState,
        event_type: str,
        message: str = "",
        **metadata: Any,
    ) -> DeliveryOrder:
        order.state = new_state
        order.metadata.update({k: v for k, v in metadata.items() if v is not None})
        self.update_order(order, event_type=event_type, message=message)
        return order

    def add_event(
        self,
        order_id: str,
        state: OrderState,
        event_type: str,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        payload = (
            str(order_id),
            state.value if isinstance(state, OrderState) else str(state),
            str(event_type),
            str(message or ""),
            json.dumps(data or {}, ensure_ascii=False),
            utc_now(),
        )
        if conn is not None:
            conn.execute(
                "INSERT INTO order_events(order_id, state, event_type, message, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )
            return
        with self._lock, self._connect() as own_conn:
            own_conn.execute(
                "INSERT INTO order_events(order_id, state, event_type, message, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )

    def was_message_seen(self, message_key: str, chat_id: str, text: str) -> bool:
        now = time.time()
        cutoff = now - 86400
        text_hash = str(abs(hash(text)))
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM message_dedupe WHERE created_at < ?", (cutoff,))
            row = conn.execute("SELECT message_key FROM message_dedupe WHERE message_key = ?", (message_key,)).fetchone()
            if row:
                return True
            conn.execute(
                "INSERT INTO message_dedupe(message_key, chat_id, text_hash, created_at) VALUES (?, ?, ?, ?)",
                (message_key, str(chat_id), text_hash, now),
            )
        return False

    def can_send_scope(self, scope: str, interval_seconds: int) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT last_sent_at FROM outgoing_antispam WHERE scope = ?", (scope,)).fetchone()
            if row and now - float(row["last_sent_at"]) < interval_seconds:
                return False
            conn.execute(
                """
                INSERT INTO outgoing_antispam(scope, last_sent_at) VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET last_sent_at = excluded.last_sent_at
                """,
                (scope, now),
            )
        return True

    def set_flag(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plugin_flags(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def get_flag(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM plugin_flags WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _row_to_order(self, row: sqlite3.Row) -> DeliveryOrder:
        try:
            metadata = json.loads(row["metadata"] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        return DeliveryOrder(
            order_id=str(row["order_id"]),
            chat_id=str(row["chat_id"]),
            buyer=str(row["buyer"]),
            lot_id=str(row["lot_id"]),
            item_name=str(row["item_name"]),
            state=OrderState(str(row["state"])),
            roblox_username=str(row["roblox_username"] or ""),
            roblox_user_id=int(row["roblox_user_id"] or 0),
            retry_count=int(row["retry_count"] or 0),
            trade_attempts=int(row["trade_attempts"] or 0),
            last_error=str(row["last_error"] or ""),
            metadata=metadata,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


# ─────────────────────────────────────────────────────────────────────────────
# State Machine
# ─────────────────────────────────────────────────────────────────────────────

class OrderStateMachine:
    ALLOWED: Dict[OrderState, Tuple[OrderState, ...]] = {
        OrderState.NEW: (OrderState.WAITING_NICK, OrderState.MANUAL_REVIEW, OrderState.FAILED),
        OrderState.WAITING_NICK: (OrderState.WAITING_FRIEND, OrderState.DELAYED, OrderState.MANUAL_REVIEW, OrderState.FAILED),
        OrderState.WAITING_FRIEND: (OrderState.WAITING_JOIN, OrderState.TRADING, OrderState.DELAYED, OrderState.MANUAL_REVIEW, OrderState.FAILED),
        OrderState.WAITING_JOIN: (OrderState.TRADING, OrderState.DELAYED, OrderState.MANUAL_REVIEW, OrderState.FAILED),
        OrderState.TRADING: (OrderState.WAITING_JOIN, OrderState.DELAYED, OrderState.COMPLETED, OrderState.MANUAL_REVIEW, OrderState.FAILED),
        OrderState.DELAYED: (OrderState.WAITING_NICK, OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN, OrderState.TRADING, OrderState.MANUAL_REVIEW),
        OrderState.COMPLETED: tuple(),
        OrderState.MANUAL_REVIEW: (OrderState.WAITING_NICK, OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN, OrderState.TRADING, OrderState.COMPLETED),
        OrderState.FAILED: (OrderState.MANUAL_REVIEW,),
    }

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def transition(
        self,
        order: DeliveryOrder,
        new_state: OrderState,
        event_type: str,
        message: str = "",
        **metadata: Any,
    ) -> DeliveryOrder:
        if new_state == order.state:
            order.metadata.update({k: v for k, v in metadata.items() if v is not None})
            self.store.update_order(order, event_type=event_type, message=message)
            return order
        allowed = self.ALLOWED.get(order.state, tuple())
        if new_state not in allowed:
            raise ValueError(f"Недопустимый переход заказа {order.order_id}: {order.state.value} -> {new_state.value}")
        return self.store.transition_order(order, new_state, event_type, message, **metadata)


# ─────────────────────────────────────────────────────────────────────────────
# FunPay adapter
# ─────────────────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def normalize_chat_id(chat_id: Any) -> str:
    if chat_id is None:
        return ""
    raw = str(chat_id).strip()
    return raw.removeprefix("chat")


class FunPayAdapter:
    def __init__(self, cardinal: Any, settings: PluginSettings, store: SQLiteStore) -> None:
        self.cardinal = cardinal
        self.settings = settings
        self.store = store

    def send_message(self, chat_id: Any, text: str, scope: str = "") -> None:
        chat = normalize_chat_id(chat_id)
        if not chat:
            logger.warning("%s: пропущена отправка без chat_id: %s", LOGGER_PREFIX, text)
            return
        if scope and not self.store.can_send_scope(scope, self.settings.message_antispam_seconds):
            logger.info("%s: антиспам пропустил сообщение scope=%s", LOGGER_PREFIX, scope)
            return
        cleaned = strip_html(text)
        try:
            self.cardinal.send_message(chat, cleaned)
            logger.info("%s: FunPay -> chat=%s: %s", LOGGER_PREFIX, chat, cleaned.replace("\n", " ")[:180])
            if self.settings.telegram_mirror_funpay_messages and not scope.startswith("admin:"):
                self.notify_admin(
                    f"FunPay chat {chat}\n"
                    f"Scope: {scope or '-'}\n"
                    f"Message:\n{cleaned}",
                    force_telegram=True,
                    allow_funpay=False,
                )
        except Exception as exc:
            logger.error("%s: ошибка отправки FunPay-сообщения chat=%s: %s", LOGGER_PREFIX, chat, exc)

    def notify_admin(self, text: str, force_telegram: bool = False, allow_funpay: Optional[bool] = None) -> None:
        allow_funpay = self.settings.admin_notifications_to_funpay if allow_funpay is None else allow_funpay
        admin_chat = self.settings.admin_funpay_chat_id
        if admin_chat and allow_funpay:
            self.send_message(admin_chat, f"[MM2 AutoDelivery]\n{text}", scope=f"admin:{hash(text)}")
        if self.settings.admin_notifications_to_telegram or force_telegram:
            _tg_send_to_admins(self.cardinal, f"🔪 <b>MM2 AutoDelivery</b>\n\n<pre>{_html(text)}</pre>")
        telegram = getattr(self.cardinal, "telegram", None)
        bot = getattr(telegram, "bot", None)
        tg_id = getattr(telegram, "admin_chat_id", None) or getattr(telegram, "chat_id", None)
        if bot and tg_id and (self.settings.admin_notifications_to_telegram or force_telegram):
            with contextlib.suppress(Exception):
                bot.send_message(tg_id, f"MM2 AutoDelivery\n{text}")

    def get_full_order_description(self, order_id: Any, fallback: str = "") -> str:
        try:
            full_order = self.cardinal.account.get_order(order_id)
            parts = [
                getattr(full_order, "full_description", "") or "",
                getattr(full_order, "description", "") or "",
                str(full_order or ""),
            ]
            return "\n".join(p for p in parts if p).strip() or fallback
        except Exception as exc:
            logger.warning("%s: не удалось получить полное описание заказа %s: %s", LOGGER_PREFIX, order_id, exc)
            return fallback

    def find_chat_id(self, buyer: str, fallback: Any = "") -> str:
        if fallback:
            return normalize_chat_id(fallback)
        try:
            chat = self.cardinal.account.get_chat_by_name(buyer)
            return normalize_chat_id(getattr(chat, "id", ""))
        except Exception:
            return ""


# ─────────────────────────────────────────────────────────────────────────────
# Roblox HTTP API
# ─────────────────────────────────────────────────────────────────────────────

class RobloxAuthError(RuntimeError):
    pass


class RobloxHttpError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"Roblox HTTP {status}: {message}")
        self.status = status
        self.payload = payload


class RobloxApiClient:
    BASE_HEADERS = {
        "User-Agent": "Mozilla/5.0 FunPayCardinal-MM2AutoDelivery/1.0",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self, settings: PluginSettings) -> None:
        self.settings = settings
        self._csrf_token = ""
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return bool(self.settings.roblox_security_cookie)

    async def validate_session(self) -> RobloxUser:
        result = await self.request("GET", "https://users.roblox.com/v1/users/authenticated", auth=True)
        if not result.ok:
            raise RobloxAuthError(result.body[:200])
        data = result.json_data or {}
        return RobloxUser(
            user_id=int(data.get("id") or 0),
            username=str(data.get("name") or data.get("username") or ""),
            display_name=str(data.get("displayName") or ""),
        )

    async def get_user_by_username(self, username: str) -> Optional[RobloxUser]:
        payload = {"usernames": [username], "excludeBannedUsers": True}
        result = await self.request("POST", "https://users.roblox.com/v1/usernames/users", json_payload=payload, auth=False)
        if not result.ok:
            raise RobloxHttpError(result.status, result.body, result.json_data)
        data = (result.json_data or {}).get("data") or []
        if not data:
            return None
        user = data[0]
        return RobloxUser(
            user_id=int(user.get("id") or 0),
            username=str(user.get("name") or username),
            display_name=str(user.get("displayName") or ""),
        )

    async def send_friend_request(self, user_id: int) -> FriendRequestResult:
        url = f"https://friends.roblox.com/v1/users/{int(user_id)}/request-friendship"
        result = await self.request("POST", url, json_payload={}, auth=True, allow_statuses=(400, 401, 403, 409))
        body_lower = (result.body or "").lower()
        if result.status in (401, 403) and "token" not in body_lower and "csrf" not in body_lower:
            return FriendRequestResult(FriendRequestOutcome.AUTH_ERROR, result.body)
        if result.ok:
            return FriendRequestResult(FriendRequestOutcome.SENT)
        if result.status == 409 or "already" in body_lower or "friends" in body_lower and "already" in body_lower:
            return FriendRequestResult(FriendRequestOutcome.ALREADY_FRIENDS, result.body)
        if "max" in body_lower and "friend" in body_lower or "too many friends" in body_lower or "200" in body_lower:
            return FriendRequestResult(FriendRequestOutcome.BUYER_FRIEND_LIMIT, result.body)
        if "privacy" in body_lower or "not allowed" in body_lower or "cannot" in body_lower:
            return FriendRequestResult(FriendRequestOutcome.PRIVACY_BLOCKED, result.body)
        return FriendRequestResult(FriendRequestOutcome.UNKNOWN_ERROR, result.body)

    async def get_friend_status(self, bot_user_id: int, buyer_user_id: int) -> str:
        if not bot_user_id:
            return ""
        url = f"https://friends.roblox.com/v1/users/{int(bot_user_id)}/friends/statuses?userIds={int(buyer_user_id)}"
        result = await self.request("GET", url, auth=True, allow_statuses=(400, 401, 403))
        if not result.ok:
            return ""
        data = (result.json_data or {}).get("data") or []
        if not data:
            return ""
        return str(data[0].get("status") or "")

    async def get_presence(self, user_id: int) -> Optional[RobloxPresence]:
        payload = {"userIds": [int(user_id)]}
        result = await self.request("POST", "https://presence.roblox.com/v1/presence/users", json_payload=payload, auth=True, allow_statuses=(401, 403))
        if result.status in (401, 403):
            raise RobloxAuthError(result.body)
        if not result.ok:
            logger.warning("%s: Roblox presence вернул %s: %s", LOGGER_PREFIX, result.status, result.body[:160])
            return None
        entries = (result.json_data or {}).get("userPresences") or []
        if not entries:
            return None
        item = entries[0]
        return RobloxPresence(
            user_id=int(item.get("userId") or user_id),
            presence_type=int(item.get("userPresenceType") or 0),
            last_location=str(item.get("lastLocation") or ""),
            place_id=int(item.get("placeId") or 0),
            root_place_id=int(item.get("rootPlaceId") or 0),
            game_id=str(item.get("gameId") or ""),
        )

    async def get_public_server_capacity(self, place_id: int, game_id: str) -> Optional[RobloxServerCapacity]:
        if not place_id or not game_id:
            return None
        cursor = ""
        for _page in range(5):
            params = {
                "sortOrder": "Asc",
                "limit": "100",
            }
            if cursor:
                params["cursor"] = cursor
            url = f"https://games.roblox.com/v1/games/{int(place_id)}/servers/Public?{urllib.parse.urlencode(params)}"
            result = await self.request("GET", url, auth=False, allow_statuses=(400, 401, 403, 404))
            if not result.ok:
                logger.info("%s: capacity сервера %s недоступна: HTTP %s", LOGGER_PREFIX, game_id, result.status)
                return None
            payload = result.json_data or {}
            for server in payload.get("data") or []:
                if str(server.get("id") or "") == str(game_id):
                    return RobloxServerCapacity(
                        game_id=str(game_id),
                        playing=int(server.get("playing") or 0),
                        max_players=int(server.get("maxPlayers") or 0),
                        source="public_servers",
                    )
            cursor = str(payload.get("nextPageCursor") or "")
            if not cursor:
                break
        return None

    async def get_collectibles_inventory(self, user_id: int) -> List[InventoryItem]:
        if not user_id:
            user = await self.validate_session()
            user_id = user.user_id
        cursor = ""
        items: List[InventoryItem] = []
        for _page in range(10):
            params = {
                "sortOrder": "Asc",
                "limit": "100",
            }
            if cursor:
                params["cursor"] = cursor
            url = f"https://inventory.roblox.com/v1/users/{int(user_id)}/assets/collectibles?{urllib.parse.urlencode(params)}"
            result = await self.request("GET", url, auth=True, allow_statuses=(400, 401, 403, 404))
            if result.status in (401, 403):
                raise RobloxAuthError(result.body)
            if not result.ok:
                raise RobloxHttpError(result.status, result.body, result.json_data)
            payload = result.json_data or {}
            for asset in payload.get("data") or []:
                name = str(asset.get("name") or asset.get("assetName") or "").strip()
                if not name:
                    continue
                items.append(
                    InventoryItem(
                        item_name=name,
                        quantity=1,
                        category="roblox_collectible",
                        source="roblox_collectibles",
                        external_id=str(asset.get("assetId") or asset.get("id") or ""),
                        raw=asset if isinstance(asset, dict) else {},
                    )
                )
            cursor = str(payload.get("nextPageCursor") or "")
            if not cursor:
                break
        return dedupe_inventory_items(items)

    async def request(
        self,
        method: str,
        url: str,
        json_payload: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        allow_statuses: Sequence[int] = tuple(),
    ) -> HttpResult:
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.settings.roblox_http_retries + 1):
            try:
                return await asyncio.to_thread(self._request_sync, method, url, json_payload, auth, tuple(allow_statuses))
            except RobloxAuthError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning("%s: Roblox request attempt %s/%s failed: %s", LOGGER_PREFIX, attempt, self.settings.roblox_http_retries, exc)
                if attempt < self.settings.roblox_http_retries:
                    await asyncio.sleep(self.settings.roblox_http_retry_delay_seconds * attempt)
        raise RobloxHttpError(0, str(last_exc or "unknown transport error"))

    def _request_sync(
        self,
        method: str,
        url: str,
        json_payload: Optional[Dict[str, Any]],
        auth: bool,
        allow_statuses: Sequence[int],
    ) -> HttpResult:
        if auth and not self.settings.roblox_security_cookie:
            raise RobloxAuthError(".ROBLOSECURITY не настроен")
        data: Optional[bytes] = None
        headers = dict(self.BASE_HEADERS)
        if json_payload is not None:
            data = json.dumps(json_payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Cookie"] = f".ROBLOSECURITY={self.settings.roblox_security_cookie}"
            if self._csrf_token and method.upper() != "GET":
                headers["X-CSRF-TOKEN"] = self._csrf_token
        return self._send_with_csrf(method, url, data, headers, auth, allow_statuses)

    def _send_with_csrf(
        self,
        method: str,
        url: str,
        data: Optional[bytes],
        headers: Dict[str, str],
        auth: bool,
        allow_statuses: Sequence[int],
    ) -> HttpResult:
        first = self._send_once(method, url, data, headers, allow_statuses)
        token = first.headers.get("x-csrf-token") or first.headers.get("X-CSRF-TOKEN")
        if token:
            with self._lock:
                self._csrf_token = token
        if auth and method.upper() != "GET" and first.status == 403 and token:
            retry_headers = dict(headers)
            retry_headers["X-CSRF-TOKEN"] = token
            return self._send_once(method, url, data, retry_headers, allow_statuses)
        if first.status in (401, 403) and auth and first.status not in allow_statuses:
            raise RobloxAuthError(first.body[:300])
        return first

    def _send_once(
        self,
        method: str,
        url: str,
        data: Optional[bytes],
        headers: Dict[str, str],
        allow_statuses: Sequence[int],
    ) -> HttpResult:
        request = urllib.request.Request(url=url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.settings.roblox_http_timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                return self._build_result(response.status, dict(response.headers), body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in allow_statuses:
                return self._build_result(exc.code, dict(exc.headers), body)
            return self._build_result(exc.code, dict(exc.headers), body)

    def _build_result(self, status: int, headers: Dict[str, str], body: str) -> HttpResult:
        json_data: Any = None
        if body:
            with contextlib.suppress(Exception):
                json_data = json.loads(body)
        return HttpResult(status=status, headers=headers, body=body, json_data=json_data)


# ─────────────────────────────────────────────────────────────────────────────
# Автоматизация доставки
# ─────────────────────────────────────────────────────────────────────────────

class BrowserTradeAutomator:
    """Playwright-слой.

    Roblox не предоставляет публичный HTTP API для MM2-трейда. Поэтому плагин
    разделяет стабильную официальную часть (аккаунт, друзья, presence) и UI-часть.
    UI-часть работает, если на сервере установлен Playwright/браузер и настроены
    селекторы используемой Roblox/MM2-страницы или локальной web-панели автоматизации.
    """

    def __init__(self, settings: PluginSettings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.browser.get("enabled", False))

    async def scan_inventory(self) -> InventorySyncResult:
        inventory_settings = dict(self.settings.inventory_sync or {})
        if not self.enabled:
            return InventorySyncResult(
                ok=False,
                source="browser",
                errors=["Playwright-автоматизация выключена: browser.enabled=false."],
                hints=[
                    "Откройте /mm2 -> Настройки и включите Playwright трейд.",
                    "Установите playwright и браузер на сервере: python3 -m playwright install chromium.",
                    "Заполните inventory_sync.browser_scan_url и selectors под страницу/панель, где виден MM2-инвентарь.",
                ],
            )
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except Exception as exc:
            return InventorySyncResult(
                ok=False,
                source="browser",
                errors=[f"Playwright недоступен: {exc}"],
                hints=[
                    "Установите зависимость playwright в окружение Cardinal.",
                    "После установки выполните: python3 -m playwright install chromium.",
                ],
            )

        scan_url = str(inventory_settings.get("browser_scan_url") or "").strip()
        scan_url = scan_url or self.settings.roblox_vip_server_url or self.settings.roblox_profile_url
        if not scan_url:
            return InventorySyncResult(
                ok=False,
                source="browser",
                errors=["Не указан URL для сканирования инвентаря."],
                hints=[
                    "Заполните inventory_sync.browser_scan_url в settings.json.",
                    "Можно указать URL вашей web-панели/страницы, где отображается MM2-инвентарь аккаунта-бота.",
                ],
            )

        selectors = dict(inventory_settings.get("selectors") or {})
        item_selector = str(selectors.get("inventory_items") or "").strip()
        if not item_selector:
            return InventorySyncResult(
                ok=False,
                source="browser",
                errors=["Не заполнен selector inventory_sync.selectors.inventory_items."],
                hints=["Укажите CSS-селектор строки/карточки предмета в инвентаре MM2."],
            )

        headless = bool(self.settings.browser.get("headless", False))
        slow_mo = int(self.settings.browser.get("slow_mo_ms") or 0)
        launch_timeout = int(self.settings.browser.get("launch_timeout_ms") or 120000)
        wait_seconds = max(1, int(inventory_settings.get("browser_scan_wait_seconds") or 15))
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=headless, slow_mo=slow_mo, timeout=launch_timeout)
                context = await browser.new_context()
                await self._install_cookie(context)
                page = await context.new_page()
                await page.goto(scan_url, wait_until="domcontentloaded", timeout=launch_timeout)
                open_button = str(selectors.get("inventory_open_button") or "").strip()
                if open_button:
                    with contextlib.suppress(Exception):
                        await page.locator(open_button).first.click(timeout=10000)
                await asyncio.sleep(wait_seconds)
                items = await self._extract_inventory_items(page, selectors)
                next_selector = str(selectors.get("inventory_next_page") or "").strip()
                for _ in range(10):
                    if not next_selector or not await self._is_visible(page, next_selector):
                        break
                    await page.locator(next_selector).first.click(timeout=5000)
                    await asyncio.sleep(2)
                    items.extend(await self._extract_inventory_items(page, selectors))
                await context.close()
                await browser.close()
                items = dedupe_inventory_items(items)
                if not items:
                    return InventorySyncResult(
                        ok=False,
                        source="browser",
                        errors=["Инвентарь открыт, но предметы по указанным селекторам не найдены."],
                        hints=[
                            "Проверьте inventory_sync.selectors.inventory_items и inventory_item_name.",
                            "Убедитесь, что аккаунт-бот авторизован и инвентарь MM2 реально виден на странице.",
                            "Roblox/MM2 может не отдавать внутриигровой инвентарь через сайт; тогда используйте ручной импорт списка.",
                        ],
                    )
                return InventorySyncResult(ok=True, source="browser", items=items)
        except PlaywrightTimeoutError as exc:
            return InventorySyncResult(
                ok=False,
                source="browser",
                errors=[f"Playwright timeout: {exc}"],
                hints=["Проверьте доступность страницы, скорость сервера и корректность selector'ов."],
            )
        except Exception as exc:
            return InventorySyncResult(
                ok=False,
                source="browser",
                errors=[f"Ошибка браузерного сканирования: {exc}"],
                hints=[
                    "Проверьте .ROBLOSECURITY, browser_scan_url и selector'ы.",
                    "Если MM2-инвентарь не отображается в браузере на сервере, используйте ручной импорт.",
                ],
            )

    async def deliver(self, order: DeliveryOrder) -> TradeResult:
        if not self.enabled:
            return TradeResult(DeliveryOutcome.AUTOMATION_UNAVAILABLE, "Playwright-автоматизация выключена")
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except Exception as exc:
            return TradeResult(DeliveryOutcome.AUTOMATION_UNAVAILABLE, f"Playwright недоступен: {exc}")

        selectors = dict(self.settings.browser.get("selectors") or {})
        headless = bool(self.settings.browser.get("headless", False))
        slow_mo = int(self.settings.browser.get("slow_mo_ms") or 0)
        launch_timeout = int(self.settings.browser.get("launch_timeout_ms") or 120000)
        server_url = str(order.metadata.get("preferred_join_url") or "").strip()
        server_url = server_url or self.settings.roblox_vip_server_url or self.settings.roblox_profile_url
        if not server_url:
            return TradeResult(DeliveryOutcome.BUYER_NOT_IN_SERVER, "Не указан VIP-сервер или профиль Roblox")

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=headless, slow_mo=slow_mo, timeout=launch_timeout)
                context = await browser.new_context()
                await self._install_cookie(context)
                page = await context.new_page()
                await page.goto(server_url, wait_until="domcontentloaded", timeout=launch_timeout)
                await self._click_join_if_present(page, selectors)
                result = await self._run_trade_script(page, selectors, order)
                await context.close()
                await browser.close()
                return result
        except PlaywrightTimeoutError as exc:
            return TradeResult(DeliveryOutcome.TRADE_TIMEOUT, f"Playwright timeout: {exc}")
        except Exception as exc:
            logger.error("%s: ошибка Playwright-доставки заказа %s: %s", LOGGER_PREFIX, order.order_id, exc)
            logger.debug(traceback.format_exc())
            return TradeResult(DeliveryOutcome.UNKNOWN_ERROR, str(exc))

    async def _install_cookie(self, context: Any) -> None:
        cookie = self.settings.roblox_security_cookie
        if not cookie:
            return
        await context.add_cookies(
            [
                {
                    "name": ".ROBLOSECURITY",
                    "value": cookie,
                    "domain": ".roblox.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
        )

    async def _click_join_if_present(self, page: Any, selectors: Dict[str, str]) -> None:
        join_wait = int(self.settings.browser.get("join_wait_seconds") or 45)
        for key in ("vip_join_button", "profile_join_button"):
            selector = selectors.get(key)
            if not selector:
                continue
            locator = page.locator(selector).first
            with contextlib.suppress(Exception):
                if await locator.count() > 0:
                    await locator.click(timeout=5000)
                    await asyncio.sleep(join_wait)
                    return

    async def _run_trade_script(self, page: Any, selectors: Dict[str, str], order: DeliveryOrder) -> TradeResult:
        privacy_marker = selectors.get("trade_privacy_marker")
        declined_marker = selectors.get("trade_declined_marker")
        success_marker = selectors.get("trade_success_marker")
        trade_button = selectors.get("trade_request_button")
        search_input = selectors.get("trade_search_input")
        player_row = selectors.get("trade_player_row")
        inv_search = selectors.get("inventory_search_input")
        inv_item = selectors.get("inventory_item")
        accept_button = selectors.get("trade_accept_button")
        confirm_button = selectors.get("trade_confirm_button")

        required = [trade_button, search_input, player_row, inv_search, inv_item, accept_button, confirm_button]
        if not all(required):
            return TradeResult(DeliveryOutcome.AUTOMATION_UNAVAILABLE, "Не заполнены селекторы trade UI")

        await page.locator(trade_button).first.click(timeout=10000)
        await page.locator(search_input).first.fill(order.roblox_username, timeout=10000)
        await page.locator(player_row).filter(has_text=order.roblox_username).first.click(timeout=15000)
        await page.locator(inv_search).first.fill(order.item_name, timeout=10000)
        await page.locator(inv_item).filter(has_text=order.item_name).first.dblclick(timeout=15000)
        await page.locator(accept_button).first.click(timeout=10000)
        await page.locator(confirm_button).first.click(timeout=10000)

        deadline = time.monotonic() + self.settings.trade_timeout_seconds
        while time.monotonic() < deadline:
            if success_marker and await self._is_visible(page, success_marker):
                return TradeResult(DeliveryOutcome.SUCCESS)
            if privacy_marker and await self._is_visible(page, privacy_marker):
                return TradeResult(DeliveryOutcome.TRADE_PRIVACY_DISABLED)
            if declined_marker and await self._is_visible(page, declined_marker):
                return TradeResult(DeliveryOutcome.TRADE_DECLINED)
            await asyncio.sleep(1)
        return TradeResult(DeliveryOutcome.TRADE_TIMEOUT)

    async def _is_visible(self, page: Any, selector: str) -> bool:
        with contextlib.suppress(Exception):
            return await page.locator(selector).first.is_visible(timeout=500)
        return False

    async def _extract_inventory_items(self, page: Any, selectors: Dict[str, str]) -> List[InventoryItem]:
        item_selector = str(selectors.get("inventory_items") or "").strip()
        name_selector = str(selectors.get("inventory_item_name") or "").strip()
        qty_selector = str(selectors.get("inventory_item_quantity") or "").strip()
        result: List[InventoryItem] = []
        cards = page.locator(item_selector)
        count = await cards.count()
        for idx in range(count):
            card = cards.nth(idx)
            if name_selector:
                with contextlib.suppress(Exception):
                    name = (await card.locator(name_selector).first.inner_text(timeout=1000)).strip()
                    if name:
                        qty = await self._extract_quantity(card, qty_selector)
                        result.append(InventoryItem(item_name=name, quantity=qty, source="browser"))
                        continue
            with contextlib.suppress(Exception):
                text = (await card.inner_text(timeout=1000)).strip()
                parsed_name, qty = parse_inventory_card_text(text)
                if parsed_name:
                    result.append(InventoryItem(item_name=parsed_name, quantity=qty, source="browser"))
        return result

    async def _extract_quantity(self, card: Any, qty_selector: str) -> int:
        if not qty_selector:
            return 1
        with contextlib.suppress(Exception):
            raw = await card.locator(qty_selector).first.inner_text(timeout=1000)
            match = re.search(r"(\d+)", raw or "")
            if match:
                return max(1, int(match.group(1)))
        return 1


# ─────────────────────────────────────────────────────────────────────────────
# Извлечение данных из событий Cardinal
# ─────────────────────────────────────────────────────────────────────────────

NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


def get_message_text(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("message") or message.get("text") or "").strip()
    text = getattr(message, "text", None)
    if text is not None:
        return str(text).strip()
    return str(message or "").strip()


def get_message_chat_id(message: Any) -> str:
    if isinstance(message, dict):
        return normalize_chat_id(message.get("chat_id") or message.get("chat") or "")
    return normalize_chat_id(getattr(message, "chat_id", "") or getattr(message, "chat", ""))


def get_message_author_id(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("author_id")
    return getattr(message, "author_id", None)


def get_message_chat_name(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("chat_name") or message.get("buyer") or "")
    return str(getattr(message, "chat_name", "") or getattr(message, "buyer", "") or "")


def get_message_id(message: Any) -> str:
    if isinstance(message, dict):
        raw = message.get("id") or message.get("message_id") or ""
    else:
        raw = getattr(message, "id", "") or getattr(message, "message_id", "")
    return str(raw or "")


def get_order_field(order: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(order, dict) and name in order:
            return order.get(name)
        if hasattr(order, name):
            return getattr(order, name)
    return default


def normalize_funpay_username(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def get_cardinal_funpay_username(cardinal: Any) -> str:
    account = getattr(cardinal, "account", None)
    candidates: List[Any] = [
        getattr(account, "username", None),
        getattr(account, "name", None),
        getattr(account, "login", None),
        getattr(account, "nickname", None),
        getattr(cardinal, "username", None),
        getattr(cardinal, "account_username", None),
    ]
    profile = getattr(account, "profile", None)
    if profile is not None:
        candidates.extend([
            getattr(profile, "username", None),
            getattr(profile, "name", None),
        ])
    user = getattr(account, "user", None)
    if user is not None:
        candidates.extend([
            getattr(user, "username", None),
            getattr(user, "name", None),
        ])
    for candidate in candidates:
        normalized = normalize_funpay_username(candidate)
        if normalized:
            return normalized
    return ""


def get_seller_aliases(cardinal: Any, settings: PluginSettings) -> List[str]:
    aliases = [normalize_funpay_username(x) for x in settings.seller_funpay_usernames]
    own = get_cardinal_funpay_username(cardinal)
    if own:
        aliases.append(own)
    return sorted({alias for alias in aliases if alias})


def order_text_blob(payload: Dict[str, Any]) -> str:
    parts = [
        payload.get("description", ""),
        payload.get("raw_title", ""),
        payload.get("category", ""),
        payload.get("subcategory", ""),
        payload.get("game", ""),
        payload.get("lot_title", ""),
    ]
    return "\n".join(str(part or "") for part in parts).lower()


def is_mm2_order_payload(payload: Dict[str, Any], settings: PluginSettings, store: Optional[SQLiteStore] = None) -> bool:
    if not settings.require_mm2_category_match:
        return True
    lot_id = str(payload.get("lot_id") or "")
    if lot_id and store and store.get_mapping(lot_id):
        return True
    blob = order_text_blob(payload)
    return any(keyword and keyword in blob for keyword in settings.mm2_category_keywords)


def should_process_sale_payload(
    payload: Dict[str, Any],
    cardinal: Any,
    settings: PluginSettings,
    store: Optional[SQLiteStore] = None,
) -> Tuple[bool, str]:
    aliases = get_seller_aliases(cardinal, settings)
    buyer = normalize_funpay_username(payload.get("buyer"))
    seller = normalize_funpay_username(payload.get("seller"))
    if settings.ignore_orders_where_buyer_is_me and buyer and buyer in aliases:
        return False, "ignored_own_purchase_buyer_is_me"
    if seller and aliases and seller not in aliases:
        return False, "ignored_not_my_sale_seller_mismatch"
    if not is_mm2_order_payload(payload, settings, store):
        return False, "ignored_not_mm2_category"
    return True, "ok"


def extract_order_payload(cardinal: Any, event: Any, settings: PluginSettings, adapter: FunPayAdapter) -> Optional[Dict[str, Any]]:
    order = getattr(event, "order", event)
    order_id = get_order_field(order, "id", "order_id", "OrderID", default="")
    if not order_id:
        return None
    buyer = str(get_order_field(order, "buyer_username", "buyer", "buyer_name", default="") or "")
    seller = str(get_order_field(order, "seller_username", "seller", "seller_name", "owner_username", default="") or "")
    chat_id = adapter.find_chat_id(buyer, get_order_field(order, "chat_id", "chat", default=""))
    fallback = str(order)
    description = adapter.get_full_order_description(order_id, fallback=fallback)
    lot_id = extract_lot_id(description, settings)
    return {
        "order_id": str(order_id),
        "buyer": buyer,
        "seller": seller,
        "chat_id": chat_id,
        "description": description,
        "lot_id": lot_id,
        "amount": get_order_field(order, "amount", default=1),
        "price": get_order_field(order, "price", default=""),
        "currency": str(get_order_field(order, "currency", default="")),
        "category": str(get_order_field(order, "category", "category_name", "lot_category", default="")),
        "subcategory": str(get_order_field(order, "subcategory", "sub_category", "subcategory_name", default="")),
        "game": str(get_order_field(order, "game", "game_name", "node", default="")),
        "lot_title": str(get_order_field(order, "title", "short_description", "name", default="")),
        "raw_title": fallback,
    }


def extract_lot_id(text: str, settings: PluginSettings) -> str:
    haystack = text or ""
    for pattern in settings.lot_id_patterns:
        try:
            match = re.search(pattern, haystack, flags=re.I | re.M)
        except re.error as exc:
            logger.warning("%s: некорректный regex lot_id_patterns=%s: %s", LOGGER_PREFIX, pattern, exc)
            continue
        if match:
            return str(match.group(1)).strip()
    return ""


def is_delay_request(text: str, settings: PluginSettings) -> bool:
    lowered = (text or "").lower()
    return any(keyword and keyword in lowered for keyword in settings.delay_keywords)


def is_ready_request(text: str, settings: PluginSettings) -> bool:
    lowered = (text or "").lower()
    return any(keyword and keyword in lowered for keyword in settings.ready_keywords)


def is_buyer_join_request(text: str, settings: PluginSettings) -> bool:
    if not settings.buyer_server_join_enabled:
        return False
    lowered = (text or "").lower().strip()
    return any(keyword and keyword in lowered for keyword in settings.buyer_join_keywords)


def is_buyer_menu_request(text: str, settings: PluginSettings) -> bool:
    lowered = (text or "").lower().strip()
    return any(keyword and keyword == lowered for keyword in settings.buyer_menu_keywords)


def is_buyer_back_request(text: str, settings: PluginSettings) -> bool:
    lowered = (text or "").lower().strip()
    return any(keyword and keyword == lowered for keyword in settings.buyer_back_keywords)


def normalize_nickname(text: str) -> str:
    text = (text or "").strip()
    text = text.removeprefix("@").strip()
    text = re.sub(r"https?://(?:www\.)?roblox\.com/users/(\d+)[^\s]*", "", text, flags=re.I).strip()
    return text.split()[0] if text.split() else text


# ─────────────────────────────────────────────────────────────────────────────
# Встроенный справочник и админ-команды
# ─────────────────────────────────────────────────────────────────────────────

ADMIN_GUIDE_SECTIONS: Dict[str, Tuple[str, str]] = {
    "start": (
        "Быстрый старт",
        (
            "1. Скопируйте файл mm2_auto_delivery.py в папку plugins Cardinal.\n"
            "2. Перезапустите Cardinal, чтобы создалась папка storage/plugins/"
            f"{UUID}.\n"
            "3. Откройте settings.json и заполните roblox_security_cookie, "
            "roblox_bot_user_id, roblox_profile_url и roblox_vip_server_url.\n"
            "4. Добавьте маппинг лотов через /mm2_map или напрямую в SQLite.\n"
            "5. В описание каждого лота FunPay добавьте ID: 1001 или [MM2:1001].\n"
            "6. Оплатите тестовый заказ и проверьте, что бот спрашивает Roblox-ник."
        ),
    ),
    "cookies": (
        "Roblox cookie",
        (
            ".ROBLOSECURITY берётся из браузера аккаунта-бота. "
            "Не отправляйте cookie покупателям и не публикуйте settings.json. "
            "Если Roblox вернул 401/403, плагин ставит paused_by_auth=1 и прекращает "
            "авто-действия, чтобы не флудить Roblox и покупателю."
        ),
    ),
    "lots": (
        "Маппинг лотов",
        (
            "Плагин ищет номер лота в полном подробном описании заказа. Поддерживаются шаблоны:\n"
            "- ID: 1001\n"
            "- Lot #1001\n"
            "- Лот №1001\n"
            "- #MM2-1001\n"
            "- [MM2:1001]\n\n"
            "Команда добавления: /mm2_map 1001 | Harvester | gun | 3\n"
            "Команда списка: /mm2_mappings"
        ),
    ),
    "inventory": (
        "Инвентарь и ID",
        (
            "Раздел /mm2 -> Инвентарь нужен, чтобы получить таблицу:\n"
            "ID: 1001 — Harvester\n"
            "ID: 1002 — Icebreaker\n\n"
            "Этот ID вставляется в конец подробного описания лота FunPay:\n"
            "ID: 1001\n\n"
            "Автоскан пробует войти в Roblox по .ROBLOSECURITY и прочитать предметы. "
            "Если MM2-инвентарь не доступен через сайт/API, бот покажет причину и предложит ручной импорт списка."
        ),
    ),
    "states": (
        "Состояния заказа",
        (
            "WAITING_NICK - бот ждёт ник Roblox.\n"
            "WAITING_FRIEND - ник проверен, отправляется или ожидается дружба.\n"
            "WAITING_JOIN - бот ждёт покупателя в MM2.\n"
            "TRADING - идёт попытка трейда.\n"
            "DELAYED - покупатель попросил отложить.\n"
            "COMPLETED - предмет передан.\n"
            "MANUAL_REVIEW - автоматике нужна ручная помощь.\n"
            "FAILED - заказ остановлен после критической ошибки."
        ),
    ),
    "buyer": (
        "Команды покупателя",
        (
            "Покупателю доступны простые текстовые команды в его заказе:\n"
            "- /delay, 'позже', 'отложи', 'завтра' - поставить передачу на паузу.\n"
            "- /ready, 'готов', 'можно' - возобновить доставку.\n"
            "- /joinme, 'зайди ко мне' - попросить бота присоединиться к текущему серверу покупателя.\n"
            "Если покупатель отправляет новый валидный ник в состоянии ожидания, "
            "плагин перепроверит аккаунт и повторит friend request."
        ),
    ),
    "trade": (
        "Трейд и Playwright",
        (
            "Roblox не даёт публичного HTTP API для реального MM2-trade. "
            "Официальными API плагин делает только безопасные операции: проверка ника, "
            "friend request, presence. UI-часть вынесена в BrowserTradeAutomator: "
            "если включить browser.enabled и указать селекторы вашей web-панели/Roblox UI, "
            "плагин выполнит сценарий: join -> trade request -> item search -> accept -> confirm.\n"
            "Если UI-слой недоступен, заказ переводится в MANUAL_REVIEW с понятным логом."
        ),
    ),
    "privacy": (
        "Приватность и лимиты",
        (
            "Обрабатываемые ошибки:\n"
            "- ник не найден: покупатель получает просьбу проверить ник;\n"
            "- лимит друзей 200/200: покупатель получает инструкцию очистить список;\n"
            "- friend request заблокирован privacy: покупатель получает ссылку на профиль бота;\n"
            "- трейды отключены: покупатель получает путь Settings -> Privacy;\n"
            "- AFK/timeout: повторная попытка с лимитом trade_retry_count;\n"
            "- auth error: плагин ставит paused_by_auth и уведомляет администратора."
        ),
    ),
}


ADMIN_RUNBOOKS: Dict[str, Tuple[str, List[str]]] = {
    "auth": (
        "Roblox auth error",
        [
            "Откройте лог Cardinal и найдите строку CRITICAL MM2AutoDelivery.",
            "Проверьте, что аккаунт-бот не разлогинен в браузере.",
            "Скопируйте новый .ROBLOSECURITY только с доверенного устройства.",
            f"Вставьте cookie в storage/plugins/{UUID}/settings.json.",
            "Перезапустите Cardinal или выполните /mm2_reload.",
            "Выполните /mm2_unpause_auth, затем /mm2_diag.",
            "Для незавершённых заказов выполните /mm2_retry FP_ID.",
        ],
    ),
    "nickname": (
        "Покупатель пишет неверный ник",
        [
            "Попросите покупателя скопировать username, не display name.",
            "Username в Roblox содержит только латиницу, цифры и подчёркивание.",
            "Если покупатель прислал ссылку профиля, попросите именно ник текстом.",
            "Проверьте ник вручную на roblox.com/users/profile?username=...",
            "После корректного ника плагин сам повторит проверку и friend request.",
        ],
    ),
    "friends": (
        "Friend request не отправляется",
        [
            "Если ошибка 200/200, покупатель должен удалить друзей.",
            "Если privacy, покупатель должен разрешить friend requests или добавить бота сам.",
            "Проверьте, не забит ли список друзей у аккаунта-бота.",
            "Убедитесь, что bot_user_id в settings.json совпадает с текущим аккаунтом.",
            "После исправления покупатель может снова отправить ник или /ready.",
        ],
    ),
    "join": (
        "Покупатель не заходит на сервер",
        [
            "Проверьте roblox_vip_server_url и права доступа к VIP-серверу.",
            "Если используется публичный сервер, включите use_public_server_if_no_vip.",
            "Если покупатель хочет доставку на своём сервере, он должен зайти в MM2 и написать /joinme.",
            "При /joinme плагин проверит Public server capacity; если сервер полный, покупатель получит инструкцию сменить сервер.",
            "Presence API видит игру, но не всегда раскрывает private server instance.",
            "Если задан expected_game_instance_id, проверьте актуальность ID.",
            "Покупателю можно отправить ссылку из сообщения join_fallback.",
        ],
    ),
    "trade": (
        "Трейд не завершается",
        [
            "Проверьте browser.enabled и установку Playwright, если нужна UI-автоматика.",
            "Проверьте селекторы browser.selectors под вашу страницу/панель.",
            "Если у покупателя отключены трейды, он должен изменить Privacy settings.",
            "Если покупатель AFK, дождитесь /ready и выполните /mm2_retry FP_ID.",
            "Если предмета нет в инвентаре бота, переведите заказ в ручную проверку.",
        ],
    ),
    "mapping": (
        "Лот не распознан",
        [
            "Откройте описание лота FunPay и убедитесь, что там есть ID: 1001.",
            "Проверьте /mm2_mappings, есть ли такой номер в локальной таблице.",
            "Добавьте запись /mm2_map 1001 | Item Name | knife | 1.",
            "Если нужен новый формат номера, добавьте regex в lot_id_patterns.",
            "Заказ без mapping попадает в MANUAL_REVIEW и не теряется.",
        ],
    ),
    "inventory": (
        "Инвентарь не сканируется",
        [
            "Проверьте .ROBLOSECURITY через /mm2 -> Roblox Auth -> Диагностика.",
            "Помните: MM2-предметы внутриигровые, официальный Roblox inventory API обычно их не отдаёт.",
            "Для автосканирования включите browser.enabled и настройте inventory_sync.browser_scan_url.",
            "Заполните CSS-селекторы inventory_sync.selectors под страницу/панель, где виден инвентарь.",
            "Если UI-скан невозможен, используйте /mm2 -> Инвентарь -> Ручной импорт.",
            "После импорта бот сам присвоит ID и создаст mapping для лотов.",
        ],
    ),
    "storage": (
        "SQLite или settings повреждены",
        [
            "Плагин хранит данные в storage/plugins/<UUID>.",
            "Перед ручной правкой остановите Cardinal или сделайте копию файлов.",
            "settings.json при ошибке чтения переименуется в .broken-* и создаётся заново.",
            "SQLite работает в WAL-режиме; копируйте sqlite3 вместе с -wal/-shm при горячем бэкапе.",
            "Список событий заказа смотрите через /mm2_order FP_ID.",
        ],
    ),
}


KNOWN_MM2_ITEMS: Dict[str, List[str]] = {
    "ancient": [
        "Batwing",
        "Elderwood Scythe",
        "Hallowscythe",
        "Harvester",
        "Icebreaker",
        "Icewing",
        "Logchopper",
        "Niks Scythe",
        "Reaver",
        "Swirly Axe",
        "Traveler's Axe",
        "Vampire's Axe",
    ],
    "unique": [
        "Corrupt",
        "Gold Candy",
        "Gold Elderwood",
        "Gold Hallows",
        "Gold Iceblaster",
        "Gold Icebreaker",
        "Gold Luger",
        "Gold Minty",
        "Gold Sugar",
        "Gold Vampires Edge",
        "Silver Candy",
        "Silver Elderwood",
        "Silver Hallows",
        "Silver Iceblaster",
        "Silver Icebreaker",
        "Silver Luger",
        "Silver Minty",
        "Silver Sugar",
        "Silver Vampires Edge",
    ],
    "godly_knife": [
        "Amerilaser",
        "BattleAxe",
        "BattleAxe II",
        "Bioblade",
        "Blaster",
        "Blue Seer",
        "Boneblade",
        "Candleflame",
        "Candy",
        "Chill",
        "Chroma Boneblade",
        "Chroma Candleflame",
        "Chroma Darkbringer",
        "Chroma Deathshard",
        "Chroma Fang",
        "Chroma Gemstone",
        "Chroma Gingerblade",
        "Chroma Heat",
        "Chroma Laser",
        "Chroma Lightbringer",
        "Chroma Luger",
        "Chroma Saw",
        "Chroma Seer",
        "Chroma Shark",
        "Chroma Slasher",
        "Chroma Tides",
        "Clockwork",
        "Cookieblade",
        "Darkbringer",
        "Darkshot",
        "Deathshard",
        "Elderwood Blade",
        "Elderwood Revolver",
        "Eternal",
        "Eternal II",
        "Eternal III",
        "Eternal IV",
        "Fang",
        "Flames",
        "Frostbite",
        "Frostsaber",
        "Gemstone",
        "Ghostblade",
        "Ginger Luger",
        "Gingerblade",
        "Green Luger",
        "Handsaw",
        "Hallows Blade",
        "Hallows Edge",
        "Heat",
        "Heartblade",
        "Ice Dragon",
        "Ice Shard",
        "Iceblaster",
        "Iceflake",
        "Laser",
        "Lightbringer",
        "Luger",
        "Lugercane",
        "Minty",
        "Nebula",
        "Nightblade",
        "Old Glory",
        "Orange Seer",
        "Peppermint",
        "Pixel",
        "Plasmabeam",
        "Plasmablade",
        "Prismatic",
        "Pumpking",
        "Purple Seer",
        "Red Luger",
        "Red Seer",
        "Sakura",
        "Saw",
        "Seer",
        "Shark",
        "Slasher",
        "Spider",
        "Sugar",
        "Swirly Blade",
        "Swirly Gun",
        "Tides",
        "Vampires Edge",
        "Virtual",
        "Watergun",
        "Winters Edge",
        "Xmas",
        "Yellow Seer",
    ],
    "vintage": [
        "America",
        "Blood",
        "Cowboy",
        "Ghost",
        "Golden",
        "Laser Vintage",
        "Phaser",
        "Prince",
        "Shadow",
        "Splitter",
    ],
    "legendary": [
        "Aurora",
        "Blue Elite",
        "Blue Scratch",
        "Cavern Knife",
        "Cavern Gun",
        "Cotton Candy",
        "Elite",
        "Emerald",
        "Fade",
        "Ghost Knife",
        "Ginger Knife",
        "Ginger Gun",
        "Green Elite",
        "JD",
        "Midnight",
        "Overseer Knife",
        "Overseer Gun",
        "Predator Knife",
        "Predator Gun",
        "Ripper Knife",
        "Ripper Gun",
        "Rune",
        "Scratch",
        "Sparkle",
        "Tree Knife",
        "Tree Gun",
        "Web",
    ],
    "rare": [
        "Ace",
        "Bacon",
        "Bit",
        "Black",
        "Bluesteel",
        "Cane Knife",
        "Cane Gun",
        "Cardboard",
        "Cherry",
        "Circuit",
        "Damp",
        "Deep Sea",
        "Doge",
        "Galaxy",
        "Ghosts",
        "Graffiti",
        "Green Marble",
        "Hacker",
        "Hazmat",
        "Ice Camo",
        "Icicles",
        "Krypto",
        "Magma",
        "Molten Knife",
        "Molten Gun",
        "Nether",
        "Nightfire",
        "Orange Marble",
        "Purple",
        "Rainbow",
        "Rainbow Gun",
        "Squire",
        "Viper",
        "Watcher",
    ],
    "uncommon": [
        "Adurite",
        "Biogun",
        "Blue",
        "Brush",
        "Camo",
        "Cheddar",
        "Clown Knife",
        "Clown Gun",
        "Cold",
        "Combat II",
        "Copper",
        "High Tech",
        "Jigsaw",
        "Linked",
        "Lovely",
        "Marina",
        "Missing",
        "Paper",
        "Pea",
        "Pinky",
        "Pirate",
        "Sandy",
        "Shaded",
        "Slate",
        "Tiger",
        "Wanwood",
        "Wooden",
    ],
    "common": [
        "Aqua",
        "Borders",
        "Brown",
        "Carved",
        "Checker",
        "Coal",
        "Combat",
        "Default Knife",
        "Default Gun",
        "Eco",
        "Engraved Knife",
        "Engraved Gun",
        "Hardened",
        "HL2",
        "Leaf",
        "Love",
        "News",
        "Orange",
        "Pink",
        "Reptile",
        "Shiny",
        "Skool",
        "Splat",
        "Stainless",
        "Static",
        "Steel",
        "Yellow",
    ],
}


class ItemNameHelper:
    """Поиск и нормализация названий MM2-предметов для администратора."""

    def __init__(self, catalog: Optional[Dict[str, List[str]]] = None) -> None:
        self.catalog = catalog or KNOWN_MM2_ITEMS
        self._flat: List[Tuple[str, str]] = []
        for category, names in self.catalog.items():
            for name in names:
                self._flat.append((category, name))

    def find(self, query: str, limit: int = 20) -> List[Tuple[str, str]]:
        needle = self._normalize(query)
        if not needle:
            return self._flat[:limit]
        scored: List[Tuple[int, str, str]] = []
        for category, name in self._flat:
            hay = self._normalize(name)
            score = self._score(needle, hay)
            if score > 0:
                scored.append((score, category, name))
        scored.sort(key=lambda item: (-item[0], item[2].lower()))
        return [(category, name) for _, category, name in scored[:limit]]

    def suggest_category(self, item_name: str) -> str:
        normalized = self._normalize(item_name)
        for category, name in self._flat:
            if self._normalize(name) == normalized:
                return category
        matches = self.find(item_name, limit=1)
        return matches[0][0] if matches else ""

    def format_search(self, query: str) -> str:
        matches = self.find(query)
        title = f"MM2 items for '{query}':" if query else "Популярные MM2 items:"
        lines = [title]
        if not matches:
            lines.append("Ничего не найдено. Можно всё равно добавить точное название через /mm2_map.")
            return "\n".join(lines)
        for category, name in matches:
            lines.append(f"{name} [{category}]")
        return "\n".join(lines)

    def _normalize(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    def _score(self, needle: str, hay: str) -> int:
        if needle == hay:
            return 100
        if hay.startswith(needle):
            return 80
        if needle in hay:
            return 60
        # Простая subsequence-проверка помогает находить "ew scythe" -> Elderwood Scythe.
        pos = 0
        for char in needle:
            found = hay.find(char, pos)
            if found < 0:
                return 0
            pos = found + 1
        return 30


class OrderFormatter:
    """Единое форматирование текстов для админ-команд и логов."""

    @staticmethod
    def short_order(order: DeliveryOrder) -> str:
        roblox = order.roblox_username or "-"
        return (
            f"#{order.order_id} {order.state.value} | "
            f"{order.lot_id}:{order.item_name or '-'} | "
            f"buyer={order.buyer or '-'} | roblox={roblox}"
        )

    @staticmethod
    def detailed_order(order: DeliveryOrder, events: Sequence[Dict[str, Any]]) -> str:
        lines = [
            f"Заказ #{order.order_id}",
            f"state: {order.state.value}",
            f"buyer: {order.buyer or '-'}",
            f"chat_id: {order.chat_id or '-'}",
            f"lot_id: {order.lot_id or '-'}",
            f"item_name: {order.item_name or '-'}",
            f"roblox_username: {order.roblox_username or '-'}",
            f"roblox_user_id: {order.roblox_user_id or '-'}",
            f"retry_count: {order.retry_count}",
            f"trade_attempts: {order.trade_attempts}",
            f"last_error: {order.last_error or '-'}",
            f"created_at: {order.created_at}",
            f"updated_at: {order.updated_at}",
            "",
            "metadata:",
        ]
        if order.metadata:
            for key in sorted(order.metadata):
                value = order.metadata[key]
                lines.append(f"  {key}: {str(value)[:120]}")
        else:
            lines.append("  -")
        lines.extend(["", "Последние события:"])
        if not events:
            lines.append("  -")
        for event in events:
            lines.append(
                f"  {event.get('created_at')} {event.get('state')} "
                f"{event.get('event_type')}: {str(event.get('message') or '-')[:120]}"
            )
        return "\n".join(lines)

    @staticmethod
    def mappings_table(mappings: Sequence[ItemMapping], limit: int = 60) -> str:
        if not mappings:
            return "Маппинг пуст. Добавьте: /mm2_map 1001 | Harvester | gun | 1"
        lines = ["Таблица лотов MM2:"]
        for item in mappings[:limit]:
            enabled = "on" if item.enabled else "off"
            stock = f" stock={item.stock}" if item.stock else ""
            category = f" [{item.category}]" if item.category else ""
            notes = f" - {item.notes[:40]}" if item.notes else ""
            lines.append(f"{item.lot_id}: {item.item_name}{category}{stock} {enabled}{notes}")
        if len(mappings) > limit:
            lines.append(f"... и ещё {len(mappings) - limit}")
        return "\n".join(lines)

    @staticmethod
    def active_orders(title: str, orders: Sequence[DeliveryOrder]) -> str:
        lines = [title]
        if not orders:
            lines.append("Нет заказов.")
            return "\n".join(lines)
        for order in orders:
            lines.append(OrderFormatter.short_order(order))
        return "\n".join(lines)


class MappingCatalogIO:
    """Импорт/экспорт таблицы соответствия в компактном JSON."""

    VERSION = 1

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def export_json(self) -> str:
        payload = {
            "version": self.VERSION,
            "exported_at": utc_now(),
            "items": [
                {
                    "lot_id": item.lot_id,
                    "item_name": item.item_name,
                    "category": item.category,
                    "stock": item.stock,
                    "enabled": item.enabled,
                    "notes": item.notes,
                }
                for item in self.store.list_mappings()
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def import_json(self, raw: str) -> Tuple[int, List[str]]:
        try:
            payload = json.loads(raw)
        except Exception as exc:
            return 0, [f"JSON parse error: {exc}"]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("items") or []
        else:
            return 0, ["JSON должен быть объектом или списком"]
        imported = 0
        errors: List[str] = []
        for idx, entry in enumerate(items):
            if not isinstance(entry, dict):
                errors.append(f"#{idx}: запись не объект")
                continue
            lot_id = str(entry.get("lot_id") or "").strip()
            item_name = str(entry.get("item_name") or "").strip()
            if not lot_id or not item_name:
                errors.append(f"#{idx}: lot_id/item_name обязательны")
                continue
            stock = 0
            with contextlib.suppress(Exception):
                stock = int(entry.get("stock") or 0)
            self.store.upsert_mapping(
                ItemMapping(
                    lot_id=lot_id,
                    item_name=item_name,
                    category=str(entry.get("category") or ""),
                    stock=max(0, stock),
                    enabled=bool(entry.get("enabled", True)),
                    notes=str(entry.get("notes") or ""),
                )
            )
            imported += 1
        return imported, errors

    def export_lines_for_chat(self, max_chars: int = 3500) -> str:
        raw = self.export_json()
        if len(raw) <= max_chars:
            return raw
        mappings = self.store.list_mappings()
        lines = ["JSON слишком большой для одного сообщения. Краткий экспорт:"]
        for item in mappings:
            lines.append(f"/mm2_map {item.lot_id} | {item.item_name} | {item.category} | {item.stock}")
            if sum(len(line) + 1 for line in lines) > max_chars:
                lines.append("...обрезано")
                break
        return "\n".join(lines)


class HealthInspector:
    """Собирает эксплуатационную диагностику без изменения состояния заказов."""

    def __init__(self, settings_getter: Callable[[], PluginSettings], store: SQLiteStore, roblox: RobloxApiClient) -> None:
        self._settings_getter = settings_getter
        self.store = store
        self.roblox = roblox

    @property
    def settings(self) -> PluginSettings:
        return self._settings_getter()

    async def build_report(self) -> str:
        settings = self.settings
        lines = [
            "Диагностика MM2 AutoDelivery:",
            f"version: {VERSION}",
            f"enabled: {settings.enabled}",
            f"paused_by_auth: {self.store.get_flag('paused_by_auth', '0')}",
            f"storage_dir: {STORAGE_DIR}",
            f"db_exists: {os.path.exists(DB_FILE)}",
            f"settings_exists: {os.path.exists(SETTINGS_FILE)}",
            f"log_exists: {os.path.exists(LOG_FILE)}",
            "",
            "Roblox:",
            f"  cookie: {'set' if settings.roblox_security_cookie else 'missing'}",
            f"  bot_user_id: {settings.roblox_bot_user_id or '-'}",
            f"  bot_username: {settings.roblox_bot_username or '-'}",
            f"  place_id: {settings.roblox_place_id or '-'}",
            f"  expected_game_instance_id: {settings.expected_game_instance_id or '-'}",
            f"  profile_url: {'set' if settings.roblox_profile_url else 'missing'}",
            f"  vip_server_url: {'set' if settings.roblox_vip_server_url else 'missing'}",
            "",
            "Delivery:",
            f"  delivery_timeout_minutes: {settings.delivery_timeout_minutes}",
            f"  presence_poll_seconds: {settings.presence_poll_seconds}",
            f"  trade_timeout_seconds: {settings.trade_timeout_seconds}",
            f"  trade_retry_count: {settings.trade_retry_count}",
            "",
            "Browser:",
            f"  enabled: {bool(settings.browser.get('enabled'))}",
            f"  headless: {bool(settings.browser.get('headless'))}",
        ]
        lines.extend(self._storage_summary_lines())
        if settings.roblox_security_cookie:
            lines.extend(await self._roblox_auth_lines())
        else:
            lines.append("Roblox auth: skipped, cookie missing")
        return "\n".join(lines)

    def _storage_summary_lines(self) -> List[str]:
        mappings = self.store.list_mappings()
        lines = [
            "",
            "Storage:",
            f"  mappings: {len(mappings)}",
        ]
        for state in [OrderState.WAITING_NICK, OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN, OrderState.TRADING, OrderState.DELAYED, OrderState.MANUAL_REVIEW]:
            lines.append(f"  {state.value}: {len(self.store.list_orders_by_states([state], limit=1000))}")
        return lines

    async def _roblox_auth_lines(self) -> List[str]:
        lines = ["", "Roblox auth:"]
        try:
            user = await self.roblox.validate_session()
            lines.append(f"  OK: {user.username} ({user.user_id})")
            if self.settings.roblox_bot_user_id and user.user_id != self.settings.roblox_bot_user_id:
                lines.append(f"  WARNING: settings bot id {self.settings.roblox_bot_user_id} != cookie user {user.user_id}")
        except Exception as exc:
            lines.append(f"  ERROR: {exc}")
        return lines

    def stale_orders_report(self, max_age_minutes: int = 30) -> str:
        # ISO-строки UTC сортируются лексикографически; для подробного отчёта достаточно
        # показать активные записи, которые давно не обновлялись.
        active = self.store.list_orders_by_states(
            [OrderState.WAITING_NICK, OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN, OrderState.TRADING],
            limit=200,
        )
        now_ts = time.time()
        lines = [f"Активные заказы старше {max_age_minutes} минут:"]
        found = False
        for order in active:
            age = self._age_seconds(order.updated_at, now_ts)
            if age >= max_age_minutes * 60:
                found = True
                lines.append(f"{OrderFormatter.short_order(order)} | idle={int(age // 60)}m")
        if not found:
            lines.append("Нет зависших заказов.")
        return "\n".join(lines)

    def _age_seconds(self, iso_value: str, now_ts: float) -> float:
        try:
            dt = datetime.fromisoformat(iso_value)
            return max(0.0, now_ts - dt.timestamp())
        except Exception:
            return 0.0


class InventorySynchronizer:
    """Синхронизация инвентаря аккаунта-бота с локальной таблицей ID лотов."""

    def __init__(
        self,
        settings_getter: Callable[[], PluginSettings],
        store: SQLiteStore,
        roblox: RobloxApiClient,
        automator: BrowserTradeAutomator,
    ) -> None:
        self._settings_getter = settings_getter
        self.store = store
        self.roblox = roblox
        self.automator = automator

    @property
    def settings(self) -> PluginSettings:
        return self._settings_getter()

    async def sync(self) -> InventorySyncResult:
        settings = self.settings
        inv_settings = dict(settings.inventory_sync or {})
        if not inv_settings.get("enabled", True):
            return InventorySyncResult(
                ok=False,
                source="disabled",
                errors=["Синхронизация инвентаря выключена: inventory_sync.enabled=false."],
                hints=["Включите inventory_sync.enabled через settings.json."],
            )
        if not settings.roblox_security_cookie:
            return InventorySyncResult(
                ok=False,
                source="auth",
                errors=["Не заполнен roblox_security_cookie (.ROBLOSECURITY)."],
                hints=[
                    "В Telegram откройте /mm2 -> Настройки -> Cookie и вставьте актуальный .ROBLOSECURITY.",
                    "После сохранения нажмите Roblox Auth / Диагностика.",
                ],
            )
        try:
            user = await self.roblox.validate_session()
            if settings.roblox_bot_user_id and user.user_id != settings.roblox_bot_user_id:
                return InventorySyncResult(
                    ok=False,
                    source="auth",
                    errors=[f"Cookie принадлежит аккаунту {user.username} ({user.user_id}), а в настройках bot_user_id={settings.roblox_bot_user_id}."],
                    hints=["Обновите roblox_bot_user_id или вставьте cookie нужного аккаунта-бота."],
                )
        except RobloxAuthError as exc:
            return InventorySyncResult(
                ok=False,
                source="auth",
                errors=[f"Roblox отклонил cookie: {exc}"],
                hints=[
                    "Cookie устарел или скопирован не полностью.",
                    "Войдите в Roblox под аккаунтом-ботом и заново скопируйте .ROBLOSECURITY.",
                ],
            )
        except Exception as exc:
            return InventorySyncResult(
                ok=False,
                source="auth",
                errors=[f"Не удалось проверить Roblox-сессию: {exc}"],
                hints=["Проверьте сеть сервера и доступ к users.roblox.com."],
            )

        source_order = [str(x) for x in inv_settings.get("source_order") or ["browser", "roblox_collectibles"]]
        attempted: List[InventorySyncResult] = []
        for source in source_order:
            if source == "browser":
                result = await self.automator.scan_inventory()
            elif source == "roblox_collectibles":
                result = await self._sync_collectibles_source()
            else:
                result = InventorySyncResult(ok=False, source=source, errors=[f"Неизвестный источник inventory_sync: {source}"])
            attempted.append(result)
            if result.ok and result.items:
                return self._persist_result(result)

        errors: List[str] = []
        hints: List[str] = [
            "MM2-инвентарь является внутриигровым и обычно не доступен через официальный Roblox inventory API.",
            "Для автоматического чтения настройте browser.enabled=true, inventory_sync.browser_scan_url и CSS-селекторы inventory_sync.selectors.",
            "Если UI-скан невозможен, используйте кнопку ручного импорта инвентаря и вставьте названия предметов списком.",
        ]
        for result in attempted:
            errors.extend([f"{result.source}: {err}" for err in result.errors])
            hints.extend(result.hints)
        return InventorySyncResult(ok=False, source="auto", errors=dedupe_strings(errors), hints=dedupe_strings(hints))

    async def _sync_collectibles_source(self) -> InventorySyncResult:
        if not bool((self.settings.inventory_sync or {}).get("roblox_collectibles_enabled", True)):
            return InventorySyncResult(ok=False, source="roblox_collectibles", errors=["Источник roblox_collectibles выключен."])
        try:
            user_id = int(self.settings.roblox_bot_user_id or 0)
            items = await self.roblox.get_collectibles_inventory(user_id)
            mm2_items = self._filter_mm2_like_items(items)
            if not mm2_items:
                return InventorySyncResult(
                    ok=False,
                    source="roblox_collectibles",
                    errors=["Roblox collectibles API не вернул MM2-предметы."],
                    hints=[
                        "Это ожидаемо: ножи/пистолеты MM2 - внутриигровые предметы, а не Roblox collectible assets.",
                        "Используйте browser scan или ручной импорт.",
                    ],
                )
            return InventorySyncResult(ok=True, source="roblox_collectibles", items=mm2_items)
        except RobloxAuthError as exc:
            return InventorySyncResult(ok=False, source="roblox_collectibles", errors=[f"Auth error: {exc}"])
        except Exception as exc:
            return InventorySyncResult(ok=False, source="roblox_collectibles", errors=[str(exc)])

    def manual_import(self, raw: str) -> InventorySyncResult:
        items = parse_manual_inventory_items(raw)
        if not items:
            return InventorySyncResult(
                ok=False,
                source="manual",
                errors=["Не найдено ни одного предмета в тексте."],
                hints=["Вставьте список: Harvester, Icebreaker x2, Corrupt. Можно по одному предмету на строку."],
            )
        return self._persist_result(InventorySyncResult(ok=True, source="manual", items=items))

    def _persist_result(self, result: InventorySyncResult) -> InventorySyncResult:
        inv_settings = dict(self.settings.inventory_sync or {})
        updated, created = self.store.upsert_inventory_items(
            result.items,
            auto_mapping=bool(inv_settings.get("auto_create_lot_mapping", True)),
            lot_id_start=int(inv_settings.get("lot_id_start") or 1001),
        )
        result.updated_inventory = updated
        result.created_mappings = created
        result.items = self.store.list_inventory_items(limit=500)
        return result

    def _filter_mm2_like_items(self, items: Sequence[InventoryItem]) -> List[InventoryItem]:
        helper = ItemNameHelper()
        known = {normalize_item_name(name) for names in KNOWN_MM2_ITEMS.values() for name in names}
        result: List[InventoryItem] = []
        for item in items:
            normalized = normalize_item_name(item.item_name)
            lower_name = item.item_name.lower()
            if normalized in known or "mm2" in lower_name or "murder mystery" in lower_name:
                item.category = item.category or helper.suggest_category(item.item_name)
                result.append(item)
        return result

    def format_inventory_ids(self, limit: int = 120) -> str:
        items = self.store.list_inventory_items(limit=limit)
        if not items:
            return (
                "Инвентарь пуст.\n"
                "Нажмите «🔄 Сканировать инвентарь» или «✍️ Ручной импорт»."
            )
        lines = ["ID предметов для описаний FunPay:", ""]
        for item in items:
            stock = f" x{item.quantity}" if item.quantity else ""
            lot = item.lot_id or "-"
            lines.append(f"ID: {lot} — {item.item_name}{stock}")
        lines.append("")
        lines.append("В конец подробного описания лота вставляйте строку, например: ID: 1001")
        return "\n".join(lines)

    def format_funpay_description_hint(self, item_name: str = "") -> str:
        item: Optional[InventoryItem] = None
        if item_name:
            normalized = normalize_item_name(item_name)
            for current in self.store.list_inventory_items(limit=500):
                if normalize_item_name(current.item_name) == normalized:
                    item = current
                    break
        if not item:
            items = self.store.list_inventory_items(limit=1)
            item = items[0] if items else None
        if not item:
            return "Сначала синхронизируйте или импортируйте инвентарь."
        return (
            "Пример конца подробного описания лота:\n\n"
            "▰▰▰▰▰▰▰▰▰▰▰\n"
            f"ID: {item.lot_id}\n\n"
            f"Этот ID соответствует предмету: {item.item_name}"
        )


def parse_manual_inventory_items(raw: str) -> List[InventoryItem]:
    chunks = re.split(r"[\n;,]+", raw or "")
    items: List[InventoryItem] = []
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        name, qty = parse_inventory_card_text(text)
        if name:
            items.append(InventoryItem(item_name=name, quantity=qty, source="manual", category=ItemNameHelper().suggest_category(name)))
    return dedupe_inventory_items(items)


def dedupe_strings(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


class AdminCommandRouter:
    """Небольшая FunPay-панель управления без зависимости от Telegram-кнопок."""

    def __init__(
        self,
        settings_getter: Callable[[], PluginSettings],
        store: SQLiteStore,
        funpay: FunPayAdapter,
        roblox: RobloxApiClient,
        resume_callback: Callable[[str], None],
        reload_callback: Callable[[], None],
        state_machine: OrderStateMachine,
    ) -> None:
        self._settings_getter = settings_getter
        self.store = store
        self.funpay = funpay
        self.roblox = roblox
        self.resume_callback = resume_callback
        self.reload_callback = reload_callback
        self.state_machine = state_machine
        self.catalog = MappingCatalogIO(store)
        self.health = HealthInspector(settings_getter, store, roblox)
        self.item_names = ItemNameHelper()

    @property
    def settings(self) -> PluginSettings:
        return self._settings_getter()

    def is_admin_chat(self, chat_id: Any) -> bool:
        admin_chat = normalize_chat_id(self.settings.admin_funpay_chat_id)
        if not admin_chat:
            return False
        return normalize_chat_id(chat_id) == admin_chat

    async def handle(self, chat_id: Any, text: str) -> bool:
        if not text.startswith("/mm2"):
            return False
        command, args = self._split_command(text)
        if command in ("/mm2", "/mm2_help"):
            self._send_help(chat_id, args)
            return True
        if not self.is_admin_chat(chat_id):
            self.funpay.send_message(
                chat_id,
                "Команды /mm2 доступны только администратору. Для заказа используйте /ready или /delay.",
                scope=f"admin_denied:{chat_id}",
            )
            return True
        handlers: Dict[str, Callable[[Any, str], Any]] = {
            "/mm2_status": self._cmd_status,
            "/mm2_mappings": self._cmd_mappings,
            "/mm2_map": self._cmd_map,
            "/mm2_delmap": self._cmd_delmap,
            "/mm2_order": self._cmd_order,
            "/mm2_delay": self._cmd_delay,
            "/mm2_ready": self._cmd_ready,
            "/mm2_manual": self._cmd_manual,
            "/mm2_retry": self._cmd_retry,
            "/mm2_diag": self._cmd_diag,
            "/mm2_reload": self._cmd_reload,
            "/mm2_unpause_auth": self._cmd_unpause_auth,
            "/mm2_guide": self._cmd_guide,
            "/mm2_runbook": self._cmd_runbook,
            "/mm2_export_maps": self._cmd_export_maps,
            "/mm2_import_maps": self._cmd_import_maps,
            "/mm2_stale": self._cmd_stale,
            "/mm2_items": self._cmd_items,
        }
        handler = handlers.get(command)
        if not handler:
            self.funpay.send_message(chat_id, "Неизвестная команда. Используйте /mm2_help.", scope=f"unknown_cmd:{chat_id}")
            return True
        result = handler(chat_id, args)
        if asyncio.iscoroutine(result):
            await result
        return True

    def _split_command(self, text: str) -> Tuple[str, str]:
        parts = (text or "").strip().split(maxsplit=1)
        if not parts:
            return "", ""
        return parts[0].lower(), parts[1] if len(parts) > 1 else ""

    def _send_help(self, chat_id: Any, args: str = "") -> None:
        if args:
            self._cmd_guide(chat_id, args)
            return
        text = (
            "MM2 Auto Delivery команды:\n"
            "/mm2_help - краткая помощь\n"
            "/mm2_guide start|cookies|lots|states|buyer|trade|privacy - справочник\n"
            "/mm2_status - состояние плагина\n"
            "/mm2_mappings - список лотов\n"
            "/mm2_map 1001 | Harvester | gun | 3 - добавить/обновить лот\n"
            "/mm2_delmap 1001 - удалить лот\n"
            "/mm2_items harv - поиск названия предмета\n"
            "/mm2_export_maps - экспорт таблицы лотов JSON\n"
            "/mm2_import_maps {json} - импорт таблицы лотов JSON\n"
            "/mm2_order FP_ID - карточка заказа\n"
            "/mm2_stale - зависшие активные заказы\n"
            "/mm2_delay FP_ID - отложить заказ\n"
            "/mm2_ready FP_ID - вернуть заказ в работу\n"
            "/mm2_retry FP_ID - повторить доставку\n"
            "/mm2_manual FP_ID - ручная проверка\n"
            "/mm2_diag - диагностика Roblox\n"
            "/mm2_reload - перечитать settings.json\n"
            "/mm2_unpause_auth - снять паузу после обновления cookie\n"
            "/mm2_runbook auth|nickname|friends|join|trade|mapping|storage - инструкции"
        )
        self.funpay.send_message(chat_id, text, scope=f"help:{chat_id}")

    def _cmd_guide(self, chat_id: Any, args: str) -> None:
        key = (args or "start").strip().lower()
        section = ADMIN_GUIDE_SECTIONS.get(key)
        if not section:
            keys = ", ".join(sorted(ADMIN_GUIDE_SECTIONS))
            self.funpay.send_message(chat_id, f"Раздел не найден. Доступно: {keys}", scope=f"guide_missing:{chat_id}")
            return
        title, body = section
        self.funpay.send_message(chat_id, f"{title}\n\n{body}", scope=f"guide:{chat_id}:{key}")

    def _cmd_runbook(self, chat_id: Any, args: str) -> None:
        key = (args or "auth").strip().lower()
        section = ADMIN_RUNBOOKS.get(key)
        if not section:
            keys = ", ".join(sorted(ADMIN_RUNBOOKS))
            self.funpay.send_message(chat_id, f"Runbook не найден. Доступно: {keys}", scope=f"runbook_missing:{chat_id}")
            return
        title, steps = section
        lines = [title, ""]
        lines.extend(f"{idx}. {step}" for idx, step in enumerate(steps, start=1))
        self.funpay.send_message(chat_id, "\n".join(lines), scope=f"runbook:{chat_id}:{key}")

    def _cmd_status(self, chat_id: Any, args: str) -> None:
        settings = self.settings
        states = [OrderState.WAITING_NICK, OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN, OrderState.TRADING, OrderState.DELAYED, OrderState.MANUAL_REVIEW]
        counts: List[str] = []
        for state in states:
            count = len(self.store.list_orders_by_states([state], limit=1000))
            counts.append(f"{state.value}: {count}")
        paused = self.store.get_flag("paused_by_auth", "0")
        text = (
            f"MM2 AutoDelivery v{VERSION}\n"
            f"enabled: {settings.enabled}\n"
            f"paused_by_auth: {paused}\n"
            f"roblox_bot: {settings.roblox_bot_username or settings.roblox_bot_user_id or 'не задан'}\n"
            f"place_id: {settings.roblox_place_id}\n"
            f"browser.enabled: {bool(settings.browser.get('enabled'))}\n"
            f"VIP URL: {'задан' if settings.roblox_vip_server_url else 'не задан'}\n\n"
            + "\n".join(counts)
        )
        self.funpay.send_message(chat_id, text, scope=f"status:{chat_id}")

    def _cmd_mappings(self, chat_id: Any, args: str) -> None:
        self.funpay.send_message(chat_id, OrderFormatter.mappings_table(self.store.list_mappings()), scope=f"maps:{chat_id}")

    def _cmd_map(self, chat_id: Any, args: str) -> None:
        item = self._parse_mapping_args(args)
        if not item:
            self.funpay.send_message(chat_id, "Формат: /mm2_map 1001 | Harvester | gun | 3", scope=f"map_usage:{chat_id}")
            return
        if not item.category:
            item.category = self.item_names.suggest_category(item.item_name)
        self.store.upsert_mapping(item)
        self.funpay.send_message(chat_id, f"Лот {item.lot_id} -> {item.item_name} сохранён.", scope=f"map_saved:{chat_id}:{item.lot_id}")

    def _parse_mapping_args(self, args: str) -> Optional[ItemMapping]:
        raw = (args or "").strip()
        if not raw:
            return None
        if "|" in raw:
            parts = [p.strip() for p in raw.split("|")]
        else:
            parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            return None
        lot_id = parts[0].strip()
        item_name = parts[1].strip()
        if not lot_id or not item_name:
            return None
        category = parts[2].strip() if len(parts) > 2 else ""
        stock = 0
        if len(parts) > 3:
            with contextlib.suppress(Exception):
                stock = int(parts[3])
        return ItemMapping(lot_id=lot_id, item_name=item_name, category=category, stock=max(0, stock), enabled=True)

    def _cmd_delmap(self, chat_id: Any, args: str) -> None:
        lot_id = (args or "").strip().split()[0] if (args or "").strip() else ""
        if not lot_id:
            self.funpay.send_message(chat_id, "Формат: /mm2_delmap 1001", scope=f"delmap_usage:{chat_id}")
            return
        deleted = self.store.delete_mapping(lot_id)
        self.funpay.send_message(chat_id, "Удалено." if deleted else "Лот не найден.", scope=f"delmap:{chat_id}:{lot_id}")

    def _cmd_items(self, chat_id: Any, args: str) -> None:
        self.funpay.send_message(chat_id, self.item_names.format_search(args.strip()), scope=f"items:{chat_id}:{args[:30]}")

    def _cmd_export_maps(self, chat_id: Any, args: str) -> None:
        self.funpay.send_message(chat_id, self.catalog.export_lines_for_chat(), scope=f"export_maps:{chat_id}:{int(time.time())}")

    def _cmd_import_maps(self, chat_id: Any, args: str) -> None:
        raw = (args or "").strip()
        if not raw:
            self.funpay.send_message(chat_id, "Формат: /mm2_import_maps {\"items\":[...]}", scope=f"import_usage:{chat_id}")
            return
        imported, errors = self.catalog.import_json(raw)
        lines = [f"Импортировано: {imported}"]
        if errors:
            lines.append("Ошибки:")
            lines.extend(errors[:10])
        self.funpay.send_message(chat_id, "\n".join(lines), scope=f"import_result:{chat_id}:{int(time.time())}")

    def _cmd_order(self, chat_id: Any, args: str) -> None:
        order_id = (args or "").strip().split()[0] if (args or "").strip() else ""
        if not order_id:
            self.funpay.send_message(chat_id, "Формат: /mm2_order FP_ID", scope=f"order_usage:{chat_id}")
            return
        order = self.store.get_order(order_id)
        if not order:
            self.funpay.send_message(chat_id, "Заказ не найден в MM2-хранилище.", scope=f"order_missing:{chat_id}:{order_id}")
            return
        events = self.store.get_recent_events(order_id, limit=5)
        self.funpay.send_message(chat_id, OrderFormatter.detailed_order(order, events), scope=f"order:{chat_id}:{order_id}")

    def _cmd_stale(self, chat_id: Any, args: str) -> None:
        minutes = 30
        if args.strip():
            with contextlib.suppress(Exception):
                minutes = max(1, int(args.strip().split()[0]))
        self.funpay.send_message(chat_id, self.health.stale_orders_report(minutes), scope=f"stale:{chat_id}:{minutes}")

    def _cmd_delay(self, chat_id: Any, args: str) -> None:
        order = self._get_order_arg(chat_id, args)
        if not order:
            return
        if order.state in TERMINAL_STATES:
            self.funpay.send_message(chat_id, "Нельзя отложить завершённый заказ.", scope=f"delay_terminal:{chat_id}:{order.order_id}")
            return
        order.metadata["state_before_delay"] = order.state.value
        self.state_machine.transition(order, OrderState.DELAYED, "admin_delay", "Администратор отложил заказ")
        self.funpay.send_message(chat_id, f"Заказ #{order.order_id} отложен.", scope=f"delay_ok:{chat_id}:{order.order_id}")

    def _cmd_ready(self, chat_id: Any, args: str) -> None:
        order = self._get_order_arg(chat_id, args)
        if not order:
            return
        target = OrderState.WAITING_JOIN if order.roblox_user_id else OrderState.WAITING_NICK
        if order.state == OrderState.MANUAL_REVIEW and order.roblox_user_id:
            target = OrderState.WAITING_JOIN
        if order.state in TERMINAL_STATES and order.state != OrderState.MANUAL_REVIEW:
            self.funpay.send_message(chat_id, "Завершённый заказ нельзя вернуть без ручного изменения БД.", scope=f"ready_terminal:{chat_id}:{order.order_id}")
            return
        self.state_machine.transition(order, target, "admin_ready", "Администратор вернул заказ в работу")
        self.funpay.send_message(chat_id, f"Заказ #{order.order_id} переведён в {target.value}.", scope=f"ready_ok:{chat_id}:{order.order_id}")
        self.resume_callback(order.order_id)

    def _cmd_manual(self, chat_id: Any, args: str) -> None:
        order = self._get_order_arg(chat_id, args)
        if not order:
            return
        if order.state in TERMINAL_STATES and order.state != OrderState.FAILED:
            self.funpay.send_message(chat_id, "Заказ уже в финальном состоянии.", scope=f"manual_terminal:{chat_id}:{order.order_id}")
            return
        self.state_machine.transition(order, OrderState.MANUAL_REVIEW, "admin_manual", "Администратор перевёл в ручную проверку")
        self.funpay.send_message(chat_id, f"Заказ #{order.order_id} переведён в MANUAL_REVIEW.", scope=f"manual_ok:{chat_id}:{order.order_id}")

    def _cmd_retry(self, chat_id: Any, args: str) -> None:
        order = self._get_order_arg(chat_id, args)
        if not order:
            return
        if order.state == OrderState.DELAYED:
            self.funpay.send_message(chat_id, "Заказ отложен. Сначала /mm2_ready FP_ID.", scope=f"retry_delayed:{chat_id}:{order.order_id}")
            return
        if order.state in TERMINAL_STATES and order.state != OrderState.MANUAL_REVIEW:
            self.funpay.send_message(chat_id, "Финальный заказ нельзя повторить.", scope=f"retry_terminal:{chat_id}:{order.order_id}")
            return
        if not order.roblox_user_id:
            self.state_machine.transition(order, OrderState.WAITING_NICK, "admin_retry_wait_nick", "Нет Roblox user id")
            self.funpay.send_message(chat_id, f"Заказ #{order.order_id}: нет Roblox ID, жду ник.", scope=f"retry_nick:{chat_id}:{order.order_id}")
            return
        if order.state == OrderState.MANUAL_REVIEW:
            self.state_machine.transition(order, OrderState.WAITING_JOIN, "admin_retry", "Повтор доставки из ручной проверки")
        self.resume_callback(order.order_id)
        self.funpay.send_message(chat_id, f"Повтор доставки заказа #{order.order_id} запущен.", scope=f"retry_ok:{chat_id}:{order.order_id}")

    async def _cmd_diag(self, chat_id: Any, args: str) -> None:
        self.funpay.send_message(chat_id, await self.health.build_report(), scope=f"diag:{chat_id}:{int(time.time())}")

    def _cmd_reload(self, chat_id: Any, args: str) -> None:
        self.reload_callback()
        self.funpay.send_message(chat_id, "settings.json перечитан.", scope=f"reload:{chat_id}:{int(time.time())}")

    def _cmd_unpause_auth(self, chat_id: Any, args: str) -> None:
        self.store.set_flag("paused_by_auth", "0")
        self.reload_callback()
        self.funpay.send_message(chat_id, "Пауза auth снята. Запустите /mm2_diag и /mm2_retry FP_ID.", scope=f"unpause:{chat_id}:{int(time.time())}")

    def _get_order_arg(self, chat_id: Any, args: str) -> Optional[DeliveryOrder]:
        order_id = (args or "").strip().split()[0] if (args or "").strip() else ""
        if not order_id:
            self.funpay.send_message(chat_id, "Укажите ID заказа FunPay.", scope=f"order_arg_usage:{chat_id}")
            return None
        order = self.store.get_order(order_id)
        if not order:
            self.funpay.send_message(chat_id, f"Заказ #{order_id} не найден.", scope=f"order_arg_missing:{chat_id}:{order_id}")
            return None
        return order


# ─────────────────────────────────────────────────────────────────────────────
# Координатор доставки
# ─────────────────────────────────────────────────────────────────────────────

class DeliveryCoordinator:
    def __init__(self, cardinal: Any, config: Optional[Dict[str, Any]] = None) -> None:
        self.cardinal = cardinal
        self.settings = PluginSettings.load(config or {})
        self.store = SQLiteStore()
        self.store.seed_examples_if_empty()
        self.state_machine = OrderStateMachine(self.store)
        self.funpay = FunPayAdapter(cardinal, self.settings, self.store)
        self.roblox = RobloxApiClient(self.settings)
        self.automator = BrowserTradeAutomator(self.settings)
        self.inventory = InventorySynchronizer(lambda: self.settings, self.store, self.roblox, self.automator)
        self.admin_commands = AdminCommandRouter(
            settings_getter=lambda: self.settings,
            store=self.store,
            funpay=self.funpay,
            roblox=self.roblox,
            resume_callback=lambda order_id: self.enqueue_delivery(order_id, "admin_resume"),
            reload_callback=self.reload_settings,
            state_machine=self.state_machine,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        self._tasks: "queue.Queue[Tuple[Callable[..., Any], Tuple[Any, ...], Dict[str, Any]]]" = queue.Queue()
        self._delivery_queue: Optional[asyncio.Queue[str]] = None
        self._delivery_queue_lock = threading.RLock()
        self._queued_delivery_order_ids: Set[str] = set()
        self._running_delivery_order_ids: Set[str] = set()
        self._pending_delivery_order_ids: List[str] = []
        self._started = False
        self._paused_by_auth = self.store.get_flag("paused_by_auth", "0") == "1"

    def reload_settings(self) -> None:
        self.settings = PluginSettings.load()
        self.funpay.settings = self.settings
        self.roblox.settings = self.settings
        self.automator.settings = self.settings
        self._paused_by_auth = self.store.get_flag("paused_by_auth", "0") == "1"
        logger.info("%s: settings.json перечитан", LOGGER_PREFIX)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._shutdown.clear()
        self._thread = threading.Thread(target=self._run_loop_thread, name="MM2AutoDeliveryLoop", daemon=True)
        self._thread.start()
        logger.info("%s: координатор запущен", LOGGER_PREFIX)
        self.submit(self._startup_checks)

    def stop(self) -> None:
        self._shutdown.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("%s: координатор остановлен", LOGGER_PREFIX)

    def submit(self, coro_func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if self._shutdown.is_set():
            return
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._safe_coro(coro_func, *args, **kwargs)))
        else:
            self._tasks.put((coro_func, args, kwargs))

    def enqueue_delivery(self, order_id: str, reason: str = "") -> bool:
        order_id = str(order_id or "").strip()
        if not order_id or self._shutdown.is_set():
            return False
        with self._delivery_queue_lock:
            if order_id in self._queued_delivery_order_ids or order_id in self._running_delivery_order_ids:
                logger.info("%s: заказ %s уже в очереди/работе (%s)", LOGGER_PREFIX, order_id, reason)
                return False
            self._queued_delivery_order_ids.add(order_id)
            if self._loop and self._loop.is_running() and self._delivery_queue is not None:
                self._loop.call_soon_threadsafe(self._delivery_queue.put_nowait, order_id)
            else:
                self._pending_delivery_order_ids.append(order_id)
        logger.info("%s: заказ %s добавлен в очередь доставки (%s)", LOGGER_PREFIX, order_id, reason or "manual")
        return True

    def _run_loop_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._delivery_queue = asyncio.Queue()
        with self._delivery_queue_lock:
            for order_id in self._pending_delivery_order_ids:
                self._delivery_queue.put_nowait(order_id)
            self._pending_delivery_order_ids.clear()
        for worker_idx in range(max(1, self.settings.delivery_queue_workers)):
            self._loop.create_task(self._delivery_worker(worker_idx + 1))
        while not self._tasks.empty():
            coro_func, args, kwargs = self._tasks.get()
            self._loop.create_task(self._safe_coro(coro_func, *args, **kwargs))
        self._loop.create_task(self._periodic_resume_worker())
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _delivery_worker(self, worker_id: int) -> None:
        while not self._shutdown.is_set():
            if self._delivery_queue is None:
                await asyncio.sleep(0.2)
                continue
            order_id = await self._delivery_queue.get()
            with self._delivery_queue_lock:
                self._queued_delivery_order_ids.discard(order_id)
                self._running_delivery_order_ids.add(order_id)
            try:
                logger.info("%s: worker %s начал доставку заказа %s", LOGGER_PREFIX, worker_id, order_id)
                await self.delivery_loop(order_id)
            except Exception as exc:
                logger.error("%s: worker %s ошибка доставки %s: %s", LOGGER_PREFIX, worker_id, order_id, exc)
                logger.debug(traceback.format_exc())
            finally:
                with self._delivery_queue_lock:
                    self._running_delivery_order_ids.discard(order_id)
                self._delivery_queue.task_done()

    async def _safe_coro(self, coro_func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            result = coro_func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.error("%s: необработанная ошибка фоновой задачи: %s", LOGGER_PREFIX, exc)
            logger.debug(traceback.format_exc())

    async def _startup_checks(self) -> None:
        if not self.settings.enabled:
            logger.warning("%s: плагин выключен в settings.json", LOGGER_PREFIX)
            return
        if not self.roblox.configured:
            logger.warning("%s: .ROBLOSECURITY не настроен; заказы будут приниматься, но доставка встанет на ручную проверку", LOGGER_PREFIX)
            return
        try:
            user = await self.roblox.validate_session()
            logger.info("%s: Roblox-сессия активна: %s (%s)", LOGGER_PREFIX, user.username, user.user_id)
            if self._paused_by_auth:
                self._paused_by_auth = False
                self.store.set_flag("paused_by_auth", "0")
        except RobloxAuthError as exc:
            await self._pause_for_auth_error(f"Roblox-сессия недействительна при старте: {exc}")

    async def _periodic_resume_worker(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(30)
            if self._paused_by_auth or not self.settings.enabled:
                continue
            active = self.store.list_orders_by_states([OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN, OrderState.TRADING], limit=25)
            for order in active:
                self.enqueue_delivery(order.order_id, "periodic_resume")

    def on_new_order(self, event: Any) -> None:
        if not self.settings.enabled:
            return
        self.submit(self.handle_new_order, event)

    def on_message_event(self, event: Any) -> None:
        if not self.settings.enabled:
            return
        self.submit(self.handle_message_event, event)

    def on_legacy_message(self, data: Any) -> None:
        if not self.settings.enabled:
            return
        self.submit(self.handle_message_object, data)

    async def handle_new_order(self, event: Any) -> None:
        payload = extract_order_payload(self.cardinal, event, self.settings, self.funpay)
        if not payload:
            return
        order_id = payload["order_id"]
        should_process, reason = should_process_sale_payload(payload, self.cardinal, self.settings, self.store)
        if not should_process:
            logger.info("%s: заказ %s пропущен: %s buyer=%s seller=%s", LOGGER_PREFIX, order_id, reason, payload.get("buyer"), payload.get("seller"))
            return
        existing = self.store.get_order(order_id)
        if existing:
            logger.info("%s: заказ %s уже существует в MM2-хранилище", LOGGER_PREFIX, order_id)
            return
        lot_id = payload.get("lot_id", "")
        if not lot_id:
            await self._handle_unknown_lot(payload, "В описании заказа не найден номер лота")
            return
        mapping = self.store.get_mapping(lot_id)
        if not mapping or not mapping.enabled:
            await self._handle_unknown_lot(payload, f"Не найден активный mapping для lot_id={lot_id}")
            return
        order = DeliveryOrder(
            order_id=order_id,
            chat_id=payload.get("chat_id", ""),
            buyer=payload.get("buyer", ""),
            lot_id=lot_id,
            item_name=mapping.item_name,
            state=OrderState.WAITING_NICK,
            metadata={
                "amount": payload.get("amount"),
                "price": payload.get("price"),
                "currency": payload.get("currency"),
                "raw_title": payload.get("raw_title"),
                "category": mapping.category,
            },
        )
        created = self.store.create_order(order)
        if not created:
            return
        self.funpay.send_message(order.chat_id, self.settings.message("welcome"), scope=f"welcome:{order.order_id}")
        logger.info("%s: новый заказ %s lot=%s item=%s buyer=%s", LOGGER_PREFIX, order.order_id, lot_id, order.item_name, order.buyer)

    async def _handle_unknown_lot(self, payload: Dict[str, Any], reason: str) -> None:
        order_id = payload.get("order_id", "")
        chat_id = payload.get("chat_id", "")
        order = DeliveryOrder(
            order_id=str(order_id),
            chat_id=str(chat_id),
            buyer=str(payload.get("buyer", "")),
            lot_id=str(payload.get("lot_id", "")),
            item_name="",
            state=OrderState.MANUAL_REVIEW,
            last_error=reason,
            metadata=payload,
        )
        self.store.create_order(order)
        if chat_id:
            self.funpay.send_message(chat_id, self.settings.message("unknown_lot"), scope=f"unknown_lot:{order_id}")
        self.funpay.notify_admin(f"Заказ #{order_id} требует ручной проверки: {reason}")
        logger.warning("%s: заказ %s переведён в ручную проверку: %s", LOGGER_PREFIX, order_id, reason)

    async def handle_message_event(self, event: Any) -> None:
        message = getattr(event, "message", event)
        await self.handle_message_object(message)

    async def handle_message_object(self, message: Any) -> None:
        text = get_message_text(message)
        if not text:
            return
        chat_id = get_message_chat_id(message)
        buyer = get_message_chat_name(message)
        message_id = get_message_id(message)
        key = f"{chat_id}:{message_id or abs(hash(text))}"
        if self.store.was_message_seen(key, chat_id, text):
            return
        if await self.admin_commands.handle(chat_id, text):
            return
        order = self.store.get_active_order_by_chat(chat_id, buyer)
        if not order:
            return
        if self.settings.telegram_mirror_funpay_messages:
            self.funpay.notify_admin(
                f"FunPay MM2 order #{order.order_id}\n"
                f"Покупатель: {order.buyer or buyer or '-'}\n"
                f"Incoming:\n{text}",
                force_telegram=True,
                allow_funpay=False,
            )
        if await self.handle_buyer_menu(order, text):
            return
        if is_buyer_join_request(text, self.settings):
            await self.request_join_buyer_server(order)
            return
        if is_delay_request(text, self.settings):
            await self.delay_order(order)
            return
        if is_ready_request(text, self.settings):
            await self.ready_order(order)
            return
        if order.state == OrderState.WAITING_NICK:
            await self.handle_nickname(order, text)
        elif order.state in (OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN):
            nickname = normalize_nickname(text)
            if nickname and NICK_RE.match(nickname):
                await self.handle_nickname(order, text)
            else:
                self.funpay.send_message(order.chat_id, self.settings.message("waiting_join", item_name=order.item_name, server_url=self.settings.roblox_vip_server_url), scope=f"wait_join:{order.order_id}")

    async def handle_buyer_menu(self, order: DeliveryOrder, text: str) -> bool:
        normalized = (text or "").strip().lower()
        if is_buyer_menu_request(text, self.settings) or normalized in {"0", "меню"}:
            self.show_buyer_menu(order, "main")
            return True
        if is_buyer_back_request(text, self.settings):
            previous = str(order.metadata.get("buyer_menu_previous") or "main")
            self.show_buyer_menu(order, previous if previous != "current" else "main")
            return True
        if normalized in {"1", "ник", "roblox", "роблокс"}:
            self.show_buyer_menu(order, "nick")
            return True
        if normalized in {"2", "join", "joinme", "сервер"}:
            self.show_buyer_menu(order, "joinme")
            return True
        if normalized in {"3", "отложить"}:
            await self.delay_order(order)
            return True
        if normalized in {"4", "готов"}:
            await self.ready_order(order)
            return True
        if normalized in {"5", "статус", "status"}:
            self.funpay.send_message(
                order.chat_id,
                self.settings.message(
                    "buyer_status",
                    state=order.state.value,
                    item_name=order.item_name,
                    roblox_username=order.roblox_username or "ещё не указан",
                ),
                scope=f"buyer_status:{order.order_id}:{int(time.time())}",
            )
            return True
        return False

    def show_buyer_menu(self, order: DeliveryOrder, page: str = "main") -> None:
        previous = str(order.metadata.get("buyer_menu_current") or "main")
        order.metadata["buyer_menu_previous"] = previous
        order.metadata["buyer_menu_current"] = page
        self.store.update_order(order, event_type="buyer_menu", message=f"menu={page}")
        if page == "nick":
            text = self.settings.message("buyer_menu_nick")
        elif page == "joinme":
            text = self.settings.message("buyer_menu_joinme")
        else:
            text = self.settings.message("buyer_menu_main")
        self.funpay.send_message(order.chat_id, text, scope=f"buyer_menu:{order.order_id}:{page}:{int(time.time())}")

    async def request_join_buyer_server(self, order: DeliveryOrder) -> None:
        if not order.roblox_user_id:
            self.funpay.send_message(order.chat_id, self.settings.message("joinme_need_nick"), scope=f"joinme_need_nick:{order.order_id}")
            return
        if self._paused_by_auth:
            self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"joinme_auth:{order.order_id}")
            return
        try:
            presence = await self.roblox.get_presence(order.roblox_user_id)
        except RobloxAuthError as exc:
            await self._pause_for_auth_error(f"Roblox auth error during /joinme: {exc}")
            self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"joinme_auth:{order.order_id}")
            return
        except Exception as exc:
            order.last_error = f"/joinme presence error: {exc}"
            self.store.update_order(order, event_type="joinme_presence_error", message=order.last_error)
            self.funpay.send_message(order.chat_id, "Roblox временно не отвечает. Попробуйте /joinme ещё раз через минуту.", scope=f"joinme_presence:{order.order_id}")
            return

        if not presence or not presence.in_game or not presence.game_id:
            self.funpay.send_message(order.chat_id, self.settings.message("joinme_no_presence"), scope=f"joinme_no_presence:{order.order_id}")
            return
        if not self._presence_is_mm2(presence):
            self.funpay.send_message(order.chat_id, self.settings.message("joinme_no_presence"), scope=f"joinme_wrong_game:{order.order_id}")
            return

        place_id = presence.place_id or presence.root_place_id or self.settings.roblox_place_id
        capacity = await self.roblox.get_public_server_capacity(place_id, presence.game_id)
        order.metadata["preferred_join_game_id"] = presence.game_id
        order.metadata["preferred_join_place_id"] = place_id
        order.metadata["preferred_join_url"] = self._build_public_join_url(place_id, presence.game_id)
        order.metadata["preferred_join_requested_at"] = utc_now()

        if capacity and capacity.is_full:
            order.last_error = f"buyer_server_full {capacity.playing}/{capacity.max_players}"
            self.store.update_order(order, event_type="joinme_server_full", message=order.last_error)
            self.funpay.send_message(
                order.chat_id,
                self.settings.message("joinme_server_full", playing=capacity.playing, max_players=capacity.max_players),
                scope=f"joinme_full:{order.order_id}:{presence.game_id}",
            )
            self.funpay.notify_admin(
                f"Заказ #{order.order_id}: покупатель {order.roblox_username} попросил /joinme, "
                f"но сервер полный {capacity.playing}/{capacity.max_players}."
            )
            return

        if capacity and capacity.known:
            message = self.settings.message(
                "joinme_joining",
                playing=capacity.playing,
                max_players=capacity.max_players,
                item_name=order.item_name,
            )
            event_message = f"/joinme server {presence.game_id} capacity {capacity.playing}/{capacity.max_players}"
        else:
            message = self.settings.message("joinme_private_unknown")
            event_message = f"/joinme server {presence.game_id} capacity unknown"

        self.state_machine.transition(order, OrderState.WAITING_JOIN, "joinme_requested", event_message)
        self.funpay.send_message(order.chat_id, message, scope=f"joinme_joining:{order.order_id}:{presence.game_id}")
        self.funpay.notify_admin(
            f"Заказ #{order.order_id}: запускаю доставку на сервер покупателя.\n"
            f"Buyer: {order.roblox_username} ({order.roblox_user_id})\n"
            f"Item: {order.item_name}\n"
            f"GameId: {presence.game_id}\n"
            f"Join URL: {order.metadata.get('preferred_join_url')}"
        )
        self.enqueue_delivery(order.order_id, "buyer_joinme")

    def _build_public_join_url(self, place_id: int, game_id: str) -> str:
        params = urllib.parse.urlencode({"placeId": int(place_id or self.settings.roblox_place_id), "gameInstanceId": str(game_id)})
        return f"https://www.roblox.com/games/start?{params}"

    async def delay_order(self, order: DeliveryOrder) -> None:
        if order.state in TERMINAL_STATES:
            return
        order.metadata["state_before_delay"] = order.state.value
        self.state_machine.transition(order, OrderState.DELAYED, "delayed_by_buyer", "Покупатель попросил отложить")
        self.funpay.send_message(order.chat_id, self.settings.message("delayed"), scope=f"delayed:{order.order_id}")

    async def ready_order(self, order: DeliveryOrder) -> None:
        if order.state == OrderState.DELAYED:
            previous = str(order.metadata.get("state_before_delay") or "")
            if previous in (OrderState.WAITING_FRIEND.value, OrderState.WAITING_JOIN.value, OrderState.TRADING.value):
                target = OrderState.WAITING_JOIN
            elif order.roblox_username:
                target = OrderState.WAITING_FRIEND
            else:
                target = OrderState.WAITING_NICK
            self.state_machine.transition(order, target, "ready_by_buyer", "Покупатель готов")
        self.funpay.send_message(order.chat_id, self.settings.message("ready"), scope=f"ready:{order.order_id}")
        if order.roblox_user_id:
            self.enqueue_delivery(order.order_id, "buyer_ready")

    async def handle_nickname(self, order: DeliveryOrder, text: str) -> None:
        nickname = normalize_nickname(text)
        if not self._valid_nickname(nickname):
            self.funpay.send_message(
                order.chat_id,
                self.settings.message("invalid_nick_format", min_len=self.settings.nickname_min_len, max_len=self.settings.nickname_max_len),
                scope=f"invalid_nick:{order.order_id}",
            )
            return
        try:
            user = await self.roblox.get_user_by_username(nickname)
        except RobloxAuthError as exc:
            await self._pause_for_auth_error(f"Roblox auth error while validating {nickname}: {exc}")
            self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"auth:{order.order_id}")
            return
        except Exception as exc:
            order.last_error = f"Ошибка проверки ника: {exc}"
            self.store.update_order(order, event_type="nickname_lookup_error", message=order.last_error)
            self.funpay.send_message(order.chat_id, "Roblox временно не отвечает. Попробуйте отправить ник ещё раз через минуту.", scope=f"nick_lookup:{order.order_id}")
            return
        if not user:
            self.funpay.send_message(order.chat_id, self.settings.message("nick_not_found", nickname=nickname), scope=f"nick_not_found:{order.order_id}:{nickname}")
            return
        order.roblox_username = user.username
        order.roblox_user_id = user.user_id
        order.metadata["roblox_display_name"] = user.display_name
        self.state_machine.transition(order, OrderState.WAITING_FRIEND, "nickname_validated", f"Roblox user {user.username} ({user.user_id})")
        await self.send_friend_request_and_start(order)

    def _valid_nickname(self, nickname: str) -> bool:
        if len(nickname) < self.settings.nickname_min_len or len(nickname) > self.settings.nickname_max_len:
            return False
        return bool(NICK_RE.match(nickname))

    async def send_friend_request_and_start(self, order: DeliveryOrder) -> None:
        if self._paused_by_auth:
            self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"auth_paused:{order.order_id}")
            return
        try:
            result = await self.roblox.send_friend_request(order.roblox_user_id)
        except RobloxAuthError as exc:
            await self._pause_for_auth_error(f"Сессия Roblox устарела при friend request: {exc}")
            self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"auth:{order.order_id}")
            return
        except Exception as exc:
            order.last_error = f"Ошибка friend request: {exc}"
            self.store.update_order(order, event_type="friend_request_error", message=order.last_error)
            self.funpay.send_message(order.chat_id, "Не удалось отправить запрос в друзья из-за ошибки Roblox. Попробую позже.", scope=f"friend_err:{order.order_id}")
            self.enqueue_delivery(order.order_id, "friend_request_retry")
            return

        if result.outcome == FriendRequestOutcome.SENT:
            order.metadata["friend_request_sent_at"] = utc_now()
            self.store.update_order(order, event_type="friend_request_sent", message=result.message)
            self.funpay.send_message(order.chat_id, self.settings.message("friend_sent", nickname=order.roblox_username), scope=f"friend_sent:{order.order_id}")
        elif result.outcome == FriendRequestOutcome.ALREADY_FRIENDS:
            self.store.update_order(order, event_type="already_friends", message=result.message)
            self.funpay.send_message(order.chat_id, self.settings.message("already_friends", nickname=order.roblox_username), scope=f"already_friends:{order.order_id}")
        elif result.outcome == FriendRequestOutcome.BUYER_FRIEND_LIMIT:
            order.last_error = "buyer_friend_limit"
            self.state_machine.transition(order, OrderState.WAITING_NICK, "friend_limit", result.message)
            self.funpay.send_message(order.chat_id, self.settings.message("friends_limit"), scope=f"friends_limit:{order.order_id}")
            return
        elif result.outcome == FriendRequestOutcome.PRIVACY_BLOCKED:
            order.last_error = "friend_privacy_blocked"
            self.store.update_order(order, event_type="friend_privacy", message=result.message)
            self.funpay.send_message(order.chat_id, self.settings.message("friend_privacy", profile_url=self.settings.roblox_profile_url), scope=f"friend_privacy:{order.order_id}")
        elif result.outcome == FriendRequestOutcome.AUTH_ERROR:
            await self._pause_for_auth_error(f"Roblox auth error during friend request: {result.message}")
            self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"auth:{order.order_id}")
            return
        else:
            order.last_error = result.message
            self.store.update_order(order, event_type="friend_unknown_result", message=result.message)

        if self.settings.auto_start_delivery_after_friend_request:
            self.enqueue_delivery(order.order_id, "friend_request_done")

    async def delivery_loop(self, order_id: str) -> None:
        order = self.store.get_order(order_id)
        if not order or order.state in TERMINAL_STATES or order.state == OrderState.DELAYED:
            return
        if not order.roblox_user_id:
            return
        if self._paused_by_auth:
            self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"auth_paused:{order.order_id}")
            return

        deadline = time.monotonic() + self.settings.delivery_timeout_minutes * 60
        last_wait_message = 0.0
        while time.monotonic() < deadline:
            current = self.store.get_order(order_id)
            if not current or current.state in TERMINAL_STATES or current.state == OrderState.DELAYED:
                return
            order = current
            try:
                presence = await self.roblox.get_presence(order.roblox_user_id)
            except RobloxAuthError as exc:
                await self._pause_for_auth_error(f"Roblox auth error during presence check: {exc}")
                self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"auth:{order.order_id}")
                return
            except Exception as exc:
                order.last_error = f"Presence error: {exc}"
                self.store.update_order(order, event_type="presence_error", message=order.last_error)
                await asyncio.sleep(self.settings.presence_poll_seconds)
                continue

            if presence and self._presence_matches_order_target(order, presence):
                await self._attempt_trade(order)
                return

            if time.monotonic() - last_wait_message > self.settings.message_antispam_seconds:
                last_wait_message = time.monotonic()
                self.state_machine.transition(order, OrderState.WAITING_JOIN, "waiting_join", "Покупатель ещё не на сервере")
                self.funpay.send_message(
                    order.chat_id,
                    self.settings.message("waiting_join", item_name=order.item_name, server_url=self.settings.roblox_vip_server_url),
                    scope=f"waiting_join:{order.order_id}",
                )
            await asyncio.sleep(self.settings.presence_poll_seconds)

        order = self.store.get_order(order_id)
        if order and order.state not in TERMINAL_STATES and order.state != OrderState.DELAYED:
            order.retry_count += 1
            order.last_error = "delivery_timeout_join"
            self.state_machine.transition(order, OrderState.WAITING_JOIN, "join_timeout", "Покупатель не зашёл на сервер вовремя")
            self.funpay.send_message(
                order.chat_id,
                self.settings.message(
                    "join_fallback",
                    profile_url=self.settings.roblox_profile_url,
                    server_url=self.settings.roblox_vip_server_url,
                ),
                scope=f"join_fallback:{order.order_id}:{order.retry_count}",
            )

    def _presence_matches_delivery_server(self, presence: RobloxPresence) -> bool:
        if not presence.in_game:
            return False
        expected_place = int(self.settings.roblox_place_id or 0)
        if expected_place and presence.place_id not in (expected_place, 0) and presence.root_place_id not in (expected_place, 0):
            return False
        expected_instance = self.settings.expected_game_instance_id
        if expected_instance:
            return presence.game_id == expected_instance
        return True

    def _presence_matches_order_target(self, order: DeliveryOrder, presence: RobloxPresence) -> bool:
        preferred_game_id = str(order.metadata.get("preferred_join_game_id") or "")
        if preferred_game_id:
            return self._presence_is_mm2(presence) and presence.game_id == preferred_game_id
        return self._presence_matches_delivery_server(presence)

    def _presence_is_mm2(self, presence: RobloxPresence) -> bool:
        expected_place = int(self.settings.roblox_place_id or 0)
        if not expected_place:
            return presence.in_game
        return presence.in_game and (
            presence.place_id in (expected_place, 0)
            or presence.root_place_id in (expected_place, 0)
        )

    async def _attempt_trade(self, order: DeliveryOrder) -> None:
        order.trade_attempts += 1
        self.state_machine.transition(order, OrderState.TRADING, "trade_started", f"Попытка трейда #{order.trade_attempts}")
        self.funpay.send_message(order.chat_id, self.settings.message("trade_started", item_name=order.item_name), scope=f"trade_started:{order.order_id}:{order.trade_attempts}")

        for attempt in range(1, self.settings.trade_retry_count + 1):
            result = await self.automator.deliver(order)
            if result.outcome == DeliveryOutcome.SUCCESS:
                await self._complete_order(order)
                return
            if result.outcome == DeliveryOutcome.TRADE_PRIVACY_DISABLED:
                order.last_error = result.message or "trade_privacy_disabled"
                self.state_machine.transition(order, OrderState.WAITING_JOIN, "trade_privacy", order.last_error)
                self.funpay.send_message(order.chat_id, self.settings.message("trade_privacy"), scope=f"trade_privacy:{order.order_id}")
                return
            if result.outcome == DeliveryOutcome.TRADE_DECLINED:
                order.last_error = result.message or "trade_declined"
                self.state_machine.transition(order, OrderState.WAITING_JOIN, "trade_declined", order.last_error)
                self.funpay.send_message(order.chat_id, self.settings.message("trade_declined"), scope=f"trade_declined:{order.order_id}")
                return
            if result.outcome == DeliveryOutcome.TRADE_TIMEOUT:
                order.last_error = result.message or "trade_timeout"
                self.store.update_order(order, event_type="trade_timeout", message=order.last_error)
                self.funpay.send_message(order.chat_id, self.settings.message("trade_timeout"), scope=f"trade_timeout:{order.order_id}:{attempt}")
                await asyncio.sleep(self.settings.trade_retry_delay_seconds)
                continue
            if result.outcome == DeliveryOutcome.AUTH_ERROR:
                await self._pause_for_auth_error(result.message)
                self.funpay.send_message(order.chat_id, self.settings.message("auth_paused"), scope=f"auth:{order.order_id}")
                return

            order.last_error = result.message or result.outcome.value
            self.store.update_order(order, event_type="trade_error", message=order.last_error)
            if result.outcome == DeliveryOutcome.AUTOMATION_UNAVAILABLE:
                break
            await asyncio.sleep(self.settings.trade_retry_delay_seconds)

        order.retry_count += 1
        self.state_machine.transition(order, OrderState.MANUAL_REVIEW, "trade_failed_manual", order.last_error)
        self.funpay.send_message(order.chat_id, self.settings.message("manual_required", item_name=order.item_name), scope=f"manual:{order.order_id}")
        self.funpay.notify_admin(f"Заказ #{order.order_id}: автоматический трейд не завершён. Предмет: {order.item_name}. Ошибка: {order.last_error}")

    async def _complete_order(self, order: DeliveryOrder) -> None:
        self.state_machine.transition(order, OrderState.COMPLETED, "completed", f"Предмет {order.item_name} передан")
        self.store.adjust_stock(order.lot_id, -1)
        self.funpay.send_message(order.chat_id, self.settings.message("completed", item_name=order.item_name), scope=f"completed:{order.order_id}")
        logger.info("%s: заказ %s завершён, item=%s", LOGGER_PREFIX, order.order_id, order.item_name)

    async def _pause_for_auth_error(self, reason: str) -> None:
        logger.critical("%s: %s", LOGGER_PREFIX, reason)
        self.store.set_flag("paused_by_auth", "1")
        self._paused_by_auth = True
        if self.settings.pause_on_auth_error:
            self.funpay.notify_admin(
                "Сессия Roblox-бота устарела или отклонена. "
                "Обновите roblox_security_cookie в storage/plugins/"
                f"{UUID}/settings.json и перезапустите Cardinal. Причина: {reason}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram inline-панель Cardinal
# ─────────────────────────────────────────────────────────────────────────────

TG_STATE_LABELS: Dict[str, str] = {
    "mm2_set_cookie": ".ROBLOSECURITY cookie",
    "mm2_set_bot_id": "Roblox bot user id",
    "mm2_set_bot_username": "Roblox bot username",
    "mm2_set_profile_url": "Roblox profile URL",
    "mm2_set_vip_url": "VIP server URL",
    "mm2_set_place_id": "Roblox place id",
    "mm2_set_game_instance": "Game instance id",
    "mm2_set_admin_chat": "FunPay admin chat id",
    "mm2_set_seller_names": "FunPay seller usernames",
    "mm2_set_delivery_timeout": "Delivery timeout minutes",
    "mm2_set_presence_poll": "Presence poll seconds",
    "mm2_set_trade_timeout": "Trade timeout seconds",
    "mm2_set_trade_retries": "Trade retry count",
    "mm2_add_mapping": "lot mapping",
    "mm2_search_item": "item search",
    "mm2_import_maps": "mapping JSON import",
    "mm2_manual_inventory": "manual inventory import",
    "mm2_inventory_hint": "inventory item hint",
    "mm2_order_open": "FunPay order id",
    "mm2_order_retry": "FunPay order id",
    "mm2_order_delay": "FunPay order id",
    "mm2_order_ready": "FunPay order id",
    "mm2_order_manual": "FunPay order id",
    "mm2_buyer_message": "buyer message",
}


def _tg_available() -> bool:
    return InlineKeyboardButton is not None and InlineKeyboardMarkup is not None


def _html(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _bool_icon(value: Any) -> str:
    return "🟢" if bool(value) else "🔴"


def _tg_back_main_keyboard() -> Any:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("⬅️ Главное меню", callback_data="mm2_back_main"))
    return kb


def _tg_main_keyboard(settings: Dict[str, Any]) -> Any:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("⚙️ Настройки", callback_data="mm2_settings"),
        InlineKeyboardButton("🎒 Инвентарь", callback_data="mm2_inventory"),
    )
    kb.row(
        InlineKeyboardButton("📦 Лоты/ID", callback_data="mm2_mappings"),
        InlineKeyboardButton("📋 Заказы", callback_data="mm2_orders"),
    )
    kb.row(
        InlineKeyboardButton("🧾 Ручная проверка", callback_data="mm2_orders_manual"),
        InlineKeyboardButton("🏥 Диагностика", callback_data="mm2_diag"),
    )
    kb.row(
        InlineKeyboardButton("🔐 Roblox Auth", callback_data="mm2_auth"),
        InlineKeyboardButton("📖 Гайд", callback_data="mm2_guide"),
    )
    kb.row(
        InlineKeyboardButton("🛟 Runbook", callback_data="mm2_runbooks"),
        InlineKeyboardButton("🔄 Reload", callback_data="mm2_reload"),
    )
    kb.add(InlineKeyboardButton(f"{_bool_icon(settings.get('enabled'))} Вкл/выкл", callback_data="mm2_toggle_enabled"))
    return kb


def _tg_settings_keyboard(settings: Dict[str, Any]) -> Any:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(f"{_bool_icon(settings.get('enabled'))} Плагин включён", callback_data="mm2_toggle_enabled"),
        InlineKeyboardButton(f"{_bool_icon(get_setting_path(settings, 'browser.enabled'))} Playwright трейд", callback_data="mm2_toggle_browser"),
        InlineKeyboardButton(f"{_bool_icon(settings.get('buyer_server_join_enabled'))} Покупатель /joinme", callback_data="mm2_toggle_joinme"),
        InlineKeyboardButton(f"{_bool_icon(settings.get('telegram_mirror_funpay_messages'))} Копии FP в Telegram", callback_data="mm2_toggle_tg_mirror"),
        InlineKeyboardButton(f"{_bool_icon(settings.get('admin_notifications_to_telegram'))} Админ -> Telegram", callback_data="mm2_toggle_admin_tg"),
        InlineKeyboardButton(f"{_bool_icon(settings.get('admin_notifications_to_funpay'))} Админ -> FunPay", callback_data="mm2_toggle_admin_fp"),
        InlineKeyboardButton(f"{_bool_icon(settings.get('require_mm2_category_match'))} Только MM2", callback_data="mm2_toggle_mm2_only"),
        InlineKeyboardButton(f"{_bool_icon(settings.get('pause_on_auth_error'))} Пауза при auth error", callback_data="mm2_toggle_pause_auth"),
        InlineKeyboardButton(f"{_bool_icon(settings.get('auto_start_delivery_after_friend_request'))} Автостарт после friend request", callback_data="mm2_toggle_auto_start"),
    )
    kb.row(
        InlineKeyboardButton("🍪 Cookie", callback_data="mm2_set_cookie"),
        InlineKeyboardButton("🤖 Bot ID", callback_data="mm2_set_bot_id"),
    )
    kb.row(
        InlineKeyboardButton("👤 Username", callback_data="mm2_set_bot_username"),
        InlineKeyboardButton("🔗 Профиль", callback_data="mm2_set_profile_url"),
    )
    kb.row(
        InlineKeyboardButton("🏰 VIP-сервер", callback_data="mm2_set_vip_url"),
        InlineKeyboardButton("🎮 Place ID", callback_data="mm2_set_place_id"),
    )
    kb.row(
        InlineKeyboardButton("🧩 Instance ID", callback_data="mm2_set_game_instance"),
        InlineKeyboardButton("💬 Admin chat", callback_data="mm2_set_admin_chat"),
    )
    kb.row(
        InlineKeyboardButton("🧑‍💼 FunPay продавец", callback_data="mm2_set_seller_names"),
    )
    kb.row(
        InlineKeyboardButton("⏱ Таймауты", callback_data="mm2_timeouts"),
        InlineKeyboardButton("⬅️ Назад", callback_data="mm2_back_main"),
    )
    return kb


def _tg_timeouts_keyboard() -> Any:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("⏳ Минуты ожидания доставки", callback_data="mm2_set_delivery_timeout"),
        InlineKeyboardButton("📡 Интервал presence", callback_data="mm2_set_presence_poll"),
        InlineKeyboardButton("🤝 Таймаут трейда", callback_data="mm2_set_trade_timeout"),
        InlineKeyboardButton("🔁 Кол-во попыток трейда", callback_data="mm2_set_trade_retries"),
        InlineKeyboardButton("⬅️ Настройки", callback_data="mm2_settings"),
    )
    return kb


def _tg_mappings_keyboard() -> Any:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("➕ Добавить", callback_data="mm2_add_mapping"),
        InlineKeyboardButton("🔎 Найти предмет", callback_data="mm2_search_item"),
    )
    kb.row(
        InlineKeyboardButton("📤 Экспорт", callback_data="mm2_export_maps"),
        InlineKeyboardButton("📥 Импорт", callback_data="mm2_import_maps"),
    )
    kb.row(
        InlineKeyboardButton("🔄 Обновить список", callback_data="mm2_mappings"),
        InlineKeyboardButton("⬅️ Назад", callback_data="mm2_back_main"),
    )
    return kb


def _tg_inventory_keyboard() -> Any:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🔄 Сканировать инвентарь", callback_data="mm2_inventory_sync"),
        InlineKeyboardButton("📋 Список ID", callback_data="mm2_inventory_list"),
    )
    kb.row(
        InlineKeyboardButton("✍️ Ручной импорт", callback_data="mm2_manual_inventory"),
        InlineKeyboardButton("🧾 Пример описания", callback_data="mm2_inventory_hint"),
    )
    kb.row(
        InlineKeyboardButton("📦 Лоты/ID", callback_data="mm2_mappings"),
        InlineKeyboardButton("⬅️ Назад", callback_data="mm2_back_main"),
    )
    return kb


def _tg_orders_keyboard() -> Any:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("⏳ Ждут ник", callback_data="mm2_orders_WAITING_NICK"),
        InlineKeyboardButton("🤝 Ждут дружбу", callback_data="mm2_orders_WAITING_FRIEND"),
    )
    kb.row(
        InlineKeyboardButton("🎮 Ждут вход", callback_data="mm2_orders_WAITING_JOIN"),
        InlineKeyboardButton("🔁 Трейдинг", callback_data="mm2_orders_TRADING"),
    )
    kb.row(
        InlineKeyboardButton("⏸ Отложены", callback_data="mm2_orders_DELAYED"),
        InlineKeyboardButton("🧾 Ручная", callback_data="mm2_orders_MANUAL_REVIEW"),
    )
    kb.row(
        InlineKeyboardButton("🔍 Открыть заказ", callback_data="mm2_order_open"),
        InlineKeyboardButton("🔁 Повторить", callback_data="mm2_order_retry"),
    )
    kb.row(
        InlineKeyboardButton("⏸ Отложить", callback_data="mm2_order_delay"),
        InlineKeyboardButton("▶️ Возобновить", callback_data="mm2_order_ready"),
    )
    kb.row(
        InlineKeyboardButton("🧾 В ручную", callback_data="mm2_order_manual"),
        InlineKeyboardButton("⬅️ Назад", callback_data="mm2_back_main"),
    )
    return kb


def _tg_auth_keyboard() -> Any:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🏥 Проверить Roblox-сессию", callback_data="mm2_diag"),
        InlineKeyboardButton("🍪 Обновить cookie", callback_data="mm2_set_cookie"),
        InlineKeyboardButton("✅ Снять auth-паузу", callback_data="mm2_unpause_auth"),
        InlineKeyboardButton("📖 Runbook auth", callback_data="mm2_runbook_auth"),
        InlineKeyboardButton("⬅️ Назад", callback_data="mm2_back_main"),
    )
    return kb


def _tg_guide_keyboard() -> Any:
    kb = InlineKeyboardMarkup(row_width=2)
    for label, key in (
        ("🚀 Старт", "start"),
        ("🍪 Cookie", "cookies"),
        ("📦 Лоты", "lots"),
        ("🎒 Инвентарь", "inventory"),
        ("🔄 Статусы", "states"),
        ("👤 Покупатель", "buyer"),
        ("🤝 Трейд", "trade"),
        ("🔐 Ошибки", "privacy"),
    ):
        kb.add(InlineKeyboardButton(label, callback_data=f"mm2_guide_{key}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="mm2_back_main"))
    return kb


def _tg_runbook_keyboard() -> Any:
    kb = InlineKeyboardMarkup(row_width=2)
    for label, key in (
        ("🔐 Auth", "auth"),
        ("👤 Nick", "nickname"),
        ("🤝 Friends", "friends"),
        ("🎮 Join", "join"),
        ("💱 Trade", "trade"),
        ("📦 Mapping", "mapping"),
        ("🎒 Inventory", "inventory"),
        ("💾 Storage", "storage"),
    ):
        kb.add(InlineKeyboardButton(label, callback_data=f"mm2_runbook_{key}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="mm2_back_main"))
    return kb


def _tg_main_text(store: SQLiteStore, settings: Dict[str, Any]) -> str:
    active_states = [OrderState.WAITING_NICK, OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN, OrderState.TRADING]
    active_count = len(store.list_orders_by_states(active_states, limit=1000))
    manual_count = len(store.list_orders_by_states([OrderState.MANUAL_REVIEW], limit=1000))
    delayed_count = len(store.list_orders_by_states([OrderState.DELAYED], limit=1000))
    mapping_count = len(store.list_mappings())
    inventory_count = len(store.list_inventory_items(limit=1000))
    return (
        f"🔪 <b>{_html(NAME)} v{_html(VERSION)}</b>\n\n"
        f"Автовыдача MM2-предметов после заказов FunPay.\n\n"
        f"{_bool_icon(settings.get('enabled'))} <b>Плагин:</b> {'включён' if settings.get('enabled') else 'выключен'}\n"
        f"{_bool_icon(get_setting_path(settings, 'browser.enabled'))} <b>Playwright трейд:</b> {'включён' if get_setting_path(settings, 'browser.enabled') else 'выключен'}\n"
        f"🔐 <b>Auth pause:</b> <code>{_html(store.get_flag('paused_by_auth', '0'))}</code>\n"
        f"📦 <b>Лотов в таблице:</b> <code>{mapping_count}</code>\n"
        f"🎒 <b>Предметов инвентаря:</b> <code>{inventory_count}</code>\n"
        f"📋 <b>Активные:</b> <code>{active_count}</code>\n"
        f"⏸ <b>Отложены:</b> <code>{delayed_count}</code>\n"
        f"🧾 <b>Ручная проверка:</b> <code>{manual_count}</code>\n\n"
        f"Нажмите кнопку ниже для настройки и сопровождения."
    )


def _tg_settings_text(settings: Dict[str, Any]) -> str:
    cookie = str(settings.get("roblox_security_cookie") or "")
    seller_aliases_raw = settings.get("seller_funpay_usernames") or []
    if isinstance(seller_aliases_raw, str):
        seller_aliases = [seller_aliases_raw]
    else:
        seller_aliases = [str(alias) for alias in seller_aliases_raw]
    return (
        "⚙️ <b>Настройки MM2 Auto Delivery</b>\n\n"
        f"🍪 Cookie: <code>{_html(mask_secret(cookie))}</code>\n"
        f"🤖 Bot ID: <code>{_html(settings.get('roblox_bot_user_id') or 'не задан')}</code>\n"
        f"👤 Username: <code>{_html(settings.get('roblox_bot_username') or 'не задан')}</code>\n"
        f"🔗 Profile: <code>{_html('задан' if settings.get('roblox_profile_url') else 'не задан')}</code>\n"
        f"🏰 VIP: <code>{_html('задан' if settings.get('roblox_vip_server_url') else 'не задан')}</code>\n"
        f"🎮 Place ID: <code>{_html(settings.get('roblox_place_id') or '-')}</code>\n"
        f"🧩 Instance: <code>{_html(settings.get('expected_game_instance_id') or '-')}</code>\n"
        f"💬 Admin chat: <code>{_html(settings.get('admin_funpay_chat_id') or '-')}</code>\n\n"
        f"🧑‍💼 Seller aliases: <code>{_html(', '.join(seller_aliases) or 'авто')}</code>\n"
        f"{_bool_icon(settings.get('require_mm2_category_match'))} только MM2-заказы\n"
        f"{_bool_icon(settings.get('buyer_server_join_enabled'))} /joinme покупателя\n"
        f"{_bool_icon(settings.get('telegram_mirror_funpay_messages'))} копии FunPay-сообщений в Telegram\n"
        f"{_bool_icon(settings.get('admin_notifications_to_telegram'))} админ-уведомления в Telegram\n"
        f"{_bool_icon(settings.get('admin_notifications_to_funpay'))} админ-уведомления в FunPay\n\n"
        "Используйте кнопки для изменения параметров."
    )


def _tg_timeouts_text(settings: Dict[str, Any]) -> str:
    return (
        "⏱ <b>Таймауты и повторы</b>\n\n"
        f"Доставка: <code>{_html(settings.get('delivery_timeout_minutes'))}</code> мин.\n"
        f"Presence poll: <code>{_html(settings.get('presence_poll_seconds'))}</code> сек.\n"
        f"Friend accept: <code>{_html(settings.get('friend_accept_timeout_minutes'))}</code> мин.\n"
        f"Trade timeout: <code>{_html(settings.get('trade_timeout_seconds'))}</code> сек.\n"
        f"Trade retries: <code>{_html(settings.get('trade_retry_count'))}</code>\n"
        f"Trade retry delay: <code>{_html(settings.get('trade_retry_delay_seconds'))}</code> сек."
    )


def _tg_mappings_text(store: SQLiteStore) -> str:
    text = OrderFormatter.mappings_table(store.list_mappings(), limit=30)
    return "📦 <b>Таблица лотов</b>\n\n" + _html(text)


def _tg_inventory_text(coordinator: DeliveryCoordinator) -> str:
    items = coordinator.store.list_inventory_items(limit=500)
    inv_settings = coordinator.settings.inventory_sync or {}
    return (
        "🎒 <b>Инвентарь аккаунта-бота</b>\n\n"
        f"Предметов в локальной базе: <code>{len(items)}</code>\n"
        f"Авто-ID для лотов: <code>{'да' if inv_settings.get('auto_create_lot_mapping', True) else 'нет'}</code>\n"
        f"Старт ID: <code>{_html(inv_settings.get('lot_id_start', 1001))}</code>\n\n"
        "Нажмите «Сканировать», чтобы бот попробовал подключиться к Roblox-аккаунту и прочитать инвентарь.\n"
        "Если MM2-инвентарь нельзя прочитать автоматически, используйте «Ручной импорт»."
    )


def _tg_orders_text(store: SQLiteStore) -> str:
    states = [OrderState.WAITING_NICK, OrderState.WAITING_FRIEND, OrderState.WAITING_JOIN, OrderState.TRADING, OrderState.DELAYED, OrderState.MANUAL_REVIEW]
    lines = ["📋 <b>Заказы MM2</b>\n"]
    for state in states:
        count = len(store.list_orders_by_states([state], limit=1000))
        lines.append(f"<code>{state.value}</code>: <b>{count}</b>")
    lines.append("\nВыберите список или действие кнопками.")
    return "\n".join(lines)


def _tg_orders_by_state_text(store: SQLiteStore, state: OrderState) -> str:
    orders = store.list_orders_by_states([state], limit=30)
    text = OrderFormatter.active_orders(f"Заказы {state.value}:", orders)
    return _html(text)


def _tg_order_detail_text(store: SQLiteStore, order_id: str) -> str:
    order = store.get_order(order_id)
    if not order:
        return f"Заказ #{_html(order_id)} не найден."
    return _html(OrderFormatter.detailed_order(order, store.get_recent_events(order_id, limit=8)))


def _tg_guide_text(section: str = "") -> str:
    if not section:
        return "📖 <b>Гайд MM2 Auto Delivery</b>\n\nВыберите раздел."
    title, body = ADMIN_GUIDE_SECTIONS.get(section, ADMIN_GUIDE_SECTIONS["start"])
    return f"📖 <b>{_html(title)}</b>\n\n{_html(body)}"


def _tg_runbook_text(section: str = "") -> str:
    if not section:
        return "🛟 <b>Runbook</b>\n\nВыберите тип проблемы."
    title, steps = ADMIN_RUNBOOKS.get(section, ADMIN_RUNBOOKS["auth"])
    lines = [f"🛟 <b>{_html(title)}</b>", ""]
    lines.extend(f"{idx}. {_html(step)}" for idx, step in enumerate(steps, start=1))
    return "\n".join(lines)


def _tg_prompt_text(state: str) -> str:
    prompts = {
        "mm2_set_cookie": (
            "Отправьте новое значение <code>.ROBLOSECURITY</code>.\n"
            "Сообщение с cookie будет удалено, если Telegram позволит."
        ),
        "mm2_set_bot_id": "Отправьте Roblox user id аккаунта-бота. Пример: <code>123456789</code>",
        "mm2_set_bot_username": "Отправьте username аккаунта-бота Roblox.",
        "mm2_set_profile_url": "Отправьте ссылку на профиль бота Roblox.",
        "mm2_set_vip_url": "Отправьте ссылку на VIP-сервер MM2.",
        "mm2_set_place_id": "Отправьте Roblox place id. Для MM2 обычно <code>142823291</code>.",
        "mm2_set_game_instance": "Отправьте game instance id или <code>-</code>, чтобы очистить.",
        "mm2_set_admin_chat": "Отправьте FunPay chat_id администратора или <code>-</code>, чтобы очистить.",
        "mm2_set_seller_names": "Отправьте ваш FunPay-ник продавца. Можно несколько через запятую. Пример: <code>MyShop, MySecondShop</code>",
        "mm2_set_delivery_timeout": "Отправьте минуты ожидания покупателя на сервере. Пример: <code>12</code>",
        "mm2_set_presence_poll": "Отправьте интервал проверки presence в секундах. Пример: <code>15</code>",
        "mm2_set_trade_timeout": "Отправьте таймаут трейда в секундах. Пример: <code>60</code>",
        "mm2_set_trade_retries": "Отправьте количество повторов трейда. Пример: <code>3</code>",
        "mm2_add_mapping": (
            "Отправьте лот в формате:\n"
            "<code>1001 | Harvester | ancient | 1</code>\n\n"
            "Категорию и остаток можно не указывать:\n"
            "<code>1001 | Harvester</code>"
        ),
        "mm2_search_item": "Отправьте часть названия предмета. Пример: <code>harv</code>",
        "mm2_import_maps": "Отправьте JSON экспорта с полем <code>items</code>.",
        "mm2_manual_inventory": (
            "Отправьте список предметов из инвентаря. Можно по строкам или через запятую:\n"
            "<code>Harvester\nIcebreaker x2\nCorrupt</code>\n\n"
            "Бот присвоит каждому предмету ID и создаст mapping для лотов."
        ),
        "mm2_inventory_hint": "Отправьте название предмета для примера ID или <code>-</code>, чтобы взять первый из списка.",
        "mm2_order_open": "Отправьте ID заказа FunPay для просмотра.",
        "mm2_order_retry": "Отправьте ID заказа FunPay для повтора доставки.",
        "mm2_order_delay": "Отправьте ID заказа FunPay, который нужно отложить.",
        "mm2_order_ready": "Отправьте ID заказа FunPay, который нужно возобновить.",
        "mm2_order_manual": "Отправьте ID заказа FunPay для ручной проверки.",
        "mm2_buyer_message": "Отправьте: <code>FP_ID | текст сообщения покупателю</code>",
    }
    return prompts.get(state, f"Отправьте значение для {_html(TG_STATE_LABELS.get(state, state))}.")


def _tg_answer(bot: Any, call: Any, text: str = "", alert: bool = False) -> None:
    with contextlib.suppress(Exception):
        bot.answer_callback_query(call.id, text[:190] if text else None, show_alert=alert)


def _tg_authorized_users() -> List[int]:
    try:
        from tg_bot.utils import load_authorized_users

        users = load_authorized_users() or []
        return [int(user_id) for user_id in users]
    except Exception:
        return []


def _tg_send_to_admins(cardinal: Any, text: str, keyboard: Any = None) -> None:
    telegram = getattr(cardinal, "telegram", None)
    bot = getattr(telegram, "bot", None)
    if not bot:
        return
    for user_id in _tg_authorized_users():
        try:
            bot.send_message(
                user_id,
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.debug("%s: не удалось отправить TG user %s: %s", LOGGER_PREFIX, user_id, exc)


def _tg_edit(bot: Any, chat_id: Any, msg_id: Any, text: str, keyboard: Any = None) -> None:
    bot.edit_message_text(text, chat_id, msg_id, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)


def _tg_send_prompt(tg: Any, bot: Any, call: Any, state: str) -> None:
    result = bot.send_message(call.message.chat.id, _tg_prompt_text(state), parse_mode="HTML", disable_web_page_preview=True)
    tg.set_state(chat_id=call.message.chat.id, message_id=result.id, user_id=call.from_user.id, state=state)
    _tg_answer(bot, call)


def _run_async_sync(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _save_tg_state_value(state: str, text: str, coordinator: DeliveryCoordinator) -> str:
    raw = (text or "").strip()
    int_settings = {
        "mm2_set_bot_id": ("roblox_bot_user_id", 0, 10**18),
        "mm2_set_place_id": ("roblox_place_id", 1, 10**18),
        "mm2_set_delivery_timeout": ("delivery_timeout_minutes", 1, 180),
        "mm2_set_presence_poll": ("presence_poll_seconds", 5, 600),
        "mm2_set_trade_timeout": ("trade_timeout_seconds", 20, 600),
        "mm2_set_trade_retries": ("trade_retry_count", 1, 20),
    }
    str_settings = {
        "mm2_set_cookie": "roblox_security_cookie",
        "mm2_set_bot_username": "roblox_bot_username",
        "mm2_set_profile_url": "roblox_profile_url",
        "mm2_set_vip_url": "roblox_vip_server_url",
        "mm2_set_game_instance": "expected_game_instance_id",
        "mm2_set_admin_chat": "admin_funpay_chat_id",
    }
    if state in int_settings:
        path, min_value, max_value = int_settings[state]
        try:
            value = int(raw)
        except Exception:
            return "❌ Нужно отправить число."
        if value < min_value or value > max_value:
            return f"❌ Число должно быть от {min_value} до {max_value}."
        update_setting_path(path, value)
        coordinator.reload_settings()
        return f"✅ Сохранено: {TG_STATE_LABELS.get(state)} = {value}"
    if state in str_settings:
        value = "" if raw == "-" else raw
        update_setting_path(str_settings[state], value)
        coordinator.reload_settings()
        if state == "mm2_set_cookie":
            return "✅ Cookie сохранён. Нажмите «Roblox Auth» → «Проверить Roblox-сессию»."
        return f"✅ Сохранено: {TG_STATE_LABELS.get(state)}"
    if state == "mm2_set_seller_names":
        aliases = [] if raw == "-" else [part.strip() for part in re.split(r"[,;\n]", raw) if part.strip()]
        update_setting_path("seller_funpay_usernames", aliases)
        coordinator.reload_settings()
        return "✅ FunPay-продавцы сохранены: " + (", ".join(aliases) if aliases else "авто")
    if state == "mm2_add_mapping":
        item = AdminCommandRouter(
            settings_getter=lambda: coordinator.settings,
            store=coordinator.store,
            funpay=coordinator.funpay,
            roblox=coordinator.roblox,
            resume_callback=lambda order_id: coordinator.enqueue_delivery(order_id, "tg_mapping_helper"),
            reload_callback=coordinator.reload_settings,
            state_machine=coordinator.state_machine,
        )._parse_mapping_args(raw)
        if not item:
            return "❌ Формат: 1001 | Harvester | ancient | 1"
        if not item.category:
            item.category = ItemNameHelper().suggest_category(item.item_name)
        coordinator.store.upsert_mapping(item)
        return f"✅ Лот {item.lot_id} -> {item.item_name} сохранён."
    if state == "mm2_search_item":
        return ItemNameHelper().format_search(raw)
    if state == "mm2_import_maps":
        imported, errors = MappingCatalogIO(coordinator.store).import_json(raw)
        lines = [f"✅ Импортировано: {imported}"]
        if errors:
            lines.append("Ошибки:")
            lines.extend(errors[:10])
        return "\n".join(lines)
    if state == "mm2_manual_inventory":
        result = coordinator.inventory.manual_import(raw)
        return result.summary_text() + "\n\n" + coordinator.inventory.format_inventory_ids(limit=80)
    if state == "mm2_inventory_hint":
        return coordinator.inventory.format_funpay_description_hint("" if raw == "-" else raw)
    if state.startswith("mm2_order_"):
        return _apply_tg_order_action(state, raw, coordinator)
    if state == "mm2_buyer_message":
        if "|" not in raw:
            return "❌ Формат: FP_ID | текст сообщения"
        order_id, message = [part.strip() for part in raw.split("|", 1)]
        order = coordinator.store.get_order(order_id)
        if not order:
            return f"❌ Заказ #{order_id} не найден."
        coordinator.funpay.send_message(order.chat_id, message, scope=f"tg_manual_msg:{order.order_id}:{int(time.time())}")
        return f"✅ Сообщение отправлено покупателю заказа #{order.order_id}."
    return "❌ Неизвестное состояние ввода."


def _apply_tg_order_action(state: str, order_id: str, coordinator: DeliveryCoordinator) -> str:
    order_id = (order_id or "").strip().split()[0] if order_id else ""
    if not order_id:
        return "❌ Укажите ID заказа FunPay."
    order = coordinator.store.get_order(order_id)
    if not order:
        return f"❌ Заказ #{order_id} не найден."
    if state == "mm2_order_open":
        return OrderFormatter.detailed_order(order, coordinator.store.get_recent_events(order_id, limit=8))
    if state == "mm2_order_retry":
        if order.state == OrderState.MANUAL_REVIEW:
            coordinator.state_machine.transition(order, OrderState.WAITING_JOIN if order.roblox_user_id else OrderState.WAITING_NICK, "tg_retry", "Повтор через Telegram")
        elif order.state == OrderState.DELAYED:
            coordinator.state_machine.transition(order, OrderState.WAITING_JOIN if order.roblox_user_id else OrderState.WAITING_NICK, "tg_ready_retry", "Возобновление через Telegram")
        if order.roblox_user_id:
            coordinator.enqueue_delivery(order.order_id, "tg_retry")
        return f"✅ Повтор доставки заказа #{order.order_id} запущен."
    if state == "mm2_order_delay":
        if order.state in TERMINAL_STATES:
            return "❌ Финальный заказ нельзя отложить."
        order.metadata["state_before_delay"] = order.state.value
        coordinator.state_machine.transition(order, OrderState.DELAYED, "tg_delay", "Отложено через Telegram")
        coordinator.funpay.send_message(order.chat_id, coordinator.settings.message("delayed"), scope=f"tg_delayed:{order.order_id}")
        return f"✅ Заказ #{order.order_id} отложен."
    if state == "mm2_order_ready":
        target = OrderState.WAITING_JOIN if order.roblox_user_id else OrderState.WAITING_NICK
        if order.state in TERMINAL_STATES and order.state != OrderState.MANUAL_REVIEW:
            return "❌ Финальный заказ нельзя возобновить."
        coordinator.state_machine.transition(order, target, "tg_ready", "Возобновлено через Telegram")
        if order.roblox_user_id:
            coordinator.enqueue_delivery(order.order_id, "tg_ready")
        coordinator.funpay.send_message(order.chat_id, coordinator.settings.message("ready"), scope=f"tg_ready:{order.order_id}")
        return f"✅ Заказ #{order.order_id} переведён в {target.value}."
    if state == "mm2_order_manual":
        if order.state in TERMINAL_STATES and order.state != OrderState.FAILED:
            return "❌ Заказ уже в финальном состоянии."
        coordinator.state_machine.transition(order, OrderState.MANUAL_REVIEW, "tg_manual", "Ручная проверка через Telegram")
        return f"✅ Заказ #{order.order_id} переведён в MANUAL_REVIEW."
    return "❌ Неизвестное действие заказа."


def init_telegram_panel(cardinal: Any, *args: Any) -> None:
    if not _tg_available() or not getattr(cardinal, "telegram", None):
        return
    if getattr(cardinal, "_mm2_telegram_panel_registered", False):
        return
    setattr(cardinal, "_mm2_telegram_panel_registered", True)

    tg = cardinal.telegram
    bot = tg.bot

    def coordinator() -> DeliveryCoordinator:
        return get_global_coordinator(cardinal)

    def send_main_panel(message: Any) -> None:
        coord = coordinator()
        settings = load_settings_file()
        bot.reply_to(
            message,
            _tg_main_text(coord.store, settings),
            reply_markup=_tg_main_keyboard(settings),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    def send_main_panel_by_text(message: Any) -> None:
        text = str(getattr(message, "text", "") or "").strip().lower().split("@")[0]
        if text not in ("/mm2", "/mm2_menu"):
            return
        send_main_panel(message)

    def send_status(message: Any) -> None:
        coord = coordinator()
        settings = load_settings_file()
        bot.reply_to(message, _tg_main_text(coord.store, settings), reply_markup=_tg_main_keyboard(settings), parse_mode="HTML")

    def handle_callback(call: Any) -> None:
        if not str(getattr(call, "data", "")).startswith("mm2_"):
            return
        coord = coordinator()
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        data = str(call.data)
        try:
            if data in ("mm2_back_main", "mm2_refresh"):
                settings = load_settings_file()
                _tg_edit(bot, chat_id, msg_id, _tg_main_text(coord.store, settings), _tg_main_keyboard(settings))
                _tg_answer(bot, call)
            elif data == "mm2_settings":
                settings = load_settings_file()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call)
            elif data == "mm2_timeouts":
                settings = load_settings_file()
                _tg_edit(bot, chat_id, msg_id, _tg_timeouts_text(settings), _tg_timeouts_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_toggle_enabled":
                settings = toggle_setting_path("enabled")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_main_text(coord.store, settings), _tg_main_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data == "mm2_toggle_browser":
                settings = toggle_setting_path("browser.enabled")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data == "mm2_toggle_joinme":
                settings = toggle_setting_path("buyer_server_join_enabled")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data == "mm2_toggle_tg_mirror":
                settings = toggle_setting_path("telegram_mirror_funpay_messages")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data == "mm2_toggle_admin_tg":
                settings = toggle_setting_path("admin_notifications_to_telegram")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data == "mm2_toggle_admin_fp":
                settings = toggle_setting_path("admin_notifications_to_funpay")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data == "mm2_toggle_mm2_only":
                settings = toggle_setting_path("require_mm2_category_match")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data == "mm2_toggle_pause_auth":
                settings = toggle_setting_path("pause_on_auth_error")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data == "mm2_toggle_auto_start":
                settings = toggle_setting_path("auto_start_delivery_after_friend_request")
                coord.reload_settings()
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(settings), _tg_settings_keyboard(settings))
                _tg_answer(bot, call, "Сохранено")
            elif data in TG_STATE_LABELS:
                _tg_send_prompt(tg, bot, call, data)
            elif data == "mm2_mappings":
                _tg_edit(bot, chat_id, msg_id, _tg_mappings_text(coord.store), _tg_mappings_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_inventory":
                _tg_edit(bot, chat_id, msg_id, _tg_inventory_text(coord), _tg_inventory_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_inventory_sync":
                _tg_answer(bot, call, "Сканирую инвентарь...", alert=False)
                bot.send_message(chat_id, "🔄 Запускаю синхронизацию инвентаря. Это может занять до минуты...")
                result = _run_async_sync(coord.inventory.sync())
                text = result.summary_text()
                if result.ok:
                    text += "\n\n" + coord.inventory.format_inventory_ids(limit=80)
                bot.send_message(chat_id, f"<pre>{_html(text)}</pre>", parse_mode="HTML", reply_markup=_tg_inventory_keyboard())
            elif data == "mm2_inventory_list":
                bot.send_message(chat_id, f"<pre>{_html(coord.inventory.format_inventory_ids(limit=120))}</pre>", parse_mode="HTML", reply_markup=_tg_inventory_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_inventory_hint":
                _tg_send_prompt(tg, bot, call, "mm2_inventory_hint")
            elif data == "mm2_manual_inventory":
                _tg_send_prompt(tg, bot, call, "mm2_manual_inventory")
            elif data == "mm2_export_maps":
                bot.send_message(chat_id, f"<pre>{_html(MappingCatalogIO(coord.store).export_lines_for_chat())}</pre>", parse_mode="HTML")
                _tg_answer(bot, call, "Экспорт отправлен")
            elif data == "mm2_orders":
                _tg_edit(bot, chat_id, msg_id, _tg_orders_text(coord.store), _tg_orders_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_orders_manual":
                _tg_edit(bot, chat_id, msg_id, _tg_orders_by_state_text(coord.store, OrderState.MANUAL_REVIEW), _tg_orders_keyboard())
                _tg_answer(bot, call)
            elif data.startswith("mm2_orders_"):
                state_name = data.replace("mm2_orders_", "")
                state = OrderState(state_name)
                _tg_edit(bot, chat_id, msg_id, _tg_orders_by_state_text(coord.store, state), _tg_orders_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_diag":
                report = _run_async_sync(HealthInspector(lambda: coord.settings, coord.store, coord.roblox).build_report())
                _tg_edit(bot, chat_id, msg_id, f"<pre>{_html(report)}</pre>", _tg_auth_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_auth":
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(load_settings_file()), _tg_auth_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_unpause_auth":
                coord.store.set_flag("paused_by_auth", "0")
                coord.reload_settings()
                _tg_answer(bot, call, "Auth-пауза снята", alert=True)
                _tg_edit(bot, chat_id, msg_id, _tg_settings_text(load_settings_file()), _tg_auth_keyboard())
            elif data == "mm2_reload":
                coord.reload_settings()
                settings = load_settings_file()
                _tg_edit(bot, chat_id, msg_id, _tg_main_text(coord.store, settings), _tg_main_keyboard(settings))
                _tg_answer(bot, call, "settings.json перечитан")
            elif data == "mm2_guide":
                _tg_edit(bot, chat_id, msg_id, _tg_guide_text(), _tg_guide_keyboard())
                _tg_answer(bot, call)
            elif data.startswith("mm2_guide_"):
                section = data.replace("mm2_guide_", "")
                _tg_edit(bot, chat_id, msg_id, _tg_guide_text(section), _tg_guide_keyboard())
                _tg_answer(bot, call)
            elif data == "mm2_runbooks":
                _tg_edit(bot, chat_id, msg_id, _tg_runbook_text(), _tg_runbook_keyboard())
                _tg_answer(bot, call)
            elif data.startswith("mm2_runbook_"):
                section = data.replace("mm2_runbook_", "")
                _tg_edit(bot, chat_id, msg_id, _tg_runbook_text(section), _tg_runbook_keyboard())
                _tg_answer(bot, call)
            else:
                _tg_answer(bot, call)
        except Exception as exc:
            logger.error("%s: ошибка Telegram callback %s: %s", LOGGER_PREFIX, data, exc)
            logger.debug(traceback.format_exc())
            _tg_answer(bot, call, "Ошибка обработки", alert=True)

    def handle_text_state(message: Any) -> None:
        state_data = tg.get_state(message.chat.id, message.from_user.id)
        if not state_data or "state" not in state_data:
            return
        state = str(state_data["state"])
        if not state.startswith("mm2_"):
            return
        coord = coordinator()
        try:
            result = _save_tg_state_value(state, getattr(message, "text", ""), coord)
            if state == "mm2_set_cookie":
                with contextlib.suppress(Exception):
                    bot.delete_message(message.chat.id, message.message_id)
            bot.reply_to(message, f"<pre>{_html(result)}</pre>", parse_mode="HTML", reply_markup=_tg_back_main_keyboard())
        except Exception as exc:
            logger.error("%s: ошибка обработки Telegram state %s: %s", LOGGER_PREFIX, state, exc)
            bot.reply_to(message, f"❌ Ошибка: {_html(exc)}", parse_mode="HTML")
        finally:
            with contextlib.suppress(Exception):
                tg.clear_state(message.chat.id, message.from_user.id)

    try:
        bot.message_handler(commands=["mm2", "mm2_menu"])(send_main_panel)
        bot.message_handler(commands=["mm2_status"])(send_status)
        bot.message_handler(func=lambda message: str(getattr(message, "text", "") or "").strip().lower().split("@")[0] in ("/mm2", "/mm2_menu"))(send_main_panel_by_text)
        bot.callback_query_handler(func=lambda call: str(getattr(call, "data", "")).startswith("mm2_"))(handle_callback)
        bot.message_handler(content_types=["text"])(handle_text_state)
        open_kb = InlineKeyboardMarkup(row_width=1)
        open_kb.add(InlineKeyboardButton("🔪 Открыть панель MM2", callback_data="mm2_back_main"))
        _tg_send_to_admins(
            cardinal,
            (
                f"✅ <b>{_html(NAME)} v{_html(VERSION)}</b> запущен\n\n"
                "Панель управления: <code>/mm2</code>\n"
                "Если команда не отвечает, нажмите кнопку ниже."
            ),
            keyboard=open_kb,
        )
        logger.info("%s: Telegram-панель /mm2 зарегистрирована", LOGGER_PREFIX)
    except Exception as exc:
        setattr(cardinal, "_mm2_telegram_panel_registered", False)
        logger.error("%s: не удалось зарегистрировать Telegram-панель: %s", LOGGER_PREFIX, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Singleton для BIND_TO_* хуков Cardinal
# ─────────────────────────────────────────────────────────────────────────────

_GLOBAL_COORDINATORS: Dict[int, DeliveryCoordinator] = {}


def get_global_coordinator(cardinal: Any, config: Optional[Dict[str, Any]] = None) -> DeliveryCoordinator:
    key = id(cardinal)
    coordinator = _GLOBAL_COORDINATORS.get(key)
    if coordinator is None:
        coordinator = DeliveryCoordinator(cardinal, config)
        coordinator.start()
        _GLOBAL_COORDINATORS[key] = coordinator
    return coordinator


def stop_global_coordinator(cardinal: Any) -> None:
    coordinator = _GLOBAL_COORDINATORS.pop(id(cardinal), None)
    if coordinator:
        coordinator.stop()


def safe_handler(func: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.error("%s: ошибка в %s: %s", LOGGER_PREFIX, getattr(func, "__name__", "handler"), exc)
            logger.debug(traceback.format_exc())
            return None

    wrapper.__name__ = getattr(func, "__name__", "safe_handler")
    wrapper.__doc__ = getattr(func, "__doc__", None)
    return wrapper


def bind_to_new_order(c: Any, e: NewOrderEvent) -> None:
    get_global_coordinator(c).on_new_order(e)


def bind_to_new_message(c: Any, e: NewMessageEvent) -> None:
    message = getattr(e, "message", None)
    msg_type = getattr(message, "type", None)
    if MessageTypes is not None and msg_type is not None and msg_type != getattr(MessageTypes, "NON_SYSTEM", msg_type):
        return
    get_global_coordinator(c).on_message_event(e)


def bind_to_last_chat_message_changed(c: Any, e: LastChatMessageChangedEvent) -> None:
    if not getattr(c, "old_mode_enabled", False):
        return
    chat = getattr(e, "chat", None)
    if not chat or not getattr(chat, "unread", False):
        return
    data = {
        "message": str(chat).strip(),
        "chat_id": getattr(chat, "id", ""),
        "chat_name": getattr(chat, "name", ""),
    }
    get_global_coordinator(c).on_legacy_message(data)


def bind_to_delete(c: Any, *args: Any) -> None:
    stop_global_coordinator(c)


def bind_to_pre_init(c: Any, *args: Any) -> None:
    init_telegram_panel(c, *args)


_safe_bind_to_pre_init = safe_handler(bind_to_pre_init)
_safe_bind_to_new_order = safe_handler(bind_to_new_order)
_safe_bind_to_new_message = safe_handler(bind_to_new_message)
_safe_bind_to_last_chat_message_changed = safe_handler(bind_to_last_chat_message_changed)
_safe_bind_to_delete = safe_handler(bind_to_delete)

BIND_TO_PRE_INIT = [_safe_bind_to_pre_init]
BIND_TO_NEW_ORDER = [_safe_bind_to_new_order]
BIND_TO_NEW_MESSAGE = [_safe_bind_to_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [_safe_bind_to_last_chat_message_changed]
BIND_TO_DELETE = _safe_bind_to_delete


# ─────────────────────────────────────────────────────────────────────────────
# Совместимый class Plugin для простого event_manager API
# ─────────────────────────────────────────────────────────────────────────────

class Plugin:
    def __init__(self, cardinal: Any, config: Optional[Dict[str, Any]] = None) -> None:
        self.cardinal = cardinal
        self.config = config or {}
        self.logger = logger
        self.coordinator = DeliveryCoordinator(cardinal, self.config)
        self._registered: List[Tuple[str, Callable[..., Any]]] = []
        self.logger.info("%s v%s загружен", NAME, VERSION)

    def setup(self) -> None:
        self.coordinator.start()
        event_manager = getattr(self.cardinal, "event_manager", None)
        if not event_manager:
            self.logger.info("%s: event_manager не найден; используются BIND_TO_* хуки Cardinal", LOGGER_PREFIX)
            return
        self._register(event_manager, "on_new_order", self.on_new_order)
        self._register(event_manager, "on_order", self.on_new_order)
        self._register(event_manager, "on_message", self.on_message)
        self.logger.info("%s: обработчики event_manager зарегистрированы", LOGGER_PREFIX)

    def unload(self) -> None:
        event_manager = getattr(self.cardinal, "event_manager", None)
        if event_manager:
            for event_name, handler in list(self._registered):
                with contextlib.suppress(Exception):
                    event_manager.unregister_handler(event_name, handler)
            self._registered.clear()
        self.coordinator.stop()
        self.logger.info("%s выгружен", NAME)

    def on_new_order(self, data: Any) -> None:
        self.coordinator.on_new_order(data)

    def on_message(self, data: Any) -> None:
        self.coordinator.on_legacy_message(data)

    def _register(self, event_manager: Any, event_name: str, handler: Callable[..., Any]) -> None:
        try:
            event_manager.register_handler(event_name, handler)
            self._registered.append((event_name, handler))
        except Exception as exc:
            self.logger.debug("%s: событие %s не зарегистрировано: %s", LOGGER_PREFIX, event_name, exc)


logger.info("$MAGENTA%s v%s загружен.$RESET", LOGGER_PREFIX, VERSION)
