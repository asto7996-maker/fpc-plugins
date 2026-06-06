from __future__ import annotations

# === ОБЯЗАТЕЛЬНЫЕ ПОЛЯ FunPay Cardinal (НЕ УДАЛЯТЬ) ===
NAME = "Gemini Review Reply"
VERSION = "1.0.0"
DESCRIPTION = "ИИ-ответы на отзывы покупателей через Google Gemini"
CREDITS = "Cursor AI"
UUID = "c4e8b2f1-9a3d-4e7b-8c6f-2d1a5e9b0c3f"
SETTINGS_PAGE = False
BIND_TO_DELETE = None
# === КОНЕЦ ОБЯЗАТЕЛЬНЫХ ПОЛЕЙ ===

import hashlib
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import requests
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from FunPayAPI.common.enums import OrderStatuses
from FunPayAPI.types import MessageTypes, Order
from FunPayAPI.updater.events import LastChatMessageChangedEvent, NewMessageEvent

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("FPC.GeminiReview")
LOGGER_PREFIX = "GeminiReview"

STORAGE_DIR = f"storage/plugins/{UUID}"
SETTINGS_FILE = f"{STORAGE_DIR}/settings.json"
HISTORY_FILE = f"{STORAGE_DIR}/history.json"

DEFAULT_SYSTEM_PROMPT = """Ты — дружелюбный и внимательный продавец на маркетплейсе FunPay.
Твоя задача — написать ответ продавца на отзыв покупателя.

СТРОГИЕ ПРАВИЛА:
1. НИКОГДА не упоминай имя, ник или любые обращения к покупателю по имени.
2. ОБЯЗАТЕЛЬНО упомяни купленный товар: {product_name}.
3. ОБЯЗАТЕЛЬНО укажи дату и время выполнения заказа: {order_datetime}.
4. Пиши тёпло, позитивно, красочно. Можно умеренно использовать эмодзи.
5. Вырази радость за отзыв, искреннюю благодарность, тёплые пожелания.
6. Обязательно напиши, что ждём покупателя снова.
7. Добавь короткий уместный анекдот или лёгкую шутку, связанную с отзывом или товаром.
8. Отталкивайся от текста отзыва покупателя — ответь на его слова и настроение.
9. Каждый ответ должен быть уникальным. Не повторяй формулировки из списка предыдущих ответов.
10. Пиши только на русском языке.
11. Не используй шаблонные фразы вроде «дорогой друг» с именем.
12. Максимум 900 символов и не более 8 строк.

ОСОБОЕ ПРАВИЛО ДЛЯ 1 ЗВЕЗДЫ:
Если оценка 1 звезда — обязательно включи мысль о том, что данный отзыв не соответствует действительности,
оставлен с целью ухудшения рейтинга и в скором времени будет удалён. Сохраняй вежливый, но твёрдый тон."""

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "gemini_api_key": "AIzaSyA5c7Jm7DZhQ3O0A7Ld_Mh4HLq1eJpvoA0",
    "gemini_model": "gemini-2.0-flash",
    "proxy": "http://wgmGhd:FFhgh5@77.83.186.237:8000",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "send_chat_message": True,
    "chat_message": (
        "Спасибо за отзыв! 🙏\n"
        "Нам очень приятно получать обратную связь.\n"
        "Будем рады видеть вас снова! ✨"
    ),
    "reply_on_changed": True,
    "skip_hidden_reviews": False,
    "temperature": 0.95,
    "max_history": 200,
}

_file_lock = threading.RLock()
_processing_lock = threading.Lock()
_processing_orders: set[str] = set()

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


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


def load_history() -> List[Dict[str, Any]]:
    history = _load_json(HISTORY_FILE, [])
    return history if isinstance(history, list) else []


def save_history(history: List[Dict[str, Any]]) -> None:
    settings = load_settings()
    max_items = int(settings.get("max_history", 200))
    _save_json(HISTORY_FILE, history[-max_items:])


def _proxy_dict(proxy_url: str) -> Dict[str, str]:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return {}
    return {"http": proxy_url, "https": proxy_url}


def _format_datetime(dt: datetime) -> str:
    month = MONTHS_RU[dt.month - 1]
    return f"{dt.day} {month} {dt.year} года, {dt.strftime('%H:%M')}"


def _get_order_datetime(c: "Cardinal", order: Order) -> str:
    try:
        _, sales, _, _ = c.account.get_sales(id=order.id, include_closed=True, include_paid=True)
        if sales and sales[0].date:
            return _format_datetime(sales[0].date)
    except Exception:
        logger.debug("%s: не удалось получить дату заказа #%s", LOGGER_PREFIX, order.id, exc_info=True)

    if order.status == OrderStatuses.CLOSED:
        return _format_datetime(datetime.now())

    return "дата уточняется"


def _get_product_name(order: Order) -> str:
    parts: List[str] = []
    if order.short_description:
        parts.append(order.short_description.strip())
    if order.lot_params_text:
        parts.append(order.lot_params_text.strip())
    if order.subcategory:
        parts.append(order.subcategory.fullname.strip())
    if not parts and order.full_description:
        parts.append(order.full_description.strip()[:120])
    if not parts:
        return "приобретённый товар"
    return ", ".join(dict.fromkeys(parts))


def _format_review_text(text: str) -> str:
    max_l = 999
    text = (text or "").strip()
    if len(text) <= max_l:
        return text

    text_ = text[: max_l + 1]
    indexes: List[int] = []
    for char in (".", "!", "\n"):
        index1 = text_.rfind(char)
        indexes.extend([index1, text_[:index1].rfind(char)])
    cut = max(indexes, key=lambda x: (x < len(text_) - 1, x))
    text_ = text_[:cut].strip()
    if text_.count("\n") > 9:
        while text_.count("\n\n") > 1 and text_.count("\n") > 9:
            text_ = text_[::-1].replace("\n\n", "\n", 1)[::-1]
        if text_.count("\n") > 9:
            text_ = text_[::-1].replace("\n", " ", text_.count("\n") - 9)[::-1]
    return text_


def _reply_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _recent_replies(limit: int = 15) -> List[str]:
    history = load_history()
    replies = []
    for item in reversed(history):
        reply = item.get("reply", "")
        if reply:
            replies.append(reply[:180])
        if len(replies) >= limit:
            break
    return replies


def _add_history_entry(order: Order, reply: str, stars: int, source: str = "gemini") -> None:
    history = load_history()
    history.append({
        "order_id": order.id,
        "stars": stars,
        "product": _get_product_name(order),
        "review_text": (order.review.text or "")[:300] if order.review else "",
        "reply": reply,
        "reply_hash": _reply_hash(reply),
        "source": source,
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_history(history)


def _is_duplicate_reply(reply: str) -> bool:
    reply_hash = _reply_hash(reply)
    for item in load_history():
        if item.get("reply_hash") == reply_hash:
            return True
    return False


class GeminiClient:
    @staticmethod
    def generate(
        system_prompt: str,
        user_prompt: str,
        api_key: str,
        model: str,
        proxy_url: str,
        temperature: float = 0.95,
    ) -> Optional[str]:
        if not api_key:
            logger.error("%s: не указан API-ключ Gemini", LOGGER_PREFIX)
            return None

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        payload = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 1024,
            },
        }
        try:
            response = requests.post(
                url,
                json=payload,
                proxies=_proxy_dict(proxy_url),
                timeout=60,
            )
            if response.status_code != 200:
                logger.error(
                    "%s: Gemini API ошибка %s — %s",
                    LOGGER_PREFIX,
                    response.status_code,
                    response.text[:500],
                )
                return None
            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            texts = [p.get("text", "") for p in parts if p.get("text")]
            result = "\n".join(texts).strip()
            return result or None
        except Exception as exc:
            logger.error("%s: ошибка запроса к Gemini — %s", LOGGER_PREFIX, exc)
            logger.debug(traceback.format_exc())
            return None


def _build_user_prompt(order: Order, order_datetime: str, product_name: str) -> str:
    review = order.review
    stars = review.stars if review else 0
    review_text = (review.text or "без текста").strip()
    recent = _recent_replies()

    prompt = (
        f"Оценка: {stars} из 5 звёзд\n"
        f"Товар: {product_name}\n"
        f"Дата выполнения заказа: {order_datetime}\n"
        f"Текст отзыва покупателя: {review_text}\n\n"
        "Напиши готовый ответ продавца на этот отзыв. Только текст ответа, без пояснений."
    )
    if recent:
        prompt += "\n\nНе повторяй и не копируй эти предыдущие ответы:\n"
        for idx, old in enumerate(recent, 1):
            prompt += f"{idx}. {old}\n"
    return prompt


def _generate_review_reply(c: "Cardinal", order: Order) -> Optional[str]:
    settings = load_settings()
    review = order.review
    if not review or not review.stars:
        return None

    product_name = _get_product_name(order)
    order_datetime = _get_order_datetime(c, order)
    system_prompt = settings.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    system_prompt = system_prompt.format(
        product_name=product_name,
        order_datetime=order_datetime,
    )

    user_prompt = _build_user_prompt(order, order_datetime, product_name)
    reply = GeminiClient.generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        api_key=settings.get("gemini_api_key", ""),
        model=settings.get("gemini_model", "gemini-2.0-flash"),
        proxy_url=settings.get("proxy", ""),
        temperature=float(settings.get("temperature", 0.95)),
    )

    if not reply:
        return _fallback_reply(order, product_name, order_datetime)

    reply = _format_review_text(reply)

    attempts = 0
    while _is_duplicate_reply(reply) and attempts < 3:
        attempts += 1
        extra_prompt = user_prompt + (
            f"\n\nПредыдущий вариант слишком похож на старые ответы. "
            f"Напиши совершенно другой ответ, попытка {attempts + 1}."
        )
        reply = GeminiClient.generate(
            system_prompt=system_prompt,
            user_prompt=extra_prompt,
            api_key=settings.get("gemini_api_key", ""),
            model=settings.get("gemini_model", "gemini-2.0-flash"),
            proxy_url=settings.get("proxy", ""),
            temperature=min(1.2, float(settings.get("temperature", 0.95)) + 0.1 * attempts),
        )
        if reply:
            reply = _format_review_text(reply)
        else:
            break

    if not reply or _is_duplicate_reply(reply):
        reply = _fallback_reply(order, product_name, order_datetime)

    if order.buyer_username:
        reply = re.sub(re.escape(order.buyer_username), "", reply, flags=re.IGNORECASE)
        reply = re.sub(r"\s{2,}", " ", reply).strip()

    return reply


def _fallback_reply(order: Order, product_name: str, order_datetime: str) -> str:
    review = order.review
    stars = review.stars if review else 5
    review_text = (review.text or "").strip().lower()
    unique_tag = hashlib.md5(f"{order.id}{time.time()}".encode()).hexdigest()[:6]

    if stars == 1:
        return _format_review_text(
            f"Благодарим за обратную связь по заказу с товаром «{product_name}» "
            f"(выполнен {order_datetime}). "
            f"Сожалеем, что впечатление оказалось негативным. "
            f"Вынуждены отметить: данный отзыв не соответствует действительности, "
            f"оставлен с целью ухудшения рейтинга и в скором времени будет удалён. "
            f"Мы всегда открыты к диалогу и готовы помочь! 🌟"
        )

    beer_note = ""
    if "пив" in review_text:
        beer_note = " Желаем отличного вечера и приятного отдыха! 🍺"

    jokes = [
        "Анекдот дня: покупатель спросил продавца — а скидка будет? Продавец ответил: скидка уже в вашей улыбке! 😄",
        "Знаете, хороший отзыв — как кофе утром: сразу поднимает настроение! ☕",
        "Говорят, довольный покупатель — лучшая реклама. Спасибо, что вы с нами! 🎉",
        "Лёгкая шутка: наш товар настолько хорош, что даже кнопка «купить» устала ждать! 😊",
    ]
    joke = jokes[int(unique_tag, 16) % len(jokes)]

    return _format_review_text(
        f"Огромное спасибо за тёплый отзыв! ⭐\n"
        f"Очень рады, что покупка «{product_name}» (заказ выполнен {order_datetime}) "
        f"принесла вам положительные эмоции.{beer_note}\n"
        f"{joke}\n"
        f"Будем искренне рады видеть вас снова! До новых встреч! 💫"
    )


def _send_chat_thanks(c: "Cardinal", order: Order, chat_id: Any) -> None:
    settings = load_settings()
    if not settings.get("send_chat_message", True):
        return
    text = settings.get("chat_message", DEFAULT_SETTINGS["chat_message"])
    if order.buyer_username:
        text = text.replace("$username", "").replace("$buyer", "").strip()
    try:
        c.send_message(chat_id, text, order.buyer_username)
        logger.info("%s: благодарность в чат отправлена (заказ #%s)", LOGGER_PREFIX, order.id)
    except Exception:
        logger.error("%s: не удалось отправить сообщение в чат #%s", LOGGER_PREFIX, order.id)
        logger.debug(traceback.format_exc())


def _should_process_review(order: Order, message_type: MessageTypes, settings: Dict[str, Any]) -> bool:
    review = order.review
    if not review or not review.stars:
        return False
    if settings.get("skip_hidden_reviews") and review.hidden:
        return False
    if message_type == MessageTypes.FEEDBACK_CHANGED and not settings.get("reply_on_changed", True):
        return False
    if review.answer and message_type == MessageTypes.NEW_FEEDBACK:
        return False
    return True


def _process_review_event(c: "Cardinal", obj: Any, message_type: MessageTypes, chat_id: Any) -> None:
    settings = load_settings()
    if not settings.get("enabled", True):
        return

    try:
        order = c.get_order_from_object(obj)
        if order is None:
            order_id_match = re.search(r"#([A-Z0-9]+)", str(obj))
            if not order_id_match:
                return
            order = c.account.get_order(order_id_match.group(1))
    except Exception:
        logger.error("%s: не удалось получить заказ для отзыва", LOGGER_PREFIX)
        logger.debug(traceback.format_exc())
        return

    if not _should_process_review(order, message_type, settings):
        return

    with _processing_lock:
        if order.id in _processing_orders:
            return
        _processing_orders.add(order.id)

    try:
        logger.info(
            "%s: новый отзыв на заказ #%s (%s зв.)",
            LOGGER_PREFIX, order.id, order.review.stars,
        )
        reply = _generate_review_reply(c, order)
        if not reply:
            logger.warning("%s: не удалось сгенерировать ответ для #%s", LOGGER_PREFIX, order.id)
            return

        c.account.send_review(order.id, reply)
        _add_history_entry(order, reply, order.review.stars)
        logger.info("%s: ответ на отзыв #%s отправлен", LOGGER_PREFIX, order.id)

        _send_chat_thanks(c, order, chat_id)
    except Exception:
        logger.error("%s: ошибка при ответе на отзыв #%s", LOGGER_PREFIX, order.id)
        logger.debug(traceback.format_exc())
    finally:
        with _processing_lock:
            _processing_orders.discard(order.id)


def review_message_handler(c: "Cardinal", e: NewMessageEvent) -> None:
    obj = e.message
    message_type = obj.type
    if message_type not in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED):
        return
    if obj.i_am_buyer:
        return

    def worker():
        _process_review_event(c, obj, message_type, obj.chat_id)

    threading.Thread(target=worker, daemon=True).start()


def review_last_chat_handler(c: "Cardinal", e: LastChatMessageChangedEvent) -> None:
    if not c.old_mode_enabled:
        return
    obj = e.chat
    message_type = obj.last_message_type
    if message_type not in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED):
        return
    if f" {c.account.username} " in str(obj):
        return

    def worker():
        _process_review_event(c, obj, message_type, obj.id)

    threading.Thread(target=worker, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Telegram-панель
# ─────────────────────────────────────────────────────────────────────────────

def _mask_secret(value: str, visible: int = 4) -> str:
    value = value or ""
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "*" * (len(value) - visible)


def _settings_summary(settings: Dict[str, Any]) -> str:
    history = load_history()
    last = history[-1] if history else None
    last_text = (
        f"\n📝 Последний ответ: <code>#{last.get('order_id')}</code> "
        f"({last.get('datetime', '—')})"
        if last else "\n📝 История ответов пуста"
    )
    return (
        f"🤖 <b>{NAME}</b> v{VERSION}\n\n"
        f"⚙️ Статус: <b>{'✅ Включён' if settings.get('enabled') else '❌ Выключен'}</b>\n"
        f"🧠 Модель: <code>{settings.get('gemini_model')}</code>\n"
        f"🔑 API-ключ: <code>{_mask_secret(settings.get('gemini_api_key', ''))}</code>\n"
        f"🌐 Прокси: <code>{_mask_secret(settings.get('proxy', ''), 8)}</code>\n"
        f"💬 Сообщение в чат: <b>{'да' if settings.get('send_chat_message') else 'нет'}</b>\n"
        f"📊 Ответов в истории: <b>{len(history)}</b>"
        f"{last_text}"
    )


def _main_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(
            "✅ Вкл" if settings.get("enabled") else "❌ Выкл",
            callback_data="grr_toggle_enabled",
        ),
        InlineKeyboardButton("📝 Промпт", callback_data="grr_prompt_menu"),
    )
    kb.add(
        InlineKeyboardButton("🔑 API-ключ", callback_data="grr_set_api_key"),
        InlineKeyboardButton("🌐 Прокси", callback_data="grr_set_proxy"),
    )
    kb.add(
        InlineKeyboardButton("🧠 Модель", callback_data="grr_set_model"),
        InlineKeyboardButton("💬 Чат-сообщение", callback_data="grr_chat_menu"),
    )
    kb.add(
        InlineKeyboardButton("📜 История", callback_data="grr_history"),
        InlineKeyboardButton("🧪 Тест Gemini", callback_data="grr_test"),
    )
    kb.add(
        InlineKeyboardButton(
            f"{'✅' if settings.get('reply_on_changed') else '❌'} Ответ при изменении",
            callback_data="grr_toggle_changed",
        ),
        InlineKeyboardButton(
            f"{'✅' if settings.get('skip_hidden_reviews') else '❌'} Пропуск скрытых",
            callback_data="grr_toggle_hidden",
        ),
    )
    return kb


def _prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("✏️ Изменить промпт", callback_data="grr_set_prompt"),
        InlineKeyboardButton("♻️ Сбросить промпт", callback_data="grr_reset_prompt"),
        InlineKeyboardButton("⬅️ Назад", callback_data="grr_back_main"),
    )


def _chat_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton("✏️ Изменить текст", callback_data="grr_set_chat_msg"),
        InlineKeyboardButton(
            "🔁 Переключить отправку",
            callback_data="grr_toggle_chat",
        ),
        InlineKeyboardButton("⬅️ Назад", callback_data="grr_back_main"),
    )


def _format_history_text(limit: int = 10) -> str:
    history = load_history()
    if not history:
        return "📜 <b>История ответов пуста</b>"
    lines = [f"📜 <b>Последние {min(limit, len(history))} ответов</b>\n"]
    for item in reversed(history[-limit:]):
        lines.append(
            f"🆔 <code>#{item.get('order_id')}</code> | "
            f"{'⭐' * int(item.get('stars', 0))}\n"
            f"🛒 {item.get('product', '—')[:60]}\n"
            f"🕐 {item.get('datetime', '—')}\n"
            f"💬 {item.get('reply', '')[:120]}...\n"
        )
    return "\n".join(lines)


def init_commands(cardinal: "Cardinal") -> None:
    if not cardinal.telegram:
        return

    tg = cardinal.telegram
    bot = tg.bot

    def send_panel(message):
        settings = load_settings()
        bot.send_message(
            message.chat.id,
            _settings_summary(settings),
            reply_markup=_main_keyboard(settings),
            parse_mode="HTML",
        )

    def handle_callback(call):
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        settings = load_settings()

        try:
            if call.data == "grr_back_main":
                bot.edit_message_text(
                    _settings_summary(settings), chat_id, msg_id,
                    reply_markup=_main_keyboard(settings), parse_mode="HTML",
                )
            elif call.data == "grr_toggle_enabled":
                settings["enabled"] = not settings.get("enabled", True)
                save_settings(settings)
                bot.edit_message_text(
                    _settings_summary(settings), chat_id, msg_id,
                    reply_markup=_main_keyboard(settings), parse_mode="HTML",
                )
            elif call.data == "grr_toggle_changed":
                settings["reply_on_changed"] = not settings.get("reply_on_changed", True)
                save_settings(settings)
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=_main_keyboard(settings))
            elif call.data == "grr_toggle_hidden":
                settings["skip_hidden_reviews"] = not settings.get("skip_hidden_reviews", False)
                save_settings(settings)
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=_main_keyboard(settings))
            elif call.data == "grr_prompt_menu":
                prompt_preview = settings.get("system_prompt", "")[:600]
                bot.edit_message_text(
                    f"📝 <b>Системный промпт</b>\n\n<code>{prompt_preview}</code>",
                    chat_id, msg_id,
                    reply_markup=_prompt_keyboard(), parse_mode="HTML",
                )
            elif call.data == "grr_reset_prompt":
                settings["system_prompt"] = DEFAULT_SYSTEM_PROMPT
                save_settings(settings)
                bot.answer_callback_query(call.id, "Промпт сброшен")
                bot.edit_message_text(
                    _settings_summary(settings), chat_id, msg_id,
                    reply_markup=_main_keyboard(settings), parse_mode="HTML",
                )
            elif call.data == "grr_chat_menu":
                chat_text = settings.get("chat_message", "")
                bot.edit_message_text(
                    f"💬 <b>Сообщение в чат</b>\n"
                    f"Отправка: <b>{'вкл' if settings.get('send_chat_message') else 'выкл'}</b>\n\n"
                    f"<code>{chat_text}</code>",
                    chat_id, msg_id,
                    reply_markup=_chat_keyboard(), parse_mode="HTML",
                )
            elif call.data == "grr_toggle_chat":
                settings["send_chat_message"] = not settings.get("send_chat_message", True)
                save_settings(settings)
                bot.edit_message_text(
                    f"💬 <b>Сообщение в чат</b>\n"
                    f"Отправка: <b>{'вкл' if settings.get('send_chat_message') else 'выкл'}</b>\n\n"
                    f"<code>{settings.get('chat_message', '')}</code>",
                    chat_id, msg_id,
                    reply_markup=_chat_keyboard(), parse_mode="HTML",
                )
            elif call.data == "grr_set_api_key":
                result = bot.send_message(chat_id, "Введите API-ключ Google Gemini:")
                tg.set_state(chat_id, result.id, call.from_user.id, state="grr_api_key")
            elif call.data == "grr_set_proxy":
                result = bot.send_message(
                    chat_id,
                    "Введите прокси в формате:\n"
                    "<code>http://login:password@host:port</code>",
                    parse_mode="HTML",
                )
                tg.set_state(chat_id, result.id, call.from_user.id, state="grr_proxy")
            elif call.data == "grr_set_model":
                result = bot.send_message(
                    chat_id,
                    "Введите название модели Gemini\n(например: gemini-2.0-flash):",
                )
                tg.set_state(chat_id, result.id, call.from_user.id, state="grr_model")
            elif call.data == "grr_set_prompt":
                result = bot.send_message(
                    chat_id,
                    "Отправьте новый системный промпт.\n"
                    "Можно использовать переменные: {product_name}, {order_datetime}",
                )
                tg.set_state(chat_id, result.id, call.from_user.id, state="grr_prompt")
            elif call.data == "grr_set_chat_msg":
                result = bot.send_message(chat_id, "Введите текст благодарности для чата:")
                tg.set_state(chat_id, result.id, call.from_user.id, state="grr_chat_msg")
            elif call.data == "grr_history":
                bot.edit_message_text(
                    _format_history_text(10), chat_id, msg_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("⬅️ Назад", callback_data="grr_back_main"),
                    ),
                    parse_mode="HTML",
                )
            elif call.data == "grr_test":
                bot.answer_callback_query(call.id, "Тестирую Gemini...")
                test_reply = GeminiClient.generate(
                    system_prompt="Ты помощник. Отвечай кратко на русском.",
                    user_prompt="Напиши одно предложение: плагин Gemini Review Reply работает.",
                    api_key=settings.get("gemini_api_key", ""),
                    model=settings.get("gemini_model", "gemini-2.0-flash"),
                    proxy_url=settings.get("proxy", ""),
                )
                if test_reply:
                    bot.send_message(
                        chat_id,
                        f"✅ <b>Gemini отвечает:</b>\n\n{test_reply}",
                        parse_mode="HTML",
                    )
                else:
                    bot.send_message(chat_id, "❌ Ошибка подключения к Gemini. Проверьте ключ и прокси.")
            bot.answer_callback_query(call.id)
        except Exception as exc:
            logger.error("%s: ошибка callback %s — %s", LOGGER_PREFIX, call.data, exc)
            try:
                bot.answer_callback_query(call.id, "Ошибка")
            except Exception:
                pass

    def handle_text_input(message):
        state_data = tg.get_state(message.chat.id, message.from_user.id)
        if not state_data or "state" not in state_data:
            return
        state = state_data["state"]
        settings = load_settings()
        text = (message.text or "").strip()

        if state == "grr_api_key":
            settings["gemini_api_key"] = text
            save_settings(settings)
            bot.reply_to(message, "✅ API-ключ Gemini сохранён.")
        elif state == "grr_proxy":
            settings["proxy"] = text
            save_settings(settings)
            bot.reply_to(message, "✅ Прокси сохранён.")
        elif state == "grr_model":
            settings["gemini_model"] = text
            save_settings(settings)
            bot.reply_to(message, f"✅ Модель: <code>{text}</code>", parse_mode="HTML")
        elif state == "grr_prompt":
            settings["system_prompt"] = text
            save_settings(settings)
            bot.reply_to(message, "✅ Системный промпт обновлён.")
        elif state == "grr_chat_msg":
            settings["chat_message"] = text
            save_settings(settings)
            bot.reply_to(message, "✅ Текст сообщения в чат сохранён.")

        tg.clear_state(message.chat.id, message.from_user.id)

    tg.cbq_handler(handle_callback, lambda c: c.data.startswith("grr_"))
    tg.msg_handler(
        handle_text_input,
        func=lambda m: any(
            tg.check_state(m.chat.id, m.from_user.id, s)
            for s in ("grr_api_key", "grr_proxy", "grr_model", "grr_prompt", "grr_chat_msg")
        ),
    )
    tg.msg_handler(send_panel, commands=["review_ai", "grr"])

    cardinal.add_telegram_commands(UUID, [
        ("review_ai", f"панель {NAME}", True),
        ("grr", f"настройки {NAME}", True),
    ])


def safe_handler(func: Callable) -> Callable:
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.error("%s: ошибка в %s — %s", LOGGER_PREFIX, func.__name__, exc)
            logger.debug(traceback.format_exc())
    wrapper.__name__ = func.__name__
    return wrapper


_safe_review_msg = safe_handler(review_message_handler)
_safe_review_last_chat = safe_handler(review_last_chat_handler)
_safe_init_commands = safe_handler(init_commands)

BIND_TO_PRE_INIT = [_safe_init_commands]
BIND_TO_NEW_MESSAGE = [_safe_review_msg]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [_safe_review_last_chat]

logger.info("$MAGENTA%s v%s загружен.$RESET", LOGGER_PREFIX, VERSION)
