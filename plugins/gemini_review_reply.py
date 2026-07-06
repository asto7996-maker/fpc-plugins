from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
#  Gemini Review Reply v3.0 — FunPay Cardinal plugin
#  Архитектура как у Starvell AI-ассистента: schema-настройки, AQ-ключ, batch
# ──────────────────────────────────────────────────────────────────────────────

import hashlib
import html
import json
import logging
import os
import random
import re
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from FunPayAPI.common.utils import RegularExpressions
from FunPayAPI.types import MessageTypes, Order
from FunPayAPI.updater.events import LastChatMessageChangedEvent, NewMessageEvent
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


NAME          = "Gemini Review Reply"
VERSION       = "3.0.3"
DESCRIPTION   = "ИИ-ответы на отзывы FunPay (Gemini AQ + HTTP/SOCKS proxy + batch) 🌈"
CREDITS       = "Cursor AI"
UUID          = "c4e8b2f1-9a3d-4e7b-8c6f-2d1a5e9b0c3f"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

MAX_REVIEW_LEN:   Final[int] = 999
MAX_CHAT_LEN:     Final[int] = 240
CHAT_HISTORY_MAX: Final[int] = 20
SETTINGS_FILE     = f"storage/plugins/{UUID}/settings.json"
CHINESE_RE        = re.compile(r"[\u4e00-\u9fff]")
CB_PREFIX         = f"grv_{UUID[:8]}"

GEMINI_MODELS = (
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

DEFAULT_SYSTEM_PROMPT = (
    "Ты — харизматичный, живой и невероятно тёплый менеджер магазина на FunPay. "
    "Пишешь сплошным живым текстом без заголовков и разделителей. "
    "Эмодзи органично вплетены в текст.\n"
    "ЖЕЛЕЗНЫЕ правила:\n"
    "— Только русский язык\n"
    "— НИКОГДА не упоминай имя покупателя\n"
    "— Обязательно упомяни товар: {product_name}\n"
    "— Обязательно укажи дату выполнения: {order_datetime}\n"
    "— Для 1★: отзыв не соответствует действительности, оставлен для ухудшения рейтинга, будет удалён\n"
    "— Тёплые пожелания, радость за отзыв, ждём снова, лёгкая шутка по теме отзыва\n"
    "— Каждый ответ уникален, до 700 символов"
)

DEFAULT_REVIEW_PROMPT = (
    "Покупатель оставил отзыв. Напиши ответ от лица продавца.\n\n"
    "ДАННЫЕ:\n"
    "Товар: {item}\n"
    "Сумма: {cost} {currency}\n"
    "Дата выполнения: {order_datetime}\n"
    "Оценка: {rating} из 5\n"
    "Текст отзыва: {text}\n\n"
    "ИСТОРИЯ ЧАТА:\n{chat_history}\n\n"
    "Отвечай ТОЛЬКО готовым текстом. Без имени покупателя."
)

DEFAULT_CHAT_SYSTEM = (
    "Ты — дружелюбный менеджер магазина FunPay. "
    "Короткое сообщение покупателю после отзыва. Только русский, без имени."
)

DEFAULT_CHAT_PROMPT = (
    "Покупатель оставил отзыв {rating}⭐ на {item}.\n"
    "История чата:\n{chat_history}\n\n"
    "Напиши короткую благодарность (до 180 символов), 2–3 эмодзи. Без имени."
)

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

logger = logging.getLogger("FPC.GeminiReviews")
_P = "[GeminiReviews]"

_plugin: "Plugin | None" = None


# ═════════════════════════════════════════════════════════════════════════════
#  Gemini API — ключи AIza… и AQ.… через x-goog-api-key
# ═════════════════════════════════════════════════════════════════════════════

def _gemini_url(model: str) -> str:
    return f"{GEMINI_BASE}/{model}:generateContent"


def _request_headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "x-goog-api-key": api_key.strip()}


_SOCKS_SCHEMES = ("socks4", "socks4a", "socks5", "socks5h")
_SOCKS_READY = False


def _ensure_socks() -> None:
    global _SOCKS_READY
    if _SOCKS_READY:
        return
    try:
        import socks  # noqa: F401  # PySocks
        _SOCKS_READY = True
        return
    except ImportError:
        pass
    _pip("PySocks")
    import socks  # noqa: F401
    _SOCKS_READY = True


def _parse_telegram_socks(url: str) -> str:
    """t.me/socks?server=ip&port=8000&user=login&pass=password → socks5://…"""
    parsed = urlparse(url.strip())
    qs = parse_qs(parsed.query)
    server = (qs.get("server") or [""])[0].strip()
    if not server:
        return ""
    port = (qs.get("port") or ["1080"])[0].strip()
    user = (qs.get("user") or [""])[0].strip()
    passwd = (qs.get("pass") or qs.get("password") or [""])[0].strip()
    auth = f"{user}:{passwd}@" if user else ""
    return f"socks5://{auth}{server}:{port}"


def _normalize_proxy(proxy: str) -> str:
    """
    Поддерживаемые форматы:
    - socks5://user:pass@host:port
    - socks4://host:port
    - http://user:pass@host:port
    - https://user:pass@host:port
    - https://t.me/socks?server=…&port=…&user=…&pass=…
    - user:pass@host:port (по умолчанию socks5)
    - host:port (по умолчанию socks5)
    """
    proxy = (proxy or "").strip()
    if not proxy:
        return ""

    low = proxy.lower()
    if "t.me/socks" in low or "telegram.me/socks" in low:
        parsed = _parse_telegram_socks(proxy)
        return parsed or proxy

    if "://" in proxy:
        scheme = low.split("://", 1)[0]
        if scheme in _SOCKS_SCHEMES or scheme in ("http", "https"):
            return proxy
        return f"socks5://{proxy.split('://', 1)[1]}"

    if "@" in proxy:
        return f"socks5://{proxy}"

    if re.match(r"^[\w.\-]+:\d+$", proxy):
        return f"socks5://{proxy}"

    return f"http://{proxy}"


def _client_proxies(proxy: str) -> dict[str, str] | None:
    url = _normalize_proxy(proxy)
    if not url:
        return None
    if url.lower().startswith(_SOCKS_SCHEMES):
        _ensure_socks()
    return {"http": url, "https": url}


def _proxy_label(proxy: str) -> str:
    url = _normalize_proxy(proxy)
    if not url:
        return "не задан"
    scheme = url.split("://", 1)[0].upper()
    return scheme


def _check_proxy(proxy: str) -> tuple[bool, str]:
    proxies = _client_proxies(proxy)
    if not proxies:
        return False, "Прокси не задан"
    url = _normalize_proxy(proxy)
    try:
        r = http_get("https://api.ipify.org?format=json", proxies=proxies, timeout=15)
        if r.status_code == 200:
            ip = r.json().get("ip", "?")
            scheme = url.split("://", 1)[0].upper()
            return True, f"OK [{scheme}] — IP: {ip}"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)[:120]


def _gemini_generate(
    api_key: str, proxy: str, model: str,
    system: str, prompt: str, temperature: float = 0.95,
) -> str | None:
    if not api_key.strip():
        return None
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 900},
    }
    proxies = _client_proxies(proxy)
    for try_model in [model] + [m for m in GEMINI_MODELS if m != model]:
        try:
            resp = http_post(
                _gemini_url(try_model), json=payload,
                headers=_request_headers(api_key), proxies=proxies, timeout=60,
            )
            if resp.status_code in (404, 429):
                if resp.status_code == 429:
                    time.sleep(3)
                continue
            if resp.status_code != 200:
                logger.warning("%s Gemini %s: %s", _P, resp.status_code, resp.text[:180])
                continue
            candidates = resp.json().get("candidates") or []
            if not candidates:
                continue
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
            if text and not _is_bad(text):
                return text
        except Exception as exc:
            logger.warning("%s Gemini %s err: %s", _P, try_model, exc)
    return None


def _is_bad(text: str) -> bool:
    if not text or len(text.strip()) < 10:
        return True
    if CHINESE_RE.search(text):
        return True
    return any(x in text.lower() for x in ("quota exceeded", "resource_exhausted"))


def _trim(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    last = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"), cut.rfind("\n"))
    if last > max_len // 2:
        return cut[:last + 1].strip()
    return cut.rsplit(" ", 1)[0].strip() + "…"


def _strip_name(text: str, buyer: str) -> str:
    if not buyer or not text:
        return text
    for p in (
        rf"(?i)(дорогой|уважаемый|привет|здравствуй)[,!\s]+{re.escape(buyer)}[,!\s]*",
        rf"(?i)\b{re.escape(buyer)}\b",
    ):
        text = re.sub(p, "", text)
    return re.sub(r" {2,}", " ", text).strip()


def _reply_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text.strip().lower()).encode()).hexdigest()[:16]


def _format_datetime(dt: datetime) -> str:
    return f"{dt.day} {MONTHS_RU[dt.month - 1]} {dt.year} года, {dt.strftime('%H:%M')}"


def _escape(val: Any) -> str:
    return html.escape(str(val if val is not None else ""))


def _seller_review_reply(review: Any) -> str | None:
    """Текст ответа продавца на отзыв (FunPayAPI: .reply, не .answer)."""
    if not review:
        return None
    return getattr(review, "reply", None) or getattr(review, "answer", None)


# ═════════════════════════════════════════════════════════════════════════════
#  Plugin (архитектура StarvellPlugin)
# ═════════════════════════════════════════════════════════════════════════════

class Plugin:
    def __init__(self, cardinal: Cardinal) -> None:
        self.cardinal = cardinal
        self._cfg: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._processing: set[str] = set()
        self._batch_running = False
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
                self._cfg = loaded
            else:
                self._cfg = defaults
                self._save_settings()
        except Exception as exc:
            logger.error("%s settings load: %s", _P, exc)
            self._cfg = defaults

    def _save_settings(self) -> None:
        with self._lock:
            tmp = f"{SETTINGS_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cfg, f, indent=4, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)

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
        if key == "gemini_api_key" and value:
            self.log("API key обновлён")

    @staticmethod
    def _default_cfg() -> dict[str, Any]:
        return {
            "enabled": True,
            "gemini_api_key": "AIzaSyA5c7Jm7DZhQ3O0A7Ld_Mh4HLq1eJpvoA0",
            "gemini_proxy": "socks5://nLFuxn:8TzG10@45.147.180.59:8000",
            "gemini_model": "gemini-2.5-flash-lite",
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "review_prompt": DEFAULT_REVIEW_PROMPT,
            "chat_system": DEFAULT_CHAT_SYSTEM,
            "chat_prompt": DEFAULT_CHAT_PROMPT,
            "temperature": "0.95",
            "send_chat_message": True,
            "reply_on_changed": False,
            "batch_count": 5,
            "recent_replies": [],
        }

    @staticmethod
    def settings_page_size() -> int:
        return 8

    def get_settings_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "enabled", "label": "⚡ Мгновенный автоответ (сразу при новом отзыве)", "type": "bool", "default": True},
            {"key": "gemini_api_key", "label": "Gemini API Key (AIza / AQ)", "type": "text", "default": ""},
            {
                "key": "gemini_proxy",
                "label": "Proxy (HTTP / SOCKS5 / t.me/socks)",
                "type": "text",
                "default": "",
                "description": (
                    "socks5://user:pass@ip:port или ссылка "
                    "https://t.me/socks?server=…&port=…&user=…&pass=…"
                ),
            },
            {"key": "gemini_model", "label": "Модель Gemini", "type": "text", "default": "gemini-2.5-flash-lite"},
            {"key": "system_prompt", "label": "Системный промпт", "type": "multiline", "default": DEFAULT_SYSTEM_PROMPT},
            {"key": "review_prompt", "label": "Промпт ответа на отзыв", "type": "multiline", "default": DEFAULT_REVIEW_PROMPT},
            {"key": "temperature", "label": "Temperature", "type": "text", "default": "0.95"},
            {"key": "send_chat_message", "label": "Благодарность в чат", "type": "bool", "default": True},
            {"key": "batch_count", "label": "N для ручной обработки старых отзывов", "type": "int", "default": 5, "min": 1, "max": 50},
            {"key": "reply_recent", "label": "▶ Вручную: ответить на N старых отзывов", "type": "action"},
            {"key": "test_gemini", "label": "🧪 Тест Gemini API", "type": "action"},
            {"key": "check_proxy", "label": "🌐 Проверить прокси", "type": "action"},
        ]

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

    def render_settings_text(self, page: int = 0) -> str:
        schema = self.get_settings_schema()
        ps = self.settings_page_size()
        pages = max(1, (len(schema) + ps - 1) // ps)
        page = max(0, min(page, pages - 1))
        chunk = schema[page * ps:(page + 1) * ps]
        lines = [
            f"⚙️ <b>{_escape(NAME)}</b> v{VERSION}",
            "━━━━━━━━━━━━━━━━━━",
            f"<i>{_escape(DESCRIPTION)}</i>", "",
        ]
        if pages > 1:
            lines.append(f"📄 Страница <b>{page + 1}</b> / {pages}\n")
        for field in chunk:
            key = field["key"]
            val = "" if field.get("type") == "action" else self.get_cfg(key)
            lines.append(self._format_setting_line(field, val))
        recent = self.get_cfg("recent_replies", [])
        lines.append(f"\n📊 История ответов: <b>{len(recent)}</b>")
        if self.get_cfg("enabled"):
            lines.append("⚡ <b>Автоответ:</b> включён — отвечаем сразу при новом отзыве")
        else:
            lines.append("🔴 <b>Автоответ выключен</b> — только кнопка ручной обработки N отзывов")
        if self._batch_running:
            lines.append("⏳ <b>Пакетная обработка…</b>")
        return "\n".join(lines)

    def build_settings_keyboard(self, page: int = 0) -> IKM:
        schema = self.get_settings_schema()
        ps = self.settings_page_size()
        pages = max(1, (len(schema) + ps - 1) // ps)
        page = max(0, min(page, pages - 1))
        chunk = schema[page * ps:(page + 1) * ps]
        kb = IKM()
        for local_i, field in enumerate(chunk):
            key = field["key"]
            idx = page * ps + local_i
            label = field.get("label", key)
            ftype = field.get("type", "str")
            if ftype == "bool":
                on = bool(self.get_cfg(key))
                kb.add(IKB(f"{'🟢' if on else '🔴'} {label[:42]}", callback_data=f"{CB_PREFIX}:tog:{idx}"))
            elif ftype == "action":
                kb.add(IKB(label[:48], callback_data=f"{CB_PREFIX}:act:{key}"))
            else:
                val = str(self.get_cfg(key, "")).replace("\n", " ")[:16]
                if len(str(self.get_cfg(key, ""))) > 16:
                    val += "…"
                kb.add(IKB(f"✏️ {label[:22]}: {val or '—'}", callback_data=f"{CB_PREFIX}:edit:{idx}"))
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(IKB("◀️", callback_data=f"{CB_PREFIX}:page:{page - 1}"))
            nav.append(IKB(f"{page + 1}/{pages}", callback_data=f"{CB_PREFIX}:noop"))
            if page < pages - 1:
                nav.append(IKB("▶️", callback_data=f"{CB_PREFIX}:page:{page + 1}"))
            kb.row(*nav)
        kb.add(IKB("◀️ К плагину", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
        return kb

    # ── Order helpers ────────────────────────────────────────────────────────

    def _order_datetime(self, order: Order, shortcut_date: datetime | None = None) -> str:
        if shortcut_date:
            return _format_datetime(shortcut_date)
        try:
            _, sales, _, _ = self.cardinal.account.get_sales(id=order.id, include_closed=True)
            if sales and sales[0].date:
                return _format_datetime(sales[0].date)
        except Exception:
            pass
        return datetime.now().strftime("%d.%m.%Y %H:%M")

    def _product_name(self, order: Order) -> str:
        parts = []
        if order.short_description:
            parts.append(order.short_description.strip())
        if order.lot_params_text:
            parts.append(order.lot_params_text.strip())
        if order.subcategory:
            parts.append(order.subcategory.fullname.strip())
        return ", ".join(dict.fromkeys(parts)) or "товар"

    def _get_chat_history(self, chat_id: Any, buyer: str) -> str:
        try:
            chat = self.cardinal.account.get_chat(chat_id)
            messages = (getattr(chat, "messages", None) or [])[-CHAT_HISTORY_MAX:]
            lines = []
            for msg in messages:
                text = str(getattr(msg, "text", "") or "").strip()
                if not text:
                    continue
                author = getattr(msg, "author", "")
                role = "👤 Покупатель" if str(author).lower() == str(buyer).lower() else "🏪 Продавец"
                lines.append(f"{role}: {text}")
            return "\n".join(lines) if lines else "История чата пуста."
        except Exception as exc:
            logger.debug("%s chat history: %s", _P, exc)
            return "История чата недоступна."

    def _fill(self, template: str, order: Order, chat_history: str, order_datetime: str) -> str:
        review = order.review
        subs = {
            "{item}": self._product_name(order),
            "{product_name}": self._product_name(order),
            "{cost}": str(order.sum),
            "{currency}": str(order.currency),
            "{rating}": str(review.stars if review else 5),
            "{text}": str(review.text if review and review.text else "без текста"),
            "{chat_history}": chat_history,
            "{order_datetime}": order_datetime,
        }
        for k, v in subs.items():
            template = template.replace(k, v)
        return template

    def _is_duplicate(self, text: str) -> bool:
        h = _reply_hash(text)
        return any(r.get("hash") == h for r in self.get_cfg("recent_replies", []))

    def _remember_reply(self, text: str, order_id: str) -> None:
        recent = list(self.get_cfg("recent_replies", []))
        recent.append({
            "hash": _reply_hash(text), "order_id": order_id,
            "text": text[:200], "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.set_cfg("recent_replies", recent[-50:])

    def _generate_reply(self, order: Order, chat_history: str, order_datetime: str) -> str | None:
        try:
            temp = float(self.get_cfg("temperature", "0.95"))
        except (TypeError, ValueError):
            temp = 0.95
        system_tpl = str(self.get_cfg("system_prompt", DEFAULT_SYSTEM_PROMPT))
        system = self._fill(system_tpl, order, chat_history, order_datetime)
        review_tpl = str(self.get_cfg("review_prompt", DEFAULT_REVIEW_PROMPT))
        if not review_tpl.strip():
            review_tpl = DEFAULT_REVIEW_PROMPT
        prompt = self._fill(review_tpl, order, chat_history, order_datetime)
        recent = self.get_cfg("recent_replies", [])[-8:]
        if recent:
            prompt += "\n\nНе повторяй эти ответы:\n"
            for i, r in enumerate(recent, 1):
                prompt += f"{i}. {r.get('text', '')[:100]}\n"
        return _gemini_generate(
            str(self.get_cfg("gemini_api_key", "")),
            str(self.get_cfg("gemini_proxy", "")),
            str(self.get_cfg("gemini_model", "gemini-2.5-flash-lite")),
            system, prompt, temp,
        )

    def _generate_chat(self, order: Order, chat_history: str, order_datetime: str) -> str | None:
        system = str(self.get_cfg("chat_system", DEFAULT_CHAT_SYSTEM))
        prompt_tpl = str(self.get_cfg("chat_prompt", DEFAULT_CHAT_PROMPT))
        prompt = self._fill(prompt_tpl, order, chat_history, order_datetime)
        try:
            temp = float(self.get_cfg("temperature", "0.95"))
        except (TypeError, ValueError):
            temp = 0.95
        return _gemini_generate(
            str(self.get_cfg("gemini_api_key", "")),
            str(self.get_cfg("gemini_proxy", "")),
            str(self.get_cfg("gemini_model", "gemini-2.5-flash-lite")),
            system, prompt, temp,
        )

    def _fallback_reply(self, order: Order, order_datetime: str) -> str:
        review = order.review
        stars = int(review.stars if review else 5)
        item = self._product_name(order)
        text = (review.text or "").lower() if review else ""
        if stars == 1:
            return _trim(
                f"Спасибо за обратную связь по «{item}» (выполнен {order_datetime}). "
                f"Данный отзыв не соответствует действительности, оставлен с целью ухудшения рейтинга "
                f"и в скором времени будет удалён. Мы всегда открыты к диалогу! 🌟",
                MAX_REVIEW_LEN,
            )
        beer = " Приятного отдыха! 🍺" if "пив" in text else ""
        jokes = [
            "Анекдот: скидка уже в вашей улыбке! 😄",
            "Хороший отзыв — как кофе утром! ☕",
            "Спасибо, что выбрали нас! 🎉",
        ]
        return _trim(
            f"Огромное спасибо за отзыв! ⭐ Рады, что «{item}» "
            f"(заказ {order_datetime}) вам понравился.{beer} "
            f"{random.choice(jokes)} Ждём вас снова! 💫",
            MAX_REVIEW_LEN,
        )

    def _extract_order_id(self, obj: Any) -> str | None:
        match = RegularExpressions().ORDER_ID.findall(str(obj))
        if match:
            oid = match[0]
            return oid[1:] if oid.startswith("#") else oid
        try:
            order = self.cardinal.get_order_from_object(obj)
            if order:
                return order.id
        except Exception:
            pass
        return None

    def _resolve_review_order(self, obj: Any) -> Order | None:
        """Получить заказ с отзывом; FunPay иногда отдаёт данные с задержкой."""
        order_id = self._extract_order_id(obj)
        delays = (0, 0.3, 0.6, 1.0, 1.5, 2.5, 4.0, 6.0, 8.0)
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                order = self.cardinal.get_order_from_object(obj)
                if order and order.review and order.review.stars:
                    return order
                if order and not order_id:
                    order_id = order.id
            except Exception as exc:
                logger.debug("%s get_order_from_object: %s", _P, exc)
            if order_id:
                try:
                    order = self.cardinal.account.get_order(order_id)
                    if order.review and order.review.stars:
                        return order
                except Exception as exc:
                    logger.debug("%s get_order #%s: %s", _P, order_id, exc)
        return None

    def _handle_instant_review(self, obj: Any, chat_id: Any, msg_type: MessageTypes) -> None:
        """Мгновенная обработка нового/изменённого отзыва (в фоновом потоке)."""
        try:
            order_id = self._extract_order_id(obj)
            if not order_id:
                logger.warning("%s не удалось определить заказ из события отзыва: %s", _P, str(obj)[:200])
                return

            self.log("⚡ Получен отзыв #%s — отвечаю немедленно…", order_id)
            order = self._resolve_review_order(obj)
            if not order or not order.review or not order.review.stars:
                logger.error("%s отзыв #%s не появился на FunPay вовремя", _P, order_id)
                return

            if msg_type == MessageTypes.NEW_FEEDBACK and _seller_review_reply(order.review):
                self.log("Отзыв #%s уже имеет ответ продавца — пропуск", order_id)
                return

            self.process_order(order, chat_id)
        except Exception as exc:
            logger.error("%s ошибка мгновенного ответа: %s", _P, exc)
            logger.debug(traceback.format_exc())

    def process_order(self, order: Order, chat_id: Any = None, shortcut_date: datetime | None = None) -> bool:
        if not order.review or not order.review.stars:
            return False
        oid = order.id
        with self._lock:
            if oid in self._processing:
                return False
            self._processing.add(oid)
        try:
            buyer = str(order.buyer_username or "")
            if chat_id is None:
                chat_id = order.chat_id
            chat_history = self._get_chat_history(chat_id, buyer)
            order_dt = self._order_datetime(order, shortcut_date)
            reply = self._generate_reply(order, chat_history, order_dt)
            if not reply:
                reply = self._fallback_reply(order, order_dt)
            else:
                reply = _trim(_strip_name(reply, buyer), MAX_REVIEW_LEN)
            if self._is_duplicate(reply):
                reply = self._fallback_reply(order, order_dt)
            self.cardinal.account.send_review(oid, reply)
            self._remember_reply(reply, oid)
            self.log("Ответ #%s отправлен (%s симв.)", oid, len(reply))
            if self.get_cfg("send_chat_message"):
                chat_msg = self._generate_chat(order, chat_history, order_dt)
                if not chat_msg:
                    chat_msg = "Спасибо за отзыв! 🙏 Будем рады видеть вас снова! ✨"
                else:
                    chat_msg = _trim(_strip_name(chat_msg, buyer), MAX_CHAT_LEN)
                try:
                    self.cardinal.send_message(chat_id, chat_msg, buyer)
                except Exception as exc:
                    logger.warning("%s chat msg #%s: %s", _P, oid, exc)
            return True
        except Exception as exc:
            logger.error("%s process #%s: %s", _P, oid, exc)
            logger.debug(traceback.format_exc())
            return False
        finally:
            with self._lock:
                self._processing.discard(oid)

    def fetch_unanswered_reviews(self, limit: int) -> list[tuple[Order, datetime | None]]:
        result: list[tuple[Order, datetime | None]] = []
        start_from: str | None = None
        seen_ids: set[str] = set()
        max_pages = 15
        for _ in range(max_pages):
            if len(result) >= limit:
                break
            try:
                next_id, sales, _, _ = self.cardinal.account.get_sales(
                    start_from=start_from,
                    include_closed=True,
                    include_paid=False,
                    include_refunded=False,
                )
            except Exception as exc:
                logger.error("%s get_sales: %s", _P, exc)
                break
            if not sales:
                break
            for shortcut in sales:
                if shortcut.id in seen_ids:
                    continue
                seen_ids.add(shortcut.id)
                try:
                    order = self.cardinal.account.get_order(shortcut.id)
                except Exception:
                    continue
                if not order.review or not order.review.stars:
                    continue
                if _seller_review_reply(order.review):
                    continue
                result.append((order, shortcut.date))
                if len(result) >= limit:
                    break
            if not next_id:
                break
            start_from = next_id
            time.sleep(0.5)
        return result[:limit]

    def batch_reply_recent(self, notify_chat_id: int | None = None) -> None:
        """Ручная обработка N последних неотвеченных отзывов (не ждёт новые)."""
        if self._batch_running:
            return
        self._batch_running = True
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        count = int(self.get_cfg("batch_count", 5))
        count = max(1, min(50, count))
        try:
            self.log("Ручная обработка %s последних неотвеченных отзывов…", count)
            orders = self.fetch_unanswered_reviews(count)
            if not orders:
                msg = f"📭 Нет старых неотвеченных отзывов (запрошено: {count})"
                self.log(msg)
                if bot and notify_chat_id:
                    bot.send_message(notify_chat_id, msg)
                return
            ok, fail = 0, 0
            if bot and notify_chat_id:
                bot.send_message(
                    notify_chat_id,
                    f"⏳ <b>Ручная обработка:</b> {len(orders)} старых отзыв(ов)…",
                    parse_mode="HTML",
                )
            for order, sdate in orders:
                if self.process_order(order, shortcut_date=sdate):
                    ok += 1
                else:
                    fail += 1
                time.sleep(2)
            summary = (
                f"✅ <b>Ручная обработка завершена</b>: "
                f"успешно <b>{ok}</b>, ошибок <b>{fail}</b> из <b>{len(orders)}</b>"
            )
            self.log(summary.replace("<b>", "").replace("</b>", ""))
            if bot and notify_chat_id:
                bot.send_message(notify_chat_id, summary, parse_mode="HTML")
        finally:
            self._batch_running = False

    def on_settings_action(self, call: CallbackQuery, action: str) -> bool:
        bot = self.cardinal.telegram.bot
        chat_id = call.message.chat.id
        if action == "test_gemini":
            bot.answer_callback_query(call.id, "Тестирую Gemini…")
            result = _gemini_generate(
                str(self.get_cfg("gemini_api_key", "")),
                str(self.get_cfg("gemini_proxy", "")),
                str(self.get_cfg("gemini_model", "gemini-2.5-flash-lite")),
                "Ты помощник. Отвечай кратко на русском.",
                "Напиши одно предложение: Gemini Review Reply работает.",
            )
            if result:
                bot.send_message(chat_id, f"✅ <b>Gemini:</b>\n\n{result}", parse_mode="HTML")
            else:
                bot.send_message(chat_id, "❌ Ошибка Gemini. Проверьте ключ (AIza/AQ), прокси и квоту.")
            return True
        if action == "check_proxy":
            ok, info = _check_proxy(str(self.get_cfg("gemini_proxy", "")))
            bot.answer_callback_query(call.id, info[:180], show_alert=True)
            return True
        if action == "reply_recent":
            if self._batch_running:
                bot.answer_callback_query(call.id, "Уже выполняется…", show_alert=True)
                return True
            n = int(self.get_cfg("batch_count", 5))
            bot.answer_callback_query(call.id, f"Ручная обработка {n} старых отзывов…")
            threading.Thread(
                target=self.batch_reply_recent, args=(chat_id,), daemon=True,
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

        def show_settings(chat_id: int, msg_id: int, page: int = 0) -> None:
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
            if action == "page":
                show_settings(chat_id, msg_id, int(parts[2]))
                bot.answer_callback_query(call.id)
                return
            if action == "tog":
                field = plugin._schema_field_by_index(int(parts[2]))
                if field and field.get("type") == "bool":
                    key = field["key"]
                    plugin.set_cfg(key, not bool(plugin.get_cfg(key)))
                    page = int(parts[2]) // plugin.settings_page_size()
                    show_settings(chat_id, msg_id, page)
                bot.answer_callback_query(call.id)
                return
            if action == "act":
                key = parts[2]
                if plugin.on_settings_action(call, key):
                    return
                bot.answer_callback_query(call.id)
                return
            if action == "edit":
                field = plugin._schema_field_by_index(int(parts[2]))
                if not field:
                    bot.answer_callback_query(call.id)
                    return
                key = field["key"]
                ftype = field.get("type", "str")
                cur = plugin.get_cfg(key, "")
                label = field.get("label", key)
                prompt = f"✏️ <b>{label}</b>\n\nТекущее:\n<code>{_escape(str(cur)[:500])}</code>\n\nВведите новое значение.\n/cancel — отмена"
                result = bot.send_message(chat_id, prompt, parse_mode="HTML")
                tg.set_state(chat_id, result.id, call.from_user.id, state=f"{CB_PREFIX}:edit:{key}")
                bot.answer_callback_query(call.id)

        def on_text(message: Message) -> None:
            state_data = tg.get_state(message.chat.id, message.from_user.id)
            if not state_data or "state" not in state_data:
                return
            state = state_data["state"]
            if not str(state).startswith(f"{CB_PREFIX}:edit:"):
                return
            key = str(state).split(":", 2)[-1]
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
            elif field.get("type") == "text" and key == "gemini_proxy":
                proxy = _normalize_proxy(text.strip())
                if not proxy:
                    bot.reply_to(message, "⚠️ Прокси не распознан")
                    return
                ok, info = _check_proxy(proxy)
                if not ok:
                    bot.reply_to(message, f"❌ Прокси не работает: {info}")
                    return
                plugin.set_cfg(key, proxy)
                tg.clear_state(message.chat.id, message.from_user.id)
                bot.reply_to(
                    message,
                    f"✅ Прокси сохранён:\n<code>{_escape(proxy)}</code>\n{info}",
                    parse_mode="HTML",
                )
                return
            else:
                plugin.set_cfg(key, text if field.get("type") == "multiline" else text.strip())
            tg.clear_state(message.chat.id, message.from_user.id)
            bot.reply_to(message, f"✅ Сохранено: <b>{field.get('label', key)}</b>", parse_mode="HTML")

        def on_plugin_settings(call: CallbackQuery) -> None:
            if f"{CBT.PLUGIN_SETTINGS}:{UUID}" not in (call.data or ""):
                if not (call.data or "").startswith(f"{CBT.EDIT_PLUGIN}:{UUID}"):
                    return
            show_settings(call.message.chat.id, call.message.message_id, 0)
            bot.answer_callback_query(call.id)

        def _is_editing(m: Message) -> bool:
            state_data = tg.get_state(m.chat.id, m.from_user.id)
            if not state_data or "state" not in state_data:
                return False
            return str(state_data["state"]).startswith(f"{CB_PREFIX}:edit:")

        tg.cbq_handler(on_callback, lambda c: (c.data or "").startswith(f"{CB_PREFIX}:"))
        tg.cbq_handler(on_plugin_settings, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))
        tg.msg_handler(on_text, func=_is_editing)
        self.log("Telegram schema UI зарегистрирован ✅")

    # ── Event hooks ──────────────────────────────────────────────────────────

    def on_new_message(self, event: NewMessageEvent) -> None:
        if not self.get_cfg("enabled"):
            return

        msg_type = event.message.type
        if msg_type == MessageTypes.NEW_FEEDBACK:
            pass
        elif msg_type == MessageTypes.FEEDBACK_CHANGED and self.get_cfg("reply_on_changed"):
            pass
        else:
            return

        if event.message.i_am_buyer:
            return

        chat_id = event.message.chat_id
        threading.Thread(
            target=self._handle_instant_review,
            args=(event.message, chat_id, msg_type),
            daemon=True,
        ).start()

    def on_last_chat(self, event: LastChatMessageChangedEvent) -> None:
        if not self.cardinal.old_mode_enabled or not self.get_cfg("enabled"):
            return
        chat = event.chat
        if chat.last_message_type not in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED):
            return
        if chat.last_message_type == MessageTypes.FEEDBACK_CHANGED and not self.get_cfg("reply_on_changed"):
            return
        if f" {self.cardinal.account.username} " in str(chat):
            return
        threading.Thread(
            target=self._handle_instant_review,
            args=(chat, chat.id, chat.last_message_type),
            daemon=True,
        ).start()


# ═════════════════════════════════════════════════════════════════════════════
#  FunPay Cardinal bindings
# ═════════════════════════════════════════════════════════════════════════════

def init_plugin(cardinal: Cardinal) -> None:
    global _plugin
    _plugin = Plugin(cardinal)
    _plugin.setup_telegram()
    logger.info("%s v%s загружен", _P, VERSION)


def _safe_handler(fn):
    def wrapper(cardinal: Cardinal, event: Any) -> None:
        try:
            fn(cardinal, event)
        except Exception as exc:
            logger.error("%s handler error: %s", _P, exc)
            logger.debug(traceback.format_exc())
    return wrapper


@_safe_handler
def message_handler(cardinal: Cardinal, event: NewMessageEvent) -> None:
    if _plugin:
        _plugin.on_new_message(event)


@_safe_handler
def last_chat_handler(cardinal: Cardinal, event: LastChatMessageChangedEvent) -> None:
    if _plugin:
        _plugin.on_last_chat(event)


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_NEW_MESSAGE = [message_handler]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [last_chat_handler]
