from __future__ import annotations

# === ОБЯЗАТЕЛЬНЫЕ ПОЛЯ FunPay Cardinal (НЕ УДАЛЯТЬ) ===
NAME = "Gemini Link Auto"
VERSION = "1.0.2"
DESCRIPTION = "Автовыдача Gemini link (18 мес.) через API Telegram-бота поставщика"
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
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from FunPayAPI.types import MessageTypes
from FunPayAPI.updater.events import LastChatMessageChangedEvent, NewMessageEvent, NewOrderEvent
from cardinal import Cardinal
from tg_bot import CBT

logger = logging.getLogger("FPC.GeminiLink")
_P = "GeminiLink"

STORAGE_DIR = f"storage/plugins/{UUID}"
SETTINGS_FILE = f"{STORAGE_DIR}/settings.json"
PROCESSED_FILE = f"{STORAGE_DIR}/processed_orders.json"
PREORDER_FILE = f"{STORAGE_DIR}/preorders.json"
ATTENTION_FILE = f"{STORAGE_DIR}/manual_attention.json"

_file_lock = threading.Lock()
_disabled_chats: Dict[str, float] = {}
_plugin: Optional["Plugin"] = None
_tg_bot_instance_id: Optional[int] = None

DEFAULT_SETTINGS: Dict[str, Any] = {
    "bot_api_url": "",
    "bot_api_key": "",
    "product_keywords": ["Gemini link"],
    "bot_product_id": "gemini_18m",
    "api_path_balance": "/api/v1/balance",
    "api_path_stock": "/api/v1/stock",
    "api_path_purchase": "/api/v1/purchase",
    "api_retry_count": 3,
    "api_retry_delay": 5,
    "notify_seller": True,
    "delivery_message": (
        "🎉 Ваша подписка Gemini готова!\n\n"
        "🔗 Ссылка для активации (18 месяцев):\n"
        "{link}\n\n"
        "📌 Перейдите по ссылке и следуйте инструкции Google.\n"
        "📋 Заказ: #{order_id}\n\n"
        "Спасибо за покупку! ⭐"
    ),
    "delay_message": (
        "🛠 Техническая задержка\n\n"
        "При автоматической выдаче подписки Gemini произошла временная ошибка.\n"
        "Продавец уже уведомлён и обработает заказ вручную.\n\n"
        "📋 Заказ: #{order_id}\n"
        "Приносим извинения за неудобства! 🙏"
    ),
    "out_of_stock_message": (
        "⏳ Gemini Link — временно нет в наличии\n\n"
        "Ваш заказ поставлен в очередь предзаказа.\n"
        "📋 ID заказа: #{order_id}\n\n"
        "Ссылка будет выдана сразу после пополнения склада.\n"
        "💬 Срочно? Напишите /Gemini help"
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
    return merged


def save_settings(data: Dict[str, Any]) -> None:
    _save_json(SETTINGS_FILE, data)


def load_processed() -> List[str]:
    return _load_json(PROCESSED_FILE, [])


def save_processed(items: List[str]) -> None:
    _save_json(PROCESSED_FILE, items)


def load_preorders() -> List[Dict[str, Any]]:
    return _load_json(PREORDER_FILE, [])


def save_preorders(items: List[Dict[str, Any]]) -> None:
    _save_json(PREORDER_FILE, items)


def load_attention() -> List[Dict[str, Any]]:
    return _load_json(ATTENTION_FILE, [])


def save_attention(items: List[Dict[str, Any]]) -> None:
    _save_json(ATTENTION_FILE, items)


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


def _matches_keywords(text: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    s = settings or load_settings()
    lower = (text or "").lower()
    keywords = s.get("product_keywords") or DEFAULT_SETTINGS["product_keywords"]
    return any(kw.lower() in lower for kw in keywords)


def send_fp(c: Cardinal, chat_id: Any, text: str) -> None:
    if not chat_id:
        logger.warning("%s: send_fp без chat_id", _P)
        return
    c.send_message(chat_id, text)


def _format_template(key: str, **kwargs: Any) -> str:
    settings = load_settings()
    template = settings.get(key, DEFAULT_SETTINGS.get(key, ""))
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError):
        result = str(template)
        for k, v in kwargs.items():
            result = result.replace("{" + k + "}", str(v))
        return result


# ─────────────────────────────────────────────────────────────────────────────
# API Telegram-бота поставщика
# ─────────────────────────────────────────────────────────────────────────────

class SupplierBotAPI:
    HEADERS = {"Accept": "application/json", "User-Agent": f"FunPayCardinal/{VERSION}"}

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
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @classmethod
    def _retry_params(cls) -> Tuple[int, int]:
        s = cls._settings()
        return int(s.get("api_retry_count", 3)), int(s.get("api_retry_delay", 5))

    @classmethod
    def _request(cls, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        retries, delay = cls._retry_params()
        url = f"{cls._base_url()}{path}"
        last_error = "unknown"

        for attempt in range(1, retries + 1):
            try:
                _log_action(f"API {method} {path}", attempt=attempt)
                resp = requests.request(
                    method,
                    url,
                    headers=cls._auth_headers(),
                    json=json_body,
                    timeout=20,
                )
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                data = resp.json() if resp.content else {}
                if not resp.ok:
                    raise requests.HTTPError(data.get("error") or data.get("message") or f"HTTP {resp.status_code}")
                return data if isinstance(data, dict) else {"data": data}
            except (requests.RequestException, ValueError) as exc:
                last_error = str(exc)
                logger.warning("%s: API попытка %s/%s — %s", _P, attempt, retries, exc)
                if attempt < retries:
                    time.sleep(delay)

        raise RuntimeError(f"API недоступен после {retries} попыток: {last_error}")

    @classmethod
    def get_balance(cls) -> Dict[str, Any]:
        s = cls._settings()
        data = cls._request("GET", s["api_path_balance"])
        return {
            "balance": float(data.get("balance", data.get("amount", 0))),
            "currency": data.get("currency", "RUB"),
        }

    @classmethod
    def get_stock(cls) -> Dict[str, Any]:
        s = cls._settings()
        product = s.get("bot_product_id", "gemini_18m")
        path = f"{s['api_path_stock']}?product={product}"
        data = cls._request("GET", path)
        available = int(data.get("available", data.get("quantity", data.get("stock", 0))))
        status = data.get("status", "out_of_stock" if available <= 0 else "ok")
        return {
            "available": available,
            "price": float(data["price"]) if data.get("price") is not None else None,
            "status": status,
        }

    @classmethod
    def purchase(cls, order_id: str) -> Dict[str, Any]:
        s = cls._settings()
        data = cls._request(
            "POST",
            s["api_path_purchase"],
            {"product": s.get("bot_product_id", "gemini_18m"), "order_id": order_id},
        )
        link = (
            data.get("activation_link")
            or data.get("link")
            or data.get("url")
            or data.get("activationLink")
        )
        status = data.get("status", "out_of_stock" if data.get("error") == "out_of_stock" else "ok")
        if not link and status != "out_of_stock":
            raise RuntimeError("API не вернул activation_link")
        return {"activation_link": link, "status": status}


# ─────────────────────────────────────────────────────────────────────────────
# Основной класс плагина
# ─────────────────────────────────────────────────────────────────────────────

class Plugin:
    def __init__(self, cardinal: Cardinal) -> None:
        self.cardinal = cardinal
        self._processing_lock = threading.Lock()
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

    def _already_processed(self, order_id: str) -> bool:
        return order_id in load_processed()

    def _mark_processed(self, order_id: str) -> None:
        items = load_processed()
        if order_id not in items:
            items.append(order_id)
            save_processed(items)

    def _add_attention(self, order_id: str, chat_id: Any, reason: str) -> None:
        items = load_attention()
        if not any(x.get("order_id") == order_id for x in items):
            items.append({"order_id": order_id, "chat_id": chat_id, "reason": reason, "at": _ts()})
            save_attention(items)
        self.notify_seller(
            f"⚠️ <b>{NAME}</b>\n\n"
            f"Заказ <code>#{order_id}</code> требует ручного внимания.\n"
            f"Причина: <b>{html.escape(reason)}</b>"
        )

    def _add_preorder(self, order_id: str, chat_id: Any) -> None:
        items = load_preorders()
        if not any(x.get("order_id") == order_id for x in items):
            items.append({"order_id": order_id, "chat_id": chat_id, "at": _ts(), "status": "waiting"})
            save_preorders(items)

    def process_order(self, order_id: str) -> None:
        with self._processing_lock:
            if self._already_processed(order_id):
                _log_action("Заказ уже обработан — пропуск", order_id=order_id)
                return

        settings = load_settings()
        if not _is_configured(settings):
            logger.warning("%s: API бота не настроен", _P)
            return

        try:
            full_order = self.cardinal.account.get_order(order_id)
        except Exception as exc:
            logger.error("%s: get_order #%s: %s", _P, order_id, exc)
            return

        description = full_order.full_description or full_order.short_description or ""
        if not _matches_keywords(description, settings):
            return

        buyer = full_order.buyer_username
        chat = self.cardinal.account.get_chat_by_name(buyer)
        chat_id = chat.id if chat else getattr(full_order, "chat_id", None)

        _log_action("Обнаружен заказ Gemini link", order_id=order_id, buyer=buyer)

        try:
            balance = SupplierBotAPI.get_balance()
            _log_action("Баланс проверен", order_id=order_id, balance=balance["balance"])

            stock = SupplierBotAPI.get_stock()
            _log_action("Наличие проверено", order_id=order_id, available=stock["available"])

            if stock["status"] == "out_of_stock" or stock["available"] <= 0:
                self._handle_out_of_stock(order_id, chat_id)
                return

            if stock["price"] is not None and balance["balance"] < stock["price"]:
                self._add_attention(order_id, chat_id, "insufficient_balance")
                if chat_id:
                    send_fp(self.cardinal, chat_id, _format_template("delay_message", order_id=order_id))
                return

            _log_action("Запрос покупки к боту", order_id=order_id)
            purchase = SupplierBotAPI.purchase(order_id)

            if purchase["status"] == "out_of_stock":
                self._handle_out_of_stock(order_id, chat_id)
                return

            link = purchase["activation_link"]
            _log_action("Ссылка получена", order_id=order_id)

            if chat_id:
                send_fp(
                    self.cardinal,
                    chat_id,
                    _format_template("delivery_message", order_id=order_id, link=link),
                )
                _log_action("Ссылка отправлена покупателю", order_id=order_id, chat_id=chat_id)
            else:
                self._add_attention(order_id, None, "no_chat_id")

            self._mark_processed(order_id)
            self.notify_seller(
                f"✅ <b>{NAME}</b>\n\n"
                f"Заказ <code>#{order_id}</code> выполнен автоматически.\n"
                f"👤 {html.escape(buyer or '—')}"
            )
        except Exception as exc:
            logger.error("%s: process_order #%s: %s", _P, order_id, exc)
            logger.debug(traceback.format_exc())
            self._add_attention(order_id, chat_id, "api_failure")
            if chat_id:
                send_fp(self.cardinal, chat_id, _format_template("delay_message", order_id=order_id))

    def _handle_out_of_stock(self, order_id: str, chat_id: Any) -> None:
        _log_action("Out of stock — предзаказ", order_id=order_id)
        self._add_preorder(order_id, chat_id)
        if chat_id:
            send_fp(self.cardinal, chat_id, _format_template("out_of_stock_message", order_id=order_id))
        self._add_attention(order_id, chat_id, "out_of_stock")

    # ── Команды /Gemini в чате FunPay ────────────────────────────────────────

    def _chat_disabled(self, chat_id: Any) -> bool:
        key = str(chat_id)
        until = _disabled_chats.get(key)
        if until and time.time() < until:
            return True
        if until:
            _disabled_chats.pop(key, None)
        return False

    def handle_gemini_command(self, text: str, chat_id: Any) -> None:
        if self._chat_disabled(chat_id):
            return

        parts = text.strip().split()
        sub = parts[1].lower() if len(parts) > 1 else ""

        _log_action("Команда /Gemini", chat_id=chat_id, sub=sub or "menu")

        if not sub:
            send_fp(self.cardinal, chat_id, self._main_menu())
            return

        if sub in ("check", "stock", "наличие"):
            self._cmd_check(chat_id)
        elif sub in ("preorder", "предзаказ"):
            self._cmd_preorder(chat_id)
        elif sub in ("help", "помощь"):
            self._cmd_help(chat_id)
        else:
            send_fp(self.cardinal, chat_id, self._main_menu())

    def _main_menu(self) -> str:
        return (
            "🌟 Gemini Link — Меню\n\n"
            "📦 /Gemini check — Проверить наличие\n"
            "📝 /Gemini preorder — Сделать предзаказ\n"
            "🆘 /Gemini help — Позвать продавца\n\n"
            "💡 После оплаты ссылка выдаётся автоматически."
        )

    def _cmd_check(self, chat_id: Any) -> None:
        try:
            stock = SupplierBotAPI.get_stock()
            msg = (
                f"📦 Проверка наличия\n\n"
                f"✅ В наличии: {stock['available']} шт.\n\n"
                + (
                    "🛒 Можете оформить заказ — ссылка придёт после оплаты!"
                    if stock["available"] > 0
                    else "⏳ Нет в наличии. Используйте /Gemini preorder"
                )
            )
            send_fp(self.cardinal, chat_id, msg)
        except Exception as exc:
            send_fp(self.cardinal, chat_id, "⚠️ Не удалось проверить наличие. Попробуйте /Gemini help")
            logger.error("%s: cmd check: %s", _P, exc)

    def _cmd_preorder(self, chat_id: Any) -> None:
        items = load_preorders()
        items.append({"chat_id": chat_id, "type": "intent", "at": _ts(), "status": "intent"})
        save_preorders(items)
        send_fp(
            self.cardinal,
            chat_id,
            "📝 Предзаказ Gemini Link (18 мес.)\n\n"
            "Вы в списке предзаказа ✅\n\n"
            "1. Оформите заказ на лот «Gemini link»\n"
            "2. После оплаты ссылка выдаётся автоматически\n"
            "3. При отсутствии товара — приоритетная очередь\n\n"
            "💬 /Gemini help — связь с продавцом",
        )

    def _cmd_help(self, chat_id: Any) -> None:
        _disabled_chats[str(chat_id)] = time.time() + 30 * 60
        _log_action("🆘 Требуется продавец", chat_id=chat_id)
        self.notify_seller(f"🆘 <b>{NAME}</b>\n\nПокупатель просит помощь.\n💬 chat_id: <code>{chat_id}</code>")
        send_fp(
            self.cardinal,
            chat_id,
            "🆘 Продавец уведомлён!\n\n"
            "Автоответчик отключён на 30 минут.\n"
            "Продавец ответит лично в ближайшее время.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram UI (настройки продавца)
# ─────────────────────────────────────────────────────────────────────────────

def _settings_summary() -> str:
    s = load_settings()
    configured = _is_configured(s)
    status = "🟢 Настроен" if configured else "🔴 Не настроен"
    return (
        f"⚙️ <b>{NAME}</b> v{VERSION}\n\n"
        f"Статус API: {status}\n"
        f"🌐 URL: <code>{html.escape(s.get('bot_api_url') or '—')}</code>\n"
        f"🔑 Key: <code>{html.escape(_mask_key(s.get('bot_api_key', '')))}</code>\n"
        f"🏷 Ключевые слова: <code>{html.escape(', '.join(s.get('product_keywords', [])))}</code>\n"
        f"📦 Product ID: <code>{html.escape(s.get('bot_product_id', ''))}</code>\n\n"
        f"Команды:\n"
        f"⠀∟ /gemini_link — эта панель\n"
        f"⠀∟ /gl_balance — баланс бота\n"
        f"⠀∟ /gl_stock — наличие"
    )


def _main_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🌐 URL API", callback_data="gla:set_url"),
        InlineKeyboardButton("🔑 API Key", callback_data="gla:set_key"),
    )
    kb.add(
        InlineKeyboardButton("🏷 Ключевые слова", callback_data="gla:set_keywords"),
        InlineKeyboardButton("🔄 Обновить", callback_data="gla:main"),
    )
    kb.add(
        InlineKeyboardButton("💰 Баланс бота", callback_data="gla:balance"),
        InlineKeyboardButton("📦 Наличие", callback_data="gla:stock"),
    )
    kb.add(InlineKeyboardButton("📋 Очередь / внимание", callback_data="gla:queue"))
    return kb


def _queue_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="gla:main"))
    return kb


def _queue_summary() -> str:
    pre = load_preorders()
    att = load_attention()
    lines = [
        f"📋 <b>Очередь и внимание</b>\n",
        f"Предзаказы: <b>{len(pre)}</b>",
        f"Ручное внимание: <b>{len(att)}</b>",
    ]
    if att:
        lines.append("\n<b>⚠️ Требуют внимания:</b>")
        for item in att[-5:]:
            lines.append(f"• #{item.get('order_id')} — {html.escape(item.get('reason', '?'))}")
    if pre:
        lines.append("\n<b>📝 Предзаказы:</b>")
        for item in pre[-5:]:
            oid = item.get("order_id") or item.get("chat_id") or "—"
            lines.append(f"• {html.escape(str(oid))}")
    return "\n".join(lines)


def _edit_or_send_panel(bot, chat_id: int, msg_id: Optional[int] = None) -> None:
    text = _settings_summary()
    markup = _main_keyboard()
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
        logger.warning("%s: Telegram отключён — команды не зарегистрированы", _P)
        return

    tg = cardinal.telegram
    bot = tg.bot
    bot_id = id(bot)
    if _tg_bot_instance_id == bot_id:
        logger.debug("%s: Telegram-обработчики уже зарегистрированы", _P)
        return
    _tg_bot_instance_id = bot_id

    def _answer(call, text: str = "", alert: bool = False) -> None:
        try:
            if text:
                bot.answer_callback_query(call.id, text[:200], show_alert=alert)
            else:
                bot.answer_callback_query(call.id)
        except Exception as exc:
            logger.debug("%s: answer_callback_query: %s", _P, exc)

    def cmd_panel(msg):
        _edit_or_send_panel(bot, msg.chat.id)

    def cmd_balance(msg):
        if not _is_configured():
            bot.reply_to(msg, "🔴 Сначала настройте URL и API Key в /gemini_link")
            return
        try:
            data = SupplierBotAPI.get_balance()
            bot.reply_to(msg, f"💰 Баланс бота: <b>{data['balance']:.2f}</b> {data['currency']}", parse_mode="HTML")
        except Exception as exc:
            bot.reply_to(msg, f"🔴 Ошибка: {html.escape(str(exc))}", parse_mode="HTML")

    def cmd_stock(msg):
        if not _is_configured():
            bot.reply_to(msg, "🔴 Сначала настройте URL и API Key в /gemini_link")
            return
        try:
            data = SupplierBotAPI.get_stock()
            bot.reply_to(msg, f"📦 В наличии: <b>{data['available']}</b> шт.", parse_mode="HTML")
        except Exception as exc:
            bot.reply_to(msg, f"🔴 Ошибка: {html.escape(str(exc))}", parse_mode="HTML")

    def on_plugin_settings(call):
        try:
            _edit_or_send_panel(bot, call.message.chat.id, call.message.message_id)
            _answer(call)
        except Exception as exc:
            logger.error("%s: on_plugin_settings: %s", _P, exc)
            _answer(call, f"Ошибка: {exc}", alert=True)

    def on_callback(call):
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        action = (call.data or "").split(":", 1)[-1] if (call.data or "").startswith("gla:") else ""

        try:
            if action == "main":
                _edit_or_send_panel(bot, chat_id, msg_id)
                _answer(call, "Обновлено")

            elif action == "set_url":
                _answer(call)
                r = bot.send_message(chat_id, "🌐 Введите <b>BOT_API_URL</b>:\n/cancel — отмена", parse_mode="HTML")
                tg.set_state(chat_id, r.message_id, call.from_user.id, state="gla_url")

            elif action == "set_key":
                _answer(call)
                r = bot.send_message(chat_id, "🔑 Введите <b>BOT_API_KEY</b>:\n/cancel — отмена", parse_mode="HTML")
                tg.set_state(chat_id, r.message_id, call.from_user.id, state="gla_key")

            elif action == "set_keywords":
                _answer(call)
                r = bot.send_message(
                    chat_id,
                    "🏷 Введите ключевые слова через запятую:\n"
                    "<i>например: Gemini link, gemini 18</i>\n/cancel — отмена",
                    parse_mode="HTML",
                )
                tg.set_state(chat_id, r.message_id, call.from_user.id, state="gla_keywords")

            elif action == "balance":
                if not _is_configured():
                    _answer(call, "Сначала укажите URL и API Key", alert=True)
                    return
                bal = SupplierBotAPI.get_balance()
                _answer(call, f"💰 {bal['balance']:.2f} {bal['currency']}", alert=True)

            elif action == "stock":
                if not _is_configured():
                    _answer(call, "Сначала укажите URL и API Key", alert=True)
                    return
                st = SupplierBotAPI.get_stock()
                _answer(call, f"📦 В наличии: {st['available']} шт.", alert=True)

            elif action == "queue":
                bot.edit_message_text(
                    _queue_summary(), chat_id, msg_id,
                    parse_mode="HTML", reply_markup=_queue_keyboard(),
                )
                _answer(call)

            else:
                _answer(call)

        except Exception as exc:
            logger.error("%s: callback %s: %s", _P, call.data, exc)
            logger.debug(traceback.format_exc())
            _answer(call, str(exc)[:200], alert=True)

    def on_text(msg):
        state_data = tg.get_state(msg.chat.id, msg.from_user.id)
        if not state_data or "state" not in state_data:
            return
        state = state_data["state"]
        if state not in ("gla_url", "gla_key", "gla_keywords"):
            return

        settings = load_settings()
        text = (msg.text or "").strip()

        if text.lower() in ("/cancel", "отмена"):
            tg.clear_state(msg.chat.id, msg.from_user.id, True)
            bot.reply_to(msg, "❌ Отменено")
            _edit_or_send_panel(bot, msg.chat.id)
            return

        if state == "gla_url":
            settings["bot_api_url"] = text.rstrip("/")
            label = "URL API"
        elif state == "gla_key":
            settings["bot_api_key"] = text
            label = "API Key"
            try:
                bot.delete_message(msg.chat.id, msg.message_id)
            except Exception:
                pass
        elif state == "gla_keywords":
            settings["product_keywords"] = [k.strip() for k in text.split(",") if k.strip()]
            label = "Ключевые слова"
        else:
            return

        save_settings(settings)
        tg.clear_state(msg.chat.id, msg.from_user.id, True)
        bot.send_message(msg.chat.id, f"✅ {label} сохранён.", parse_mode="HTML")
        _edit_or_send_panel(bot, msg.chat.id)

    def _has_input_state(msg):
        for st in ("gla_url", "gla_key", "gla_keywords"):
            if tg.check_state(msg.chat.id, msg.from_user.id, st):
                return True
        return False

    def _is_gla_callback(call) -> bool:
        return (call.data or "").startswith("gla:")

    def _is_plugin_settings(call) -> bool:
        return f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (call.data or "")

    tg.msg_handler(cmd_panel, func=lambda m: m.text and m.text.split()[0].lower() in ("/gemini_link", "/gemini"))
    tg.msg_handler(cmd_balance, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_balance")
    tg.msg_handler(cmd_stock, func=lambda m: m.text and m.text.split()[0].lower() == "/gl_stock")
    tg.cbq_handler(on_callback, func=_is_gla_callback)
    tg.cbq_handler(on_plugin_settings, func=_is_plugin_settings)
    tg.msg_handler(on_text, func=_has_input_state)

    try:
        cardinal.add_telegram_commands(UUID, [
            ("gemini_link", f"панель {NAME}", True),
            ("gl_balance", f"баланс бота {NAME}", True),
            ("gl_stock", f"наличие {NAME}", True),
        ])
    except Exception as exc:
        logger.error("%s: add_telegram_commands: %s", _P, exc)

    logger.info("%s: Telegram UI зарегистрирован", _P)


# ─────────────────────────────────────────────────────────────────────────────
# Обработчики событий FunPay
# ─────────────────────────────────────────────────────────────────────────────

def init_plugin(cardinal: Cardinal, *_args) -> None:
    global _plugin
    _plugin = Plugin(cardinal)
    setup_telegram(cardinal)
    logger.info("%s v%s загружен", _P, VERSION)


def on_new_order(cardinal: Cardinal, event: NewOrderEvent) -> None:
    if not _plugin:
        return
    order_id = str(event.order.id)
    threading.Thread(target=_plugin.process_order, args=(order_id,), daemon=True).start()


def on_new_message(cardinal: Cardinal, event: NewMessageEvent) -> None:
    if not _plugin:
        return
    msg = event.message
    if msg.type != MessageTypes.NON_SYSTEM:
        return
    if msg.author_id == cardinal.account.id:
        return
    text = (msg.text or msg.get_message() or "").strip()
    if not re.match(r"^/gemini\b", text, re.I):
        return
    _plugin.handle_gemini_command(text, msg.chat_id)


def on_last_chat(cardinal: Cardinal, event: LastChatMessageChangedEvent) -> None:
    if not _plugin or not cardinal.old_mode_enabled:
        return
    chat = event.chat
    if not chat.unread:
        return
    text = str(chat).strip()
    if not re.match(r"^/gemini\b", text, re.I):
        return
    _plugin.handle_gemini_command(text, chat.id)


def safe_handler(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.error("%s: ошибка в %s: %s", _P, func.__name__, exc)
            logger.debug(traceback.format_exc())
    wrapper.__name__ = func.__name__
    return wrapper


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_NEW_ORDER = [safe_handler(on_new_order)]
BIND_TO_NEW_MESSAGE = [safe_handler(on_new_message)]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [safe_handler(on_last_chat)]

logger.info("$MAGENTA%s v%s загружен.$RESET", _P, VERSION)
