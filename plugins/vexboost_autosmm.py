"""
Плагин автонакрутки через VexBoost для FunPay Cardinal.

Настройка: команда /vexboost в Telegram-боте Cardinal.
В описании лота укажите: ID: <service_id>  и опционально #Quan: <множитель>
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import requests
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from FunPayAPI.types import MessageTypes
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("FPC.VexBoost")

NAME = "VexBoost AutoSMM"
VERSION = "1.0.1"
DESCRIPTION = "Автонакрутка через сервис VexBoost (vexboost.ru)"
CREDITS = "Cursor AI"
UUID = "a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e"
SETTINGS_PAGE = False

STORAGE_DIR = f"storage/plugins/{UUID}"
SETTINGS_FILE = f"{STORAGE_DIR}/settings.json"
PAY_ORDERS_FILE = f"{STORAGE_DIR}/payorders.json"
ACTIVE_ORDERS_FILE = f"{STORAGE_DIR}/active_orders.json"

DEFAULT_SETTINGS = {
    "api_url": "https://vexboost.ru/api/v2",
    "api_key": "",
    "auto_refund_on_error": True,
    "allow_private_telegram": False,
    "status_check_interval": 60,
}

pending_confirmations: Dict[Any, Dict[str, Any]] = {}

URL_PATTERN = re.compile(
    r"https?://(?:[a-zA-Z0-9]|[$-_@.&+]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
)


# ---------------------------------------------------------------------------
# Хранилище и настройки
# ---------------------------------------------------------------------------

def _ensure_storage() -> None:
    os.makedirs(STORAGE_DIR, exist_ok=True)


def _load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    return default


def _save_json(path: str, data: Any) -> None:
    _ensure_storage()
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def load_settings() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_FILE):
        settings = DEFAULT_SETTINGS.copy()
        save_settings(settings)
        return settings
    with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)
    merged = DEFAULT_SETTINGS.copy()
    merged.update(data)
    return merged


def save_settings(settings: Dict[str, Any]) -> None:
    _save_json(SETTINGS_FILE, settings)


def get_api_url() -> str:
    return load_settings().get("api_url", DEFAULT_SETTINGS["api_url"]).rstrip("/")


def get_api_key() -> str:
    return load_settings().get("api_key", "")


def extract_links(text: str) -> List[str]:
    return URL_PATTERN.findall(text)


def find_order_by_buyer(orders: List[Dict[str, Any]], buyer: str) -> Optional[Dict[str, Any]]:
    for order in orders:
        if order.get("buyer") == buyer:
            return order
    return None


# ---------------------------------------------------------------------------
# VexBoost API
# ---------------------------------------------------------------------------

class VexBoostAPI:
    @staticmethod
    def _request(params: Dict[str, Any]) -> Dict[str, Any]:
        api_url = get_api_url()
        api_key = get_api_key()
        payload = {"key": api_key, **params}
        try:
            response = requests.post(api_url, data=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"error": "Некорректный ответ API"}
        except requests.RequestException as exc:
            logger.error("Ошибка запроса к VexBoost: %s", exc)
            return {"error": str(exc)}
        except ValueError:
            return {"error": "Некорректный ответ API"}

    @staticmethod
    def create_order(service_id: int, link: str, quantity: int) -> Any:
        data = VexBoostAPI._request({
            "action": "add",
            "service": service_id,
            "link": link,
            "quantity": quantity,
        })
        if "order" in data:
            return data["order"]
        return data.get("error", "Неизвестная ошибка")

    @staticmethod
    def get_order_status(order_id: int) -> Optional[Dict[str, Any]]:
        data = VexBoostAPI._request({"action": "status", "order": order_id})
        if "error" in data:
            return None
        return data

    @staticmethod
    def refill_order(order_id: int) -> Optional[Any]:
        data = VexBoostAPI._request({"action": "refill", "order": order_id})
        return data.get("refill")

    @staticmethod
    def get_balance() -> Optional[Tuple[float, str]]:
        data = VexBoostAPI._request({"action": "balance"})
        if "balance" not in data:
            return None
        match = re.search(r"[\d.]+", str(data["balance"]))
        if not match:
            return None
        return float(match.group()), data.get("currency", "RUB")


# ---------------------------------------------------------------------------
# Обработка заказов FunPay
# ---------------------------------------------------------------------------

def bind_to_new_order(c: Cardinal, e: NewOrderEvent) -> None:
    try:
        if not get_api_key():
            logger.warning("API-ключ VexBoost не задан")
            return

        order_id = e.order.id
        full_order = c.account.get_order(order_id)
        description = full_order.full_description or ""
        buyer = full_order.buyer_username

        match_id = re.search(r"ID:\s*(\d+)", description, re.IGNORECASE)
        if not match_id:
            return

        service_id = int(match_id.group(1))
        multiplier = 1
        match_quan = re.search(r"#Quan:\s*(\d+)", description, re.IGNORECASE)
        if match_quan:
            multiplier = max(1, int(match_quan.group(1)))

        amount = int(e.order.amount) * multiplier
        chat = c.account.get_chat_by_name(buyer)
        chat_id = chat.id if chat else e.order.chat_id

        order_entry = {
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

        pay_orders = _load_json(PAY_ORDERS_FILE, [])
        pay_orders.append(order_entry)
        _save_json(PAY_ORDERS_FILE, pay_orders)

        if chat_id:
            c.send_message(
                chat_id,
                "👋 Спасибо за заказ!\nОтправьте ссылку на аккаунт/пост для накрутки.",
            )
        logger.info(
            "VexBoost: новый заказ FunPay #%s, service=%s, qty=%s",
            order_id, service_id, amount,
        )
    except Exception as exc:
        logger.error("Ошибка обработки нового заказа: %s", exc, exc_info=True)


def msg_hook(c: Cardinal, e: NewMessageEvent) -> None:
    msg = e.message
    message_text = (msg.text or "").strip()
    msgname = msg.chat_name

    if "вернул деньги покупателю" in message_text:
        pay_orders = _load_json(PAY_ORDERS_FILE, [])
        order = find_order_by_buyer(pay_orders, msgname)
        if order:
            pay_orders = [o for o in pay_orders if o.get("buyer") != msgname]
            _save_json(PAY_ORDERS_FILE, pay_orders)
        return

    if msg.type != MessageTypes.NON_SYSTEM:
        return

    if msg.author_id == c.account.id:
        return

    if msg.chat_id in pending_confirmations:
        if message_text in ("+", "-"):
            confirm_order(c, msg.chat_id, message_text)
            return
        if "http" in message_text:
            order = pending_confirmations.get(msg.chat_id)
            if order:
                order["chat_id"] = msg.chat_id
                links = extract_links(message_text)
                if links:
                    request_confirmation(c, order, links[0])
            return
        c.send_message(
            msg.chat_id,
            "⚪️ Отправьте + для подтверждения, - для отмены или новую ссылку.",
        )
        return

    if message_text.startswith("#статус"):
        parts = message_text.split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = VexBoostAPI.get_order_status(int(parts[1]))
            if status:
                start = status.get("start_count", 0)
                display_start = "*" if start == 0 else str(start)
                c.send_message(
                    msg.chat_id,
                    f"📈 Статус заказа: {parts[1]}\n"
                    f"⠀∟ 📊 Статус: {status.get('status', '—')}\n"
                    f"⠀∟ 🔢 Было: {display_start}\n"
                    f"⠀∟ 👀 Остаток: {status.get('remains', '—')}",
                )
            else:
                c.send_message(msg.chat_id, "🔴 Не удалось получить статус заказа.")
        return

    if message_text.startswith("#рефилл"):
        parts = message_text.split()
        if len(parts) >= 2 and parts[1].isdigit():
            result = VexBoostAPI.refill_order(int(parts[1]))
            if result is not None:
                c.send_message(msg.chat_id, "✅ Запрос на рефилл отправлен!")
            else:
                c.send_message(msg.chat_id, "🔴 Ошибка рефилла.")
        return

    pay_orders = _load_json(PAY_ORDERS_FILE, [])
    order = find_order_by_buyer(pay_orders, msgname)
    if not order:
        return

    links = extract_links(message_text)
    if links:
        order["chat_id"] = msg.chat_id
        request_confirmation(c, order, links[0])


def request_confirmation(c: Cardinal, order: Dict[str, Any], link: str) -> None:
    settings = load_settings()
    if not settings.get("allow_private_telegram"):
        if "t.me" in link and ("/c/" in link or "+" in link):
            c.send_message(
                order["chat_id"],
                "❌ Закрытые Telegram-каналы/группы не поддерживаются.\n"
                "Используйте публичную ссылку вида https://t.me/channel",
            )
            return

    order["url"] = link
    display_link = link.replace("https://", "").replace("http://", "")
    c.send_message(
        order["chat_id"],
        f"📋 Проверьте детали заказа:\n"
        f"🛒 Лот: {order['Order']}\n"
        f"🔢 Количество: {order['Amount']} шт.\n"
        f"🔗 Ссылка: {display_link}\n\n"
        f"✅ Отправьте + для подтверждения\n"
        f"❌ Отправьте - для отмены и возврата\n"
        f"🔄 Или отправьте новую ссылку",
    )
    pending_confirmations[order["chat_id"]] = order

    pay_orders = _load_json(PAY_ORDERS_FILE, [])
    for idx, existing in enumerate(pay_orders):
        if existing.get("OrderID") == order.get("OrderID"):
            pay_orders[idx] = order
            break
    else:
        pay_orders.append(order)
    _save_json(PAY_ORDERS_FILE, pay_orders)


def confirm_order(c: Cardinal, chat_id: Any, text: str) -> None:
    settings = load_settings()
    order = pending_confirmations.pop(chat_id, None)
    if not order:
        return

    if text.strip() == "+":
        result = VexBoostAPI.create_order(
            order["service_id"], order["url"], order["Amount"],
        )
        if isinstance(result, int) or (isinstance(result, str) and str(result).isdigit()):
            smm_id = int(result)
            active = _load_json(ACTIVE_ORDERS_FILE, {})
            active[str(smm_id)] = {
                "service_id": order["service_id"],
                "chat_id": order["chat_id"],
                "order_id": order["OrderID"],
                "order_url": order["url"],
                "order_amount": order["Amount"],
                "status": "pending",
            }
            _save_json(ACTIVE_ORDERS_FILE, active)

            pay_orders = _load_json(PAY_ORDERS_FILE, [])
            pay_orders = [o for o in pay_orders if o.get("buyer") != order["buyer"]]
            _save_json(PAY_ORDERS_FILE, pay_orders)

            c.send_message(
                order["chat_id"],
                f"📊 Заказ создан и отправлен в VexBoost!\n"
                f"🆔 ID заказа: {smm_id}\n\n"
                f"📋 Команды:\n"
                f"⠀∟ #статус {smm_id}\n"
                f"⠀∟ #рефилл {smm_id}\n\n"
                f"⌛ Время выполнения: от нескольких минут до 48 часов.",
            )
            logger.info("VexBoost #%s создан для FunPay #%s", smm_id, order["OrderID"])
        else:
            c.send_message(order["chat_id"], f"❌ Ошибка при создании заказа: {result}")
            if settings.get("auto_refund_on_error"):
                _refund_order(c, order["OrderID"])
    elif text.strip() == "-":
        c.send_message(chat_id, "❌ Заказ отменён.")
        pay_orders = _load_json(PAY_ORDERS_FILE, [])
        pay_orders = [o for o in pay_orders if o.get("buyer") != order["buyer"]]
        _save_json(PAY_ORDERS_FILE, pay_orders)
        _refund_order(c, order["OrderID"])


def _refund_order(c: Cardinal, order_id: str) -> None:
    if not order_id:
        return
    try:
        c.account.refund(order_id)
        logger.info("Возврат средств по заказу FunPay #%s", order_id)
    except Exception as exc:
        logger.error("Ошибка возврата FunPay #%s: %s", order_id, exc)


# ---------------------------------------------------------------------------
# Фоновая проверка статусов
# ---------------------------------------------------------------------------

def start_status_checker(c: Cardinal) -> None:
    threading.Thread(target=_status_checker_loop, args=(c,), daemon=True).start()


def _status_checker_loop(c: Cardinal) -> None:
    while True:
        try:
            _check_active_orders(c)
        except Exception as exc:
            logger.error("Ошибка проверки статусов: %s", exc)
        interval = max(30, int(load_settings().get("status_check_interval", 60)))
        time.sleep(interval)


def _check_active_orders(c: Cardinal) -> None:
    if not get_api_key():
        return

    active: Dict[str, Any] = _load_json(ACTIVE_ORDERS_FILE, {})
    if not active:
        return

    to_remove: List[str] = []
    for smm_id, info in active.items():
        status_data = VexBoostAPI.get_order_status(int(smm_id))
        if not status_data:
            continue

        status = status_data.get("status", "")
        chat_id = info.get("chat_id")
        funpay_id = info.get("order_id", "")

        if status == "Completed":
            if chat_id:
                c.send_message(
                    chat_id,
                    f"✅ Заказ #{funpay_id} выполнен!\n"
                    f"Подтвердите выполнение на FunPay:\n"
                    f"https://funpay.com/orders/{funpay_id}/",
                )
            to_remove.append(smm_id)

        elif status == "Canceled":
            if chat_id:
                c.send_message(chat_id, f"❌ Заказ #{funpay_id} отменён на стороне VexBoost.")
            _refund_order(c, funpay_id)
            to_remove.append(smm_id)

    for smm_id in to_remove:
        active.pop(smm_id, None)
    _save_json(ACTIVE_ORDERS_FILE, active)


# ---------------------------------------------------------------------------
# Telegram-настройки
# ---------------------------------------------------------------------------

def init_commands(cardinal: Cardinal, *args) -> None:
    if not cardinal.telegram:
        return

    tg = cardinal.telegram
    bot = tg.bot
    settings = load_settings()

    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🔗 API URL", callback_data="vb_set_api_url"),
        InlineKeyboardButton("🔐 API KEY", callback_data="vb_set_api_key"),
        InlineKeyboardButton("🔄 Автовозврат", callback_data="vb_toggle_refund"),
        InlineKeyboardButton("💰 Баланс VexBoost", callback_data="vb_balance"),
    )

    def send_settings(message):
        refund = "🟢 Вкл" if settings.get("auto_refund_on_error") else "🔴 Выкл"
        bot.reply_to(
            message,
            f"⚙️ <b>{NAME}</b> v{VERSION}\n\n"
            f"🔗 API: <code>{get_api_url()}</code>\n"
            f"🔐 KEY: <code>{'***' + get_api_key()[-4:] if len(get_api_key()) > 4 else 'не задан'}</code>\n"
            f"🔄 Автовозврат: {refund}\n\n"
            f"В описании лота: <code>ID: 1234</code> и опционально <code>#Quan: 10</code>",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    def handle_callback(call):
        nonlocal settings
        if call.data == "vb_set_api_url":
            result = bot.send_message(
                call.message.chat.id,
                f"Текущий URL: {get_api_url()}\n\nВведите новый API URL:",
            )
            tg.set_state(
                chat_id=call.message.chat.id,
                message_id=result.id,
                user_id=call.from_user.id,
                state="vb_api_url",
            )
            bot.answer_callback_query(call.id)
        elif call.data == "vb_set_api_key":
            result = bot.send_message(
                call.message.chat.id,
                "Введите API KEY из личного кабинета VexBoost:",
            )
            tg.set_state(
                chat_id=call.message.chat.id,
                message_id=result.id,
                user_id=call.from_user.id,
                state="vb_api_key",
            )
            bot.answer_callback_query(call.id)
        elif call.data == "vb_toggle_refund":
            settings["auto_refund_on_error"] = not settings.get("auto_refund_on_error", True)
            save_settings(settings)
            bot.answer_callback_query(call.id, "Настройка сохранена")
            send_settings(call.message)
        elif call.data == "vb_balance":
            balance = VexBoostAPI.get_balance()
            if balance:
                bot.answer_callback_query(
                    call.id,
                    f"Баланс: {balance[0]:.2f} {balance[1]}",
                    show_alert=True,
                )
            else:
                bot.answer_callback_query(call.id, "Не удалось получить баланс", show_alert=True)

    def handle_text_input(message):
        nonlocal settings
        state_data = tg.get_state(message.chat.id, message.from_user.id)
        if not state_data or "state" not in state_data:
            return
        state = state_data["state"]
        if state == "vb_api_url":
            settings["api_url"] = message.text.strip().rstrip("/")
            save_settings(settings)
            bot.reply_to(message, f"✅ API URL сохранён: {settings['api_url']}")
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
    tg.msg_handler(send_settings, commands=["vexboost"])
    cardinal.add_telegram_commands(UUID, [
        ("vexboost", f"настройки {NAME}", True),
    ])


logger.info("$MAGENTAVexBoost AutoSMM плагин загружен.$RESET")

BIND_TO_PRE_INIT = [init_commands]
BIND_TO_POST_INIT = [start_status_checker]
BIND_TO_NEW_ORDER = [bind_to_new_order]
BIND_TO_NEW_MESSAGE = [msg_hook]
BIND_TO_DELETE = None
