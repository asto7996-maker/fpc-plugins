"""
Плагин автонакрутки через VexBoost для FunPay Cardinal.

Настройка в config.json (секция vexboost):
{
    "vexboost": {
        "api_url": "https://vexboost.ru/api/v2",
        "api_key": "ВАШ_API_КЛЮЧ",
        "auto_refund_on_error": true,
        "allow_private_telegram": false,
        "status_check_interval": 60
    }
}

Настройка лотов — в описании лота укажите:
  ID: 1234          — ID услуги на VexBoost
  #Quan: 10         — (опционально) множитель количества

Покупатель отправляет ссылку, подтверждает заказ символом + или отменяет через -.
Команды: #статус <id>  |  #рефилл <id>
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

NAME = "VexBoost AutoSMM"
VERSION = "1.0.0"
DESCRIPTION = "Автонакрутка через сервис VexBoost (vexboost.ru)"
CREDITS = "Cursor AI"
UUID = "a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e"

DEFAULT_CONFIG = {
    "api_url": "https://vexboost.ru/api/v2",
    "api_key": "",
    "auto_refund_on_error": True,
    "allow_private_telegram": False,
    "status_check_interval": 60,
}

STORAGE_DIR = os.path.join("storage", "plugins", UUID)
PAY_ORDERS_FILE = os.path.join(STORAGE_DIR, "payorders.json")
ACTIVE_ORDERS_FILE = os.path.join(STORAGE_DIR, "active_orders.json")

URL_PATTERN = re.compile(
    r"https?://(?:[a-zA-Z0-9]|[$-_@.&+]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
)
SERVICE_ID_PATTERN = re.compile(r"ID:\s*(\d+)", re.IGNORECASE)
QUANTITY_MULT_PATTERN = re.compile(r"#Quan:\s*(\d+)", re.IGNORECASE)


class VexBoostAPI:
    """Клиент стандартного SMM API v2 (VexBoost)."""

    def __init__(self, api_url: str, api_key: str, logger: logging.Logger):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.logger = logger

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"key": self.api_key, **params}
        try:
            response = requests.post(self.api_url, data=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
            return {"error": "Некорректный ответ API"}
        except requests.RequestException as exc:
            self.logger.error("Ошибка запроса к VexBoost: %s", exc)
            return {"error": str(exc)}
        except ValueError:
            self.logger.error("VexBoost вернул не-JSON ответ")
            return {"error": "Некорректный ответ API"}

    def get_balance(self) -> Optional[Tuple[float, str]]:
        data = self._request({"action": "balance"})
        if "balance" in data:
            balance = float(re.search(r"[\d.]+", str(data["balance"])).group())
            return balance, data.get("currency", "RUB")
        return None

    def create_order(self, service_id: int, link: str, quantity: int) -> Any:
        data = self._request({
            "action": "add",
            "service": service_id,
            "link": link,
            "quantity": quantity,
        })
        if "order" in data:
            return data["order"]
        return data.get("error", "Неизвестная ошибка")

    def get_order_status(self, order_id: int) -> Optional[Dict[str, Any]]:
        data = self._request({"action": "status", "order": order_id})
        if "error" in data:
            self.logger.warning("Статус заказа %s: %s", order_id, data["error"])
            return None
        return data

    def refill_order(self, order_id: int) -> Optional[Any]:
        data = self._request({"action": "refill", "order": order_id})
        return data.get("refill")


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


def _extract_links(text: str) -> List[str]:
    return URL_PATTERN.findall(text)


class Plugin:
    def __init__(self, cardinal, config):
        self.cardinal = cardinal
        self.config = {**DEFAULT_CONFIG, **config.get("vexboost", config)}
        self.logger = logging.getLogger(__name__)
        self.api = VexBoostAPI(
            self.config["api_url"],
            self.config["api_key"],
            self.logger,
        )
        self.pending_confirmations: Dict[Any, Dict[str, Any]] = {}
        self._status_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.logger.info("%s v%s загружен", NAME, VERSION)

    def setup(self):
        self.cardinal.event_manager.register_handler("on_order", self.on_order)
        self.cardinal.event_manager.register_handler("on_message", self.on_message)
        self._stop_event.clear()
        self._status_thread = threading.Thread(
            target=self._status_checker_loop,
            name="VexBoostStatusChecker",
            daemon=True,
        )
        self._status_thread.start()
        self.logger.info("Обработчики VexBoost зарегистрированы")

    def unload(self):
        self._stop_event.set()
        self.cardinal.event_manager.unregister_handler("on_order", self.on_order)
        self.cardinal.event_manager.unregister_handler("on_message", self.on_message)
        self.logger.info("%s выгружен", NAME)

    # ------------------------------------------------------------------
    # Обработка заказов
    # ------------------------------------------------------------------

    def on_order(self, data: Dict[str, Any]) -> None:
        if not self.config.get("api_key"):
            self.logger.warning("API-ключ VexBoost не задан — заказ пропущен")
            return

        description = self._get_order_description(data)
        match_id = SERVICE_ID_PATTERN.search(description)
        if not match_id:
            self.logger.debug("В описании заказа нет ID: — автонакрутка не требуется")
            return

        service_id = int(match_id.group(1))
        multiplier = 1
        match_quan = QUANTITY_MULT_PATTERN.search(description)
        if match_quan:
            multiplier = max(1, int(match_quan.group(1)))

        amount = int(data.get("amount", 1)) * multiplier
        buyer = data.get("buyer_username", data.get("buyer", ""))
        order_id = str(data.get("order_id", data.get("id", "")))

        order_entry = {
            "OrderID": order_id,
            "Amount": amount,
            "OrderPrice": data.get("price", 0),
            "OrderCurrency": data.get("currency", "₽"),
            "Order": data.get("description", description),
            "service_id": service_id,
            "buyer": buyer,
            "url": "",
            "chat_id": data.get("chat_id", ""),
            "OrderDateTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        pay_orders: List[Dict[str, Any]] = _load_json(PAY_ORDERS_FILE, [])
        pay_orders.append(order_entry)
        _save_json(PAY_ORDERS_FILE, pay_orders)

        chat_id = order_entry["chat_id"]
        if chat_id:
            self.cardinal.send_message(
                chat_id,
                "👋 Спасибо за заказ!\n"
                "Отправьте ссылку на аккаунт/пост для накрутки.",
            )
        self.logger.info(
            "Новый заказ на автонакрутку: FunPay #%s, service=%s, qty=%s",
            order_id, service_id, amount,
        )

    def on_message(self, data: Dict[str, Any]) -> None:
        message = (data.get("message") or data.get("text") or "").strip()
        chat_id = data.get("chat_id")
        chat_name = data.get("chat_name", data.get("buyer", ""))
        author_id = data.get("author_id")

        if not message or not chat_id:
            return

        if data.get("is_system") or data.get("type") == "system":
            return

        account_id = getattr(getattr(self.cardinal, "account", None), "id", None)
        if account_id and author_id == account_id:
            return

        if "вернул деньги покупателю" in message.lower():
            self._remove_pay_order(chat_name)
            return

        if chat_id in self.pending_confirmations:
            self._handle_pending(chat_id, message)
            return

        if message.startswith("#статус"):
            self._cmd_status(chat_id, message)
            return

        if message.startswith("#рефилл"):
            self._cmd_refill(chat_id, message)
            return

        pay_order = self._find_pay_order(chat_name)
        if not pay_order:
            return

        links = _extract_links(message)
        if links:
            pay_order["chat_id"] = chat_id
            self._request_confirmation(pay_order, links[0])

    # ------------------------------------------------------------------
    # Логика подтверждения и создания заказа
    # ------------------------------------------------------------------

    def _request_confirmation(self, order: Dict[str, Any], link: str) -> None:
        if not self.config.get("allow_private_telegram"):
            if "t.me" in link and ("/c/" in link or "+" in link):
                self.cardinal.send_message(
                    order["chat_id"],
                    "❌ Закрытые Telegram-каналы/группы не поддерживаются.\n"
                    "Используйте публичную ссылку вида https://t.me/channel",
                )
                return

        order["url"] = link
        display_link = link.replace("https://", "").replace("http://", "")
        self.cardinal.send_message(
            order["chat_id"],
            f"📋 Проверьте детали заказа:\n"
            f"🛒 Лот: {order['Order']}\n"
            f"🔢 Количество: {order['Amount']} шт.\n"
            f"🔗 Ссылка: {display_link}\n\n"
            f"✅ Отправьте + для подтверждения\n"
            f"❌ Отправьте - для отмены и возврата\n"
            f"🔄 Или отправьте новую ссылку",
        )
        self.pending_confirmations[order["chat_id"]] = order
        self._update_pay_order(order)

    def _handle_pending(self, chat_id: Any, message: str) -> None:
        order = self.pending_confirmations.get(chat_id)
        if not order:
            return

        if message in ("+", "-"):
            self.pending_confirmations.pop(chat_id, None)
            if message == "+":
                self._create_smm_order(order)
            else:
                self._cancel_order(order)
            return

        links = _extract_links(message)
        if links:
            self._request_confirmation(order, links[0])
        else:
            self.cardinal.send_message(
                chat_id,
                "⚪️ Отправьте + для подтверждения, - для отмены или новую ссылку.",
            )

    def _create_smm_order(self, order: Dict[str, Any]) -> None:
        result = self.api.create_order(
            order["service_id"],
            order["url"],
            order["Amount"],
        )

        if isinstance(result, int) or (isinstance(result, str) and str(result).isdigit()):
            smm_id = int(result)
            active: Dict[str, Any] = _load_json(ACTIVE_ORDERS_FILE, {})
            active[str(smm_id)] = {
                "service_id": order["service_id"],
                "chat_id": order["chat_id"],
                "order_id": order["OrderID"],
                "order_url": order["url"],
                "order_amount": order["Amount"],
                "status": "pending",
            }
            _save_json(ACTIVE_ORDERS_FILE, active)
            self._remove_pay_order(order["buyer"])

            self.cardinal.send_message(
                order["chat_id"],
                f"📊 Заказ создан и отправлен в VexBoost!\n"
                f"🆔 ID заказа: {smm_id}\n\n"
                f"📋 Команды:\n"
                f"⠀∟ #статус {smm_id}\n"
                f"⠀∟ #рефилл {smm_id}\n\n"
                f"⌛ Время выполнения: от нескольких минут до 48 часов.",
            )
            self.logger.info("VexBoost заказ #%s создан для FunPay #%s", smm_id, order["OrderID"])
            return

        error_text = str(result)
        self.cardinal.send_message(
            order["chat_id"],
            f"❌ Ошибка при создании заказа: {error_text}",
        )
        self.logger.error("Ошибка VexBoost для FunPay #%s: %s", order["OrderID"], error_text)

        if self.config.get("auto_refund_on_error"):
            self._refund_funpay_order(order["OrderID"])

    def _cancel_order(self, order: Dict[str, Any]) -> None:
        self.cardinal.send_message(order["chat_id"], "❌ Заказ отменён.")
        self._remove_pay_order(order["buyer"])
        self._refund_funpay_order(order["OrderID"])

    # ------------------------------------------------------------------
    # Команды покупателя
    # ------------------------------------------------------------------

    def _cmd_status(self, chat_id: Any, message: str) -> None:
        parts = message.split()
        if len(parts) < 2 or not parts[1].isdigit():
            self.cardinal.send_message(chat_id, "Использование: #статус <id_заказа>")
            return

        status = self.api.get_order_status(int(parts[1]))
        if not status:
            self.cardinal.send_message(chat_id, "🔴 Не удалось получить статус заказа.")
            return

        start_count = status.get("start_count", 0)
        display_start = "*" if start_count == 0 else str(start_count)
        self.cardinal.send_message(
            chat_id,
            f"📈 Статус заказа: {parts[1]}\n"
            f"⠀∟ 📊 Статус: {status.get('status', '—')}\n"
            f"⠀∟ 🔢 Было: {display_start}\n"
            f"⠀∟ 👀 Остаток: {status.get('remains', '—')}",
        )

    def _cmd_refill(self, chat_id: Any, message: str) -> None:
        parts = message.split()
        if len(parts) < 2 or not parts[1].isdigit():
            self.cardinal.send_message(chat_id, "Использование: #рефилл <id_заказа>")
            return

        result = self.api.refill_order(int(parts[1]))
        if result is not None:
            self.cardinal.send_message(chat_id, "✅ Запрос на рефилл отправлен!")
        else:
            self.cardinal.send_message(
                chat_id,
                "🔴 Ошибка рефилла. Возможно, рефилл ещё недоступен для этой услуги.",
            )

    # ------------------------------------------------------------------
    # Фоновая проверка статусов
    # ------------------------------------------------------------------

    def _status_checker_loop(self) -> None:
        interval = max(30, int(self.config.get("status_check_interval", 60)))
        while not self._stop_event.is_set():
            try:
                self._check_active_orders()
            except Exception as exc:
                self.logger.error("Ошибка проверки статусов: %s", exc)
            self._stop_event.wait(interval)

    def _check_active_orders(self) -> None:
        if not self.config.get("api_key"):
            return

        active: Dict[str, Any] = _load_json(ACTIVE_ORDERS_FILE, {})
        if not active:
            return

        to_remove: List[str] = []
        for smm_id, info in active.items():
            status_data = self.api.get_order_status(int(smm_id))
            if not status_data:
                continue

            status = status_data.get("status", "")
            info["status"] = status
            chat_id = info.get("chat_id")
            funpay_id = info.get("order_id", "")

            if status == "Completed":
                if chat_id:
                    self.cardinal.send_message(
                        chat_id,
                        f"✅ Заказ #{funpay_id} выполнен!\n"
                        f"Пожалуйста, подтвердите выполнение на FunPay:\n"
                        f"https://funpay.com/orders/{funpay_id}/",
                    )
                to_remove.append(smm_id)
                self.logger.info("VexBoost #%s выполнен (FunPay #%s)", smm_id, funpay_id)

            elif status == "Canceled":
                if chat_id:
                    self.cardinal.send_message(
                        chat_id,
                        f"❌ Заказ #{funpay_id} отменён на стороне VexBoost.",
                    )
                self._refund_funpay_order(funpay_id)
                to_remove.append(smm_id)
                self.logger.warning("VexBoost #%s отменён (FunPay #%s)", smm_id, funpay_id)

        for smm_id in to_remove:
            active.pop(smm_id, None)
        _save_json(ACTIVE_ORDERS_FILE, active)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _get_order_description(self, data: Dict[str, Any]) -> str:
        description = data.get("full_description") or data.get("description", "")
        order_id = data.get("order_id", data.get("id"))
        if order_id and hasattr(self.cardinal, "account"):
            try:
                full_order = self.cardinal.account.get_order(order_id)
                description = getattr(full_order, "full_description", description) or description
            except Exception:
                pass
        return description

    def _find_pay_order(self, buyer: str) -> Optional[Dict[str, Any]]:
        pay_orders: List[Dict[str, Any]] = _load_json(PAY_ORDERS_FILE, [])
        for order in pay_orders:
            if order.get("buyer") == buyer:
                return order
        return None

    def _update_pay_order(self, order: Dict[str, Any]) -> None:
        pay_orders: List[Dict[str, Any]] = _load_json(PAY_ORDERS_FILE, [])
        for idx, existing in enumerate(pay_orders):
            if existing.get("OrderID") == order.get("OrderID"):
                pay_orders[idx] = order
                break
        else:
            pay_orders.append(order)
        _save_json(PAY_ORDERS_FILE, pay_orders)

    def _remove_pay_order(self, buyer: str) -> None:
        pay_orders: List[Dict[str, Any]] = _load_json(PAY_ORDERS_FILE, [])
        pay_orders = [o for o in pay_orders if o.get("buyer") != buyer]
        _save_json(PAY_ORDERS_FILE, pay_orders)

    def _refund_funpay_order(self, order_id: str) -> None:
        if not order_id:
            return
        account = getattr(self.cardinal, "account", None)
        if account and hasattr(account, "refund"):
            try:
                account.refund(order_id)
                self.logger.info("Возврат средств по заказу FunPay #%s", order_id)
            except Exception as exc:
                self.logger.error("Ошибка возврата FunPay #%s: %s", order_id, exc)
