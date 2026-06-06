from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
#  Gemini Review Reply v2.0 — FunPay Cardinal plugin
#  Автоматически отвечает на отзывы покупателей через Google Gemini AI
# ──────────────────────────────────────────────────────────────────────────────

import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from typing import Final

from FunPayAPI.common.utils import RegularExpressions
from FunPayAPI.types import MessageTypes
from FunPayAPI.updater.events import NewMessageEvent
from cardinal import Cardinal
from tg_bot import CBT
from telebot.types import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB
import telebot


def _pip(pkg: str) -> None:
    from pip._internal.cli.main import main as _m
    _m(["install", "-U", "-q", pkg])


try:
    from requests import get as http_get, post as http_post
except ImportError:
    _pip("requests")
    from requests import get as http_get, post as http_post


# ── Метаданные ────────────────────────────────────────────────────────────────
NAME          = "Gemini Review Reply"
VERSION       = "2.0.0"
DESCRIPTION   = "ИИ красочно и позитивно отвечает на отзывы через Gemini 🌈"
CREDITS       = "Cursor AI"
UUID          = "c4e8b2f1-9a3d-4e7b-8c6f-2d1a5e9b0c3f"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

# ── Константы ─────────────────────────────────────────────────────────────────
MIN_STARS:        Final[int]  = 1
MAX_REVIEW_LEN:   Final[int]  = 999
MAX_CHAT_LEN:     Final[int]  = 240
SEND_IN_CHAT:     Final[bool] = True
ONLY_NEW:         Final[bool] = True
CHAT_HISTORY_MAX: Final[int]  = 20
SETTINGS_FILE     = "storage/plugins/gemini_reviews.json"
CHINESE_RE        = re.compile(r"[\u4e00-\u9fff]")

GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# ── Настройки ─────────────────────────────────────────────────────────────────
DEFAULT_SETTINGS: dict = {
    "gemini_api_key": "AIzaSyA5c7Jm7DZhQ3O0A7Ld_Mh4HLq1eJpvoA0",
    "http_proxy":     "http://wgmGhd:FFhgh5@77.83.186.237:8000",

    "review_system": (
        "Ты — харизматичный, живой и невероятно тёплый менеджер крутого магазина на FunPay. "
        "Ты пишешь сплошным живым текстом — без разделителей, символов и заголовков. "
        "Эмодзи вплетаешь прямо в текст между словами и в конце предложений. "
        "Стиль — как сообщение от лучшего друга, который реально рад.\n"
        "ЖЕЛЕЗНЫЕ правила — нарушать нельзя:\n"
        "— Пиши ТОЛЬКО на русском языке\n"
        "— НИКОГДА не упоминай имя покупателя\n"
        "— Никаких обращений типа Дорогой/Уважаемый/Привет + имя\n"
        "— Никаких ссылок, сайтов, политики, мата\n"
        "— Никаких заголовков и символов-разделителей\n"
        "— Сразу живой текст с места в карьер"
    ),

    "review_prompt": (
        "Покупатель оставил отзыв. Напиши ответ от лица продавца.\n\n"
        "ДАННЫЕ ЗАКАЗА:\n"
        "Товар: {item}\n"
        "Сумма: {cost} {currency}\n"
        "Дата выполнения заказа: {order_datetime}\n"
        "Оценка: {rating} из 5\n"
        "Текст отзыва: {text}\n\n"
        "ИСТОРИЯ ЧАТА (для анализа стиля общения):\n"
        "{chat_history}\n\n"
        "ИНСТРУКЦИЯ:\n\n"
        "Шаг 1 — изучи историю чата и подстрой стиль ответа под покупателя.\n\n"
        "Шаг 2а — если оценка 4–5 звёзд:\n"
        "1) Радостная реакция на {rating}⭐\n"
        "2) Отреагируй на текст отзыва — покажи, что прочитал\n"
        "3) Обязательно упомяни товар {item} и дату выполнения {order_datetime}\n"
        "4) Тёплое пожелание по теме товара\n"
        "5) Приглашение вернуться + лёгкая шутка или анекдот в финале\n\n"
        "Шаг 2б — если оценка 2–3 звезды:\n"
        "1) Искреннее сожаление без оправданий\n"
        "2) Попроси дождаться продавца — он уже в курсе\n"
        "3) Заверь, что всё решится 🤝\n\n"
        "Шаг 2в — если оценка 1 звезда:\n"
        "1) Вежливо, но твёрдо укажи: данный отзыв не соответствует действительности, "
        "оставлен с целью ухудшения рейтинга и в скором времени будет удалён\n"
        "2) Упомяни товар {item} и дату выполнения {order_datetime}\n"
        "3) Сохрани спокойный профессиональный тон\n\n"
        "СТРОГИЕ ТРЕБОВАНИЯ:\n"
        "— ЗАПРЕЩЕНО упоминать имя покупателя\n"
        "— Сплошной живой текст\n"
        "— 6–10 эмодзи органично в тексте\n"
        "— Объём: 300–700 символов\n"
        "— Каждый ответ уникален, не повторяй предыдущие\n\n"
        "Отвечай ТОЛЬКО готовым текстом ответа."
    ),

    "chat_system": (
        "Ты — дружелюбный менеджер магазина на FunPay. "
        "Пишешь короткое личное сообщение покупателю после его отзыва. "
        "Стиль: живой, тёплый, немного неформальный. Только на русском."
    ),
    "chat_prompt": (
        "Покупатель оставил отзыв {rating}⭐ на товар {item}.\n\n"
        "История общения с покупателем в чате:\n"
        "{chat_history}\n\n"
        "Напиши короткое живое сообщение в личный чат:\n"
        "— Подстройся под стиль из истории чата\n"
        "— Поблагодари за покупку и отзыв\n"
        "— Пожелай удачи с товаром\n"
        "— 2–3 эмодзи\n"
        "— Не более 180 символов\n"
        "— Без имени покупателя\n\n"
        "Отвечай ТОЛЬКО текстом сообщения."
    ),

    "recent_replies": [],
}

SETTINGS: dict = dict(DEFAULT_SETTINGS)

logger = logging.getLogger("FPC.GeminiReviews")
_P = "[GeminiReviews]"
logger.info(f"{_P} Плагин загружен v{VERSION}")


# ═════════════════════════════════════════════════════════════════════════════
#  Утилиты
# ═════════════════════════════════════════════════════════════════════════════

def _log(msg: str)  -> None: logger.info(f"{_P} {msg}")
def _err(msg: str)  -> None: logger.error(f"{_P} {msg}")


def _tg(cardinal: Cardinal, text: str) -> None:
    try:
        for uid in cardinal.telegram.authorized_users:
            cardinal.telegram.bot.send_message(uid, text, parse_mode="HTML")
    except Exception as e:
        _err(f"tg_notify: {e}")


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=4, ensure_ascii=False)
    except Exception as e:
        _err(f"save_settings: {e}")


def _load() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                loaded.setdefault(k, v)
            if not isinstance(loaded.get("recent_replies"), list):
                loaded["recent_replies"] = []
            return loaded
    except FileNotFoundError:
        _save()
        return dict(DEFAULT_SETTINGS)
    except Exception as e:
        _err(f"load_settings: {e}")
        return dict(DEFAULT_SETTINGS)


def _format_datetime(dt: datetime) -> str:
    return f"{dt.day} {MONTHS_RU[dt.month - 1]} {dt.year} года, {dt.strftime('%H:%M')}"


def _get_order_datetime(cardinal: Cardinal, order) -> str:
    try:
        _, sales, _, _ = cardinal.account.get_sales(id=order.id, include_closed=True, include_paid=True)
        if sales and sales[0].date:
            return _format_datetime(sales[0].date)
    except Exception:
        pass
    return datetime.now().strftime("%d.%m.%Y %H:%M")


def _fill(template: str, order, chat_history: str = "", order_datetime: str = "") -> str:
    review = getattr(order, "review", None)
    item = str(getattr(order, "title", None) or getattr(order, "short_description", None) or "товар")
    subs = {
        "{name}":          str(getattr(order, "buyer_username", "Покупатель")),
        "{item}":          item,
        "{cost}":          str(getattr(order, "sum", "")),
        "{currency}":      str(getattr(order, "currency", "₽")),
        "{seller}":        str(getattr(order, "seller_username", "")),
        "{rating}":        str(getattr(review, "stars", "5")),
        "{text}":          str(getattr(review, "text", "") or "без текста"),
        "{chat_history}":  chat_history or "История чата недоступна.",
        "{order_datetime}": order_datetime or "не указана",
    }
    for k, v in subs.items():
        template = template.replace(k, v)
    return template


def _get_chat_history(cardinal: Cardinal, chat_id, buyer: str) -> str:
    try:
        chat = cardinal.account.get_chat(chat_id)
        messages = getattr(chat, "messages", []) or []
        messages = messages[-CHAT_HISTORY_MAX:]
        if not messages:
            return "История чата пуста."

        lines = []
        for msg in messages:
            author = getattr(msg, "author", "")
            text = str(getattr(msg, "text", "") or "").strip()
            if not text:
                continue
            if str(author).lower() == str(buyer).lower():
                role = "👤 Покупатель"
            else:
                role = "🏪 Продавец"
            lines.append(f"{role}: {text}")

        return "\n".join(lines) if lines else "История чата пуста."
    except Exception as e:
        _err(f"_get_chat_history: {e}")
        return "История чата недоступна."


def _strip_name(text: str, buyer: str) -> str:
    if not buyer or not text:
        return text
    patterns = [
        rf"(?i)(дорогой|уважаемый|привет|здравствуй|хей|эй)[,!\s]+{re.escape(buyer)}[,!\s]*",
        rf"(?i){re.escape(buyer)}[,!\s]+",
        rf"(?i)\b{re.escape(buyer)}\b",
    ]
    for p in patterns:
        text = re.sub(p, "", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"^\s+", "", text, flags=re.MULTILINE)
    return text.strip()


def _trim(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    last = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"), cut.rfind("\n"))
    if last > max_len // 2:
        return cut[:last + 1].strip()
    return cut.rsplit(" ", 1)[0].strip() + "…"


def _is_bad(text: str) -> bool:
    if not text or len(text.strip()) < 15:
        return True
    if CHINESE_RE.search(text):
        return True
    for bad in [
        "Unable to decode JSON", "Request ended with status code",
        "quota", "RESOURCE_EXHAUSTED", "Model not found",
    ]:
        if bad.lower() in text.lower():
            return True
    return False


def _reply_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _is_duplicate(text: str) -> bool:
    h = _reply_hash(text)
    return any(r.get("hash") == h for r in SETTINGS.get("recent_replies", []))


def _remember_reply(text: str, order_id: str) -> None:
    recent = SETTINGS.setdefault("recent_replies", [])
    recent.append({
        "hash": _reply_hash(text),
        "order_id": order_id,
        "text": text[:200],
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    SETTINGS["recent_replies"] = recent[-50:]
    _save()


def _check_proxy(proxy: str) -> bool:
    try:
        r = http_get("https://api.ipify.org",
                     proxies={"http": proxy, "https": proxy}, timeout=8)
        return r.status_code == 200
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  Генерация через Gemini — прямой HTTP
# ═════════════════════════════════════════════════════════════════════════════

def _gemini(system: str, prompt: str) -> str | None:
    api_key = SETTINGS.get("gemini_api_key", "").strip()
    proxy   = SETTINGS.get("http_proxy", "").strip()

    if not api_key:
        _err("Gemini API key не задан!")
        return None

    proxies = {"http": proxy, "https": proxy} if proxy else None
    models = GEMINI_MODELS[:]
    random.shuffle(models)

    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.95,
                "maxOutputTokens": 800,
            },
        }
        try:
            _log(f"Gemini запрос → {model}")
            resp = http_post(url, json=payload, proxies=proxies, timeout=40)

            if resp.status_code == 429:
                _log("Gemini rate limit, жду 3 сек…")
                time.sleep(3)
                continue

            if resp.status_code != 200:
                err = resp.text[:200]
                _err(f"Gemini HTTP {resp.status_code}: {err}")
                if "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err:
                    break
                continue

            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                continue
            parts = candidates[0].get("content", {}).get("parts", [])
            content = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()

            if _is_bad(content):
                _err(f"Gemini мусорный ответ от {model}: {content[:60]}…")
                continue

            _log(f"Gemini успешно ✅ модель={model} длина={len(content)}")
            return content

        except Exception as e:
            _err(f"Gemini {model} исключение: {e}")

    _err("Все модели Gemini исчерпаны.")
    return None


def _generate(cardinal: Cardinal, system_key: str, prompt_key: str, order, chat_history: str) -> str | None:
    order_datetime = _get_order_datetime(cardinal, order)
    system = _fill(SETTINGS.get(system_key, ""), order, chat_history, order_datetime)
    prompt = _fill(SETTINGS.get(prompt_key, ""), order, chat_history, order_datetime)

    recent = SETTINGS.get("recent_replies", [])[-10:]
    if recent:
        prompt += "\n\nНе повторяй эти предыдущие ответы:\n"
        for i, r in enumerate(recent, 1):
            prompt += f"{i}. {r.get('text', '')[:120]}\n"

    return _gemini(system, prompt)


def _fallback_review(order, order_datetime: str) -> str:
    review = getattr(order, "review", None)
    stars = int(getattr(review, "stars", 5) or 5)
    item = str(getattr(order, "title", None) or getattr(order, "short_description", None) or "товар")
    text = str(getattr(review, "text", "") or "").lower()

    if stars == 1:
        return _trim(
            f"Благодарим за обратную связь по товару «{item}» (выполнен {order_datetime}). "
            f"Вынуждены отметить: данный отзыв не соответствует действительности, "
            f"оставлен с целью ухудшения рейтинга и в скором времени будет удалён. "
            f"Мы всегда открыты к диалогу! 🌟",
            MAX_REVIEW_LEN,
        )

    beer = " Желаем отличного отдыха! 🍺" if "пив" in text else ""
    jokes = [
        "Анекдот: покупатель спросил про скидку — а она уже в вашей улыбке! 😄",
        "Хороший отзыв — как кофе утром: сразу поднимает настроение! ☕",
        "Довольный покупатель — лучшая реклама. Спасибо, что вы с нами! 🎉",
    ]
    joke = random.choice(jokes)
    return _trim(
        f"Огромное спасибо за тёплый отзыв! ⭐ "
        f"Очень рады, что «{item}» (заказ выполнен {order_datetime}) вам понравился.{beer} "
        f"{joke} Будем рады видеть вас снова! 💫",
        MAX_REVIEW_LEN,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Telegram — страница настроек Cardinal
# ═════════════════════════════════════════════════════════════════════════════

def init(cardinal: Cardinal) -> None:
    global SETTINGS
    SETTINGS = _load()

    tg  = cardinal.telegram
    bot = tg.bot

    if not SETTINGS.get("gemini_api_key"):
        _tg(cardinal,
            f"⚠️ <b>{NAME}</b>: Gemini API ключ не задан!\n"
            f'<a href="https://aistudio.google.com/apikey">Получить ключ</a>')

    CB_KEY    = f"gmr_{UUID}_key"
    CB_PROXY  = f"gmr_{UUID}_proxy"
    CB_TEST   = f"gmr_{UUID}_test"
    CB_BACK   = f"gmr_{UUID}_back"

    def page_main(call: telebot.types.CallbackQuery) -> None:
        key   = SETTINGS.get("gemini_api_key", "")
        proxy = SETTINGS.get("http_proxy", "")
        k_show = (key[:18] + "…") if len(key) > 18 else (key or "🛑 не задан")
        p_show = proxy or "🛑 не задан"
        recent = len(SETTINGS.get("recent_replies", []))
        kb = IKM()
        kb.add(IKB("🔑 Изменить Gemini API ключ", callback_data=CB_KEY))
        kb.add(IKB("🌐 Изменить прокси",           callback_data=CB_PROXY))
        kb.add(IKB("🧪 Тест Gemini",                callback_data=CB_TEST))
        kb.row(IKB("◀️ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
        text = (
            f"⚙️ <b>{NAME} v{VERSION}</b>\n\n"
            f"🔑 Gemini API: <code>{k_show}</code>\n"
            f"🌐 Прокси:     <code>{p_show}</code>\n"
            f"📊 Ответов в истории: <b>{recent}</b>\n\n"
            f"Плагин отвечает на отзывы ≥ {MIN_STARS}⭐ красочно и позитивно 🌈\n"
            f"Модели: {', '.join(GEMINI_MODELS)}"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  reply_markup=kb, parse_mode="HTML",
                                  disable_web_page_preview=True)
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=kb,
                             parse_mode="HTML", disable_web_page_preview=True)
        bot.answer_callback_query(call.id)

    def ask_key(call: telebot.types.CallbackQuery) -> None:
        bot.answer_callback_query(call.id)
        kb = IKM(); kb.add(IKB("◀️ Отмена", callback_data=CB_BACK))
        cur = SETTINGS.get("gemini_api_key", "нет")
        msg = bot.send_message(
            call.message.chat.id,
            f"🔑 Текущий ключ: <code>{cur[:18]}…</code>\n\n"
            f'Введите новый Gemini API ключ (<a href="https://aistudio.google.com/apikey">получить</a>):',
            reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True,
        )
        bot.register_next_step_handler(msg, _save_key)

    def _save_key(message: telebot.types.Message) -> None:
        if not message.text or message.text.startswith("/"):
            return
        SETTINGS["gemini_api_key"] = message.text.strip()
        _save()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        kb = IKM(); kb.add(IKB("◀️ В настройки", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
        bot.reply_to(message,
                     f"✅ Gemini API ключ обновлён: <code>{SETTINGS['gemini_api_key'][:18]}…</code>",
                     reply_markup=kb, parse_mode="HTML")

    def ask_proxy(call: telebot.types.CallbackQuery) -> None:
        bot.answer_callback_query(call.id)
        kb = IKM(); kb.add(IKB("◀️ Отмена", callback_data=CB_BACK))
        cur = SETTINGS.get("http_proxy", "нет")
        msg = bot.send_message(
            call.message.chat.id,
            f"🌐 Текущий прокси: <code>{cur}</code>\n\n"
            "Введите новый прокси:\n<code>http://user:pass@ip:port</code>",
            reply_markup=kb, parse_mode="HTML",
        )
        bot.register_next_step_handler(msg, _save_proxy)

    def _save_proxy(message: telebot.types.Message) -> None:
        if not message.text or message.text.startswith("/"):
            return
        proxy = message.text.strip()
        if proxy and not proxy.startswith("http"):
            proxy = f"http://{proxy}"
        bot.send_message(message.chat.id, "⏳ Проверяю прокси…")
        if not _check_proxy(proxy):
            kb = IKM(); kb.add(IKB("Ввести другой", callback_data=CB_PROXY))
            bot.reply_to(message, "❌ Прокси не отвечает. Проверьте данные.",
                         reply_markup=kb)
            return
        SETTINGS["http_proxy"] = proxy
        _save()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        kb = IKM(); kb.add(IKB("◀️ В настройки", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
        bot.reply_to(message, f"✅ Прокси установлен:\n<code>{proxy}</code>",
                     reply_markup=kb, parse_mode="HTML")

    def test_api(call: telebot.types.CallbackQuery) -> None:
        bot.answer_callback_query(call.id, "Тестирую Gemini…")
        result = _gemini(
            "Ты помощник. Отвечай кратко на русском.",
            "Напиши одно предложение: плагин Gemini Review Reply работает.",
        )
        if result:
            bot.send_message(call.message.chat.id,
                             f"✅ <b>Gemini отвечает:</b>\n\n{result}",
                             parse_mode="HTML")
        else:
            bot.send_message(call.message.chat.id,
                             "❌ Ошибка Gemini. Проверьте ключ, прокси и квоту API.")

    def back_handler(call: telebot.types.CallbackQuery) -> None:
        tg.clear_state(call.message.chat.id, call.from_user.id, True)
        page_main(call)

    tg.cbq_handler(page_main,    lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in c.data)
    tg.cbq_handler(ask_key,      lambda c: c.data == CB_KEY)
    tg.cbq_handler(ask_proxy,    lambda c: c.data == CB_PROXY)
    tg.cbq_handler(test_api,     lambda c: c.data == CB_TEST)
    tg.cbq_handler(back_handler, lambda c: c.data == CB_BACK)

    _log("init() завершён ✅")


# ═════════════════════════════════════════════════════════════════════════════
#  Обработчик отзывов
# ═════════════════════════════════════════════════════════════════════════════

def message_handler(cardinal: Cardinal, event: NewMessageEvent) -> None:
    try:
        msg_type = event.message.type

        if ONLY_NEW:
            if msg_type != MessageTypes.NEW_FEEDBACK:
                return
        else:
            if msg_type not in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED):
                return

        if event.message.i_am_buyer:
            return

        try:
            order = cardinal.get_order_from_object(event.message)
            if order is None:
                match = RegularExpressions().ORDER_ID.findall(str(event.message))
                if not match:
                    return
                order_id = match[0][1:] if match[0].startswith("#") else match[0]
                order = cardinal.account.get_order(order_id)
        except Exception as e:
            _err(f"get_order: {e}")
            return

        review = getattr(order, "review", None)
        if not review or not getattr(review, "stars", None):
            return

        stars = int(review.stars)
        if stars < MIN_STARS:
            return

        if review.answer and ONLY_NEW:
            _log(f"Заказ #{order.id}: ответ уже есть, пропуск")
            return

        buyer = str(getattr(order, "buyer_username", "") or event.message.chat_name or "")
        chat_id = event.message.chat_id
        chat_history = _get_chat_history(cardinal, chat_id, buyer)
        order_datetime = _get_order_datetime(cardinal, order)

        _log(f"Отзыв #{order.id} | {stars}⭐ | {getattr(order, 'title', 'товар')}")

        reply = _generate(cardinal, "review_system", "review_prompt", order, chat_history)
        if not reply:
            reply = _fallback_review(order, order_datetime)
        else:
            reply = _strip_name(reply, buyer)
            reply = _trim(reply, MAX_REVIEW_LEN)

        attempts = 0
        while _is_duplicate(reply) and attempts < 2:
            attempts += 1
            extra = _generate(cardinal, "review_system", "review_prompt", order, chat_history)
            if extra:
                reply = _trim(_strip_name(extra, buyer), MAX_REVIEW_LEN)
            else:
                break

        if _is_duplicate(reply):
            reply = _fallback_review(order, order_datetime)

        cardinal.account.send_review(order.id, reply)
        _remember_reply(reply, order.id)
        _log(f"Ответ на отзыв #{order.id} отправлен ✅ ({len(reply)} симв.)")

        if SEND_IN_CHAT:
            chat_msg = _generate(cardinal, "chat_system", "chat_prompt", order, chat_history)
            if not chat_msg:
                chat_msg = (
                    "Спасибо за отзыв! 🙏 Очень приятно получить обратную связь. "
                    "Будем рады видеть вас снова! ✨"
                )
            else:
                chat_msg = _strip_name(_trim(chat_msg, MAX_CHAT_LEN), buyer)
            try:
                cardinal.send_message(chat_id, chat_msg, buyer)
                _log(f"Сообщение в чат #{chat_id} отправлено ✅")
            except Exception as e:
                _err(f"send_message: {e}")

    except Exception as e:
        _err(f"message_handler: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  Привязка к Cardinal
# ═════════════════════════════════════════════════════════════════════════════

BIND_TO_PRE_INIT    = [init]
BIND_TO_NEW_MESSAGE = [message_handler]
