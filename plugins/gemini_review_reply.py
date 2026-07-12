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
VERSION       = "3.3.1"
DESCRIPTION   = "ИИ-ответы на отзывы — 2200+ вариантов стиля, 300–600 симв. 🌈"
CREDITS       = "Cursor AI"
UUID          = "c4e8b2f1-9a3d-4e7b-8c6f-2d1a5e9b0c3f"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

MAX_REVIEW_LEN:   Final[int] = 999
DEFAULT_MIN_REVIEW_LEN: Final[int] = 300
DEFAULT_MAX_REVIEW_LEN: Final[int] = 600
MAX_CHAT_LEN:     Final[int] = 240
MAX_PROMPT_LEN:   Final[int] = 4000
GEMINI_MAX_OUTPUT_TOKENS: Final[int] = 2048
PROMPT_PREVIEW_LEN: Final[int] = 250
CHAT_HISTORY_MAX: Final[int] = 20
SETTINGS_FILE     = f"storage/plugins/{UUID}/settings.json"
CHINESE_RE        = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
CB_PREFIX         = f"grv_{UUID[:8]}"

GEMINI_MODELS = (
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_SYSTEM_PROMPT_RULES = """
ДАННЫЕ:
Категория/товар (кратко): {item_short}
Полное название (для контекста, НЕ копировать целиком): {item}
Сумма: {cost} {currency} | Дата: {order_datetime}
Оценка: {rating}/5 | Отзыв покупателя: {text}
Чат: {chat_history}

СТИЛЬ ЭТОГО ОТВЕТА: {style_angle}

ПРАВИЛА:
Без приветствий и имён: сразу с сути. Табу: «Здравствуйте», «Приветствуем», «Добрый день», имя покупателя.
Товар: упомяни КРАТКО (до 40 символов), НЕ вставляй полное название лота с эмодзи.
Эмодзи: много ярких, но количество и расстановка — на твоё усмотрение (каждый раз по-разному).
Структура: {paragraph_rule}. Между абзацами — пустая строка.
Тон: сочный, позитивный, живой. Обыграй отзыв «{text}» по-своему.
Уникальность: этот текст должен РАДИКАЛЬНО отличаться от других — другой зачин, слова, ритм, эмодзи, структура.
Запрещённые зачины: «Огромная благодарность», «Рады, что», «Для нас каждый заказ», «Потрясающе».
Длина: строго {min_review_len}–{max_review_len} символов.
Вывод: ТОЛЬКО готовый текст ответа."""

SYSTEM_PROMPT_INTROS: Final[tuple[str, ...]] = (
    "Напиши уникальный красочный ответ продавца на отзыв ({min_review_len}–{max_review_len} симв.).",
    "Создай совершенно новый по стилю ответ на отзыв покупателя ({min_review_len}–{max_review_len} симв.).",
    "Сформулируй свежий, нешаблонный ответ продавца ({min_review_len}–{max_review_len} симв.).",
    "Придумай оригинальный позитивный ответ на отзыв ({min_review_len}–{max_review_len} симв.).",
    "Составь живой и запоминающийся ответ, не похожий на типовые ({min_review_len}–{max_review_len} симв.).",
    "Подготовь яркий ответ с необычной подачей ({min_review_len}–{max_review_len} симв.).",
    "Напиши ответ в неожиданном стиле — но тепло и позитивно ({min_review_len}–{max_review_len} симв.).",
    "Сгенерируй ответ, который нельзя спутать с шаблоном ({min_review_len}–{max_review_len} симв.).",
    "Выдай свежий текст-отклик продавца, будто пишешь впервые ({min_review_len}–{max_review_len} симв.).",
    "Собери эмоциональный ответ без клише и повторов ({min_review_len}–{max_review_len} симв.).",
    "Напиши ответ с необычным зачином и живой подачей ({min_review_len}–{max_review_len} симв.).",
    "Сделай ответ продавца ярким, искренним и непохожим на другие ({min_review_len}–{max_review_len} симв.).",
    "Сформируй позитивный отклик с акцентом на уникальность формулировок ({min_review_len}–{max_review_len} симв.).",
    "Придумай развёрнутый ответ в свободном стиле, но по правилам ({min_review_len}–{max_review_len} симв.).",
    "Подготовь ответ, где каждое предложение звучит по-новому ({min_review_len}–{max_review_len} симв.).",
    "Напиши сочный ответ-реакцию на отзыв покупателя ({min_review_len}–{max_review_len} симв.).",
    "Создай тёплый и стильный ответ без шаблонных фраз ({min_review_len}–{max_review_len} симв.).",
    "Сгенерируй живой ответ с неожиданной структурой ({min_review_len}–{max_review_len} симв.).",
    "Составь красочный ответ, который выделяется на фоне типовых ({min_review_len}–{max_review_len} симв.).",
    "Напиши оригинальный ответ продавца с яркими эмодзи ({min_review_len}–{max_review_len} симв.).",
)

PARAGRAPH_RULES: Final[tuple[str, ...]] = (
    "2 коротких абзаца",
    "3 абзаца разной длины",
    "4 абзаца, последний — приглашение вернуться",
    "2–3 абзаца, первый — самый длинный",
    "3 абзаца: короткий — длинный — короткий",
    "2 абзаца с контрастной длиной предложений",
    "3 абзаца, средний — самый эмоциональный",
    "4 коротких абзаца с ритмом",
)

_VARIANT_OPENINGS: Final[tuple[str, ...]] = (
    "Начни с яркой реакции на слова отзыва",
    "Открой ответ неожиданным комплиментом без слова «спасибо»",
    "Начни с риторического вопроса и сразу ответь на него",
    "Открой текст цепочкой из 3–5 эмодзи",
    "Начни с короткой метафоры про качество сервиса",
    "Открой ответ перефразированием отзыва покупателя",
    "Начни с акцента на эмоции после прочтения отзыва",
    "Открой фразой про ценность доверия покупателя",
    "Начни с живой реакции на оценку {rating}/5",
    "Открой ответ образом «маленького праздника» в магазине",
    "Начни с фразы про команду и её настроение",
    "Открой ответ лёгкой шуткой или игривым тоном",
    "Начни с признания, что отзыв вдохновил",
    "Открой ответ необычным сравнением (без клише)",
    "Начни с акцента на детали заказа кратко",
    "Открой ответ восклицанием без приветствия",
    "Начни с благодарности за честность в отзыве",
    "Открой текст фразой про скорость и комфорт",
    "Начни с упоминания категории товара одной фразой",
    "Открой ответ мини-историей из 1–2 предложений",
    "Начни с фразы про заботу о каждом покупателе",
    "Открой ответ нестандартным комплиментом магазину от покупателя",
)

_VARIANT_FOCUSES: Final[tuple[str, ...]] = (
    "в центре — скорость выполнения и чёткость",
    "в центре — качество услуги и внимание к деталям",
    "в центре — надёжность и стабильность сервиса",
    "в центре — забота о покупателе после заказа",
    "в центре — радость от доверия и повторных визитов",
    "в центре — профессионализм команды",
    "в центре — удобство покупки от начала до конца",
    "в центре — эмоциональный отклик на текст отзыва",
    "в центре — готовность помочь снова в любой момент",
    "в центре — стремление держать планку высоко",
)

_VARIANT_TONES: Final[tuple[str, ...]] = (
    "праздничный и энергичный",
    "спокойный премиальный",
    "тёплый дружелюбный",
    "восторженный но искренний",
    "уверенный и заботливый",
    "лёгкий с юмором",
    "вдохновляющий и мотивирующий",
    "ласковый и уважительный",
    "динамичный и современный",
    "солнечный и позитивный",
)

_EMOJI_STYLES: Final[tuple[str, ...]] = (
    "эмодзи в каждом предложении",
    "эмодзи через предложение",
    "эмодзи пачками в начале абзацев",
    "эмодзи в конце ключевых фраз",
    "много эмодзи, но без перегруза",
)


def _build_reply_variants() -> tuple[str, ...]:
    variants: list[str] = []
    for i, opening in enumerate(_VARIANT_OPENINGS):
        for j, focus in enumerate(_VARIANT_FOCUSES):
            for k, tone in enumerate(_VARIANT_TONES):
                struct = PARAGRAPH_RULES[(i + j + k) % len(PARAGRAPH_RULES)]
                emoji = _EMOJI_STYLES[(i + j + k) % len(_EMOJI_STYLES)]
                variants.append(
                    f"Вариант #{len(variants) + 1}: {opening}. "
                    f"Фокус: {focus}. Тон: {tone}. Структура: {struct}. Эмодзи: {emoji}."
                )
    return tuple(variants)


REPLY_VARIANTS: Final[tuple[str, ...]] = _build_reply_variants()

_FB_OPENINGS: Final[tuple[str, ...]] = (
    "⭐ {note} — это именно то, что заряжает команду!",
    "🌈 Услышали вас: {note} — и это круто!",
    "🔥 {note} — прямо в точку!",
    "💫 Отзыв {note} согрел!",
    "🎯 {note} — лучший сигнал, что всё ок!",
    "✨ {note} — спасибо за такие слова!",
    "🚀 {note} — мощно и по делу!",
    "💎 {note} — ценим искренне!",
    "🌟 {note} — супер-отклик!",
    "🎉 {note} — читаем и улыбаемся!",
    "⚡ {note} — энергия в чистом виде!",
    "🏆 {note} — для нас это важно!",
    "🎊 {note} — отличная оценка работы!",
    "💥 {note} — попали в десятку!",
    "🌸 {note} — очень приятно!",
    "🔔 {note} — услышали громко и чётко!",
    "🛡️ {note} — доверие дороже всего!",
    "📣 {note} — так держать!",
    "🧡 {note} — от души ценим!",
    "🎁 {note} — лучший подарок продавцу!",
    "🌊 {note} — накрывает позитивом!",
    "🏁 {note} — финиш на высоте!",
    "🎵 {note} — как любимая мелодия!",
    "🌺 {note} — ярко и тепло!",
    "🔮 {note} — магия хорошего сервиса!",
)

_FB_MIDDLES: Final[tuple[str, ...]] = (
    "По {item} ({order_datetime}, {cost} {currency}) рады, что всё прошло гладко. 💎\n\nКоманда старается делать каждый шаг простым и приятным. 🚀",
    "Заказ {item} — и такой отклик вдохновляет работать ещё лучше. 🌟\n\nСкорость, качество и забота — наш ежедневный стандарт. ✨",
    "Сервис {item} ({order_datetime}) — и мы счастливы, что оправдали ожидания. 🔥\n\nВсегда на связи, если понадобится помощь. 💫",
    "Покупка {item} на {cost} {currency} — и такая оценка мотивирует! 🎯\n\nМы вкладываемся в комфорт на каждом этапе. ❤️",
    "По {item} ({order_datetime}) всё сложилось отлично — спасибо за доверие! 🌈\n\nДля нас важен каждый отзыв и каждый покупатель. 🎉",
    "{item} — наша гордость, а ваш отзыв — лучшее подтверждение. 💎\n\nРаботаем, чтобы каждый заказ был лёгким. 🚀",
    "Сделка по {item} ({cost} {currency}) запомнилась как удачная. ⭐\n\nСтараемся держать планку и радовать снова. ✨",
    "Заказ {item} {order_datetime} — и мы рады, что вы довольны! 🔥\n\nКачество и поддержка — без компромиссов. 💫",
    "По {item} получили отличный сигнал — движемся в верном направлении. 🎯\n\nГотовы помочь снова в любой момент. ❤️",
    "Услуга {item} ({order_datetime}) — и такой фидбек заряжает! 🌟\n\nЦеним время и выбор каждого клиента. 🎊",
    "{item} на {cost} {currency} — и отзыв как вишенка на торте. 🍒\n\nПродолжаем делать сервис ярче. 🚀",
    "По {item} ({order_datetime}) — кайф, когда всё складывается! 💫\n\nКоманда благодарна за искренность. ✨",
)

_FB_CLOSINGS: Final[tuple[str, ...]] = (
    "Возвращайтесь — будем рады новым заказам! 🎉✨",
    "Ждём снова — готовы порадовать ещё ярче! 🌟🚀",
    "До новых встреч в магазине! 💎❤️",
    "Заходите снова — всегда на связи! 💫🔥",
    "Будем ждать вас снова! 🎊✨",
    "Возвращайтесь — есть чем удивить! 🚀🌈",
    "До скорой встречи — поможем снова! ⭐💎",
    "Ждём вас — сервис только крепнет! 🔥❤️",
    "Приходите ещё — рады каждому заказу! 🎯✨",
    "До новых покупок — удачи и хорошего дня! 🌟🎉",
    "Возвращайтесь в любое время — поможем! 💫🚀",
    "Будем рады видеть вас снова! 🌈❤️",
)

BANNED_OPENINGS: Final[tuple[str, ...]] = (
    "огромная благодарность",
    "рады, что",
    "для нас каждый заказ",
    "потрясающе",
    "какая радость",
    "огромное спасибо",
)

DEFAULT_SYSTEM_PROMPT = SYSTEM_PROMPT_INTROS[0] + _SYSTEM_PROMPT_RULES

DEFAULT_REVIEW_PROMPT = (
    "Сгенерируй уникальный ответ продавца. "
    "Длина {min_review_len}–{max_review_len} символов. "
    "Содержание должно быть СОВЕРШЕННО другим — другой зачин, мысли, эмодзи. "
    "Не копируй полное название товара."
)


def _pick_system_prompt_template(variant_idx: int = -1) -> str:
    intro = SYSTEM_PROMPT_INTROS[
        variant_idx % len(SYSTEM_PROMPT_INTROS) if variant_idx >= 0
        else random.randrange(len(SYSTEM_PROMPT_INTROS))
    ]
    return intro + _SYSTEM_PROMPT_RULES


def _pick_variant_index(order_id: str, recent: list[dict[str, Any]], total: int) -> int:
    used = {r.get("variant_id") for r in recent if r.get("variant_id") is not None}
    base = int(hashlib.md5(order_id.encode()).hexdigest()[:8], 16) % max(total, 1)
    for offset in range(min(total, 50)):
        idx = (base + offset * 17) % total
        if idx not in used:
            return idx
    return random.randrange(total)


def _pick_style_angle(variant_idx: int | None = None) -> str:
    if variant_idx is not None and 0 <= variant_idx < len(REPLY_VARIANTS):
        return REPLY_VARIANTS[variant_idx]
    return random.choice(REPLY_VARIANTS)


def _compose_fallback_text(
    seed: int, note: str, item: str, order_datetime: str, cost: Any, currency: str,
) -> str:
    o_n, m_n, c_n = len(_FB_OPENINGS), len(_FB_MIDDLES), len(_FB_CLOSINGS)
    total = o_n * m_n * c_n
    idx = seed % total
    opening = _FB_OPENINGS[idx % o_n]
    middle = _FB_MIDDLES[(idx // o_n) % m_n]
    closing = _FB_CLOSINGS[(idx // (o_n * m_n)) % c_n]
    subs = {
        "{note}": note,
        "{item}": item,
        "{order_datetime}": order_datetime,
        "{cost}": str(cost),
        "{currency}": str(currency),
    }
    parts = (opening, middle, closing)
    for k, v in subs.items():
        parts = tuple(p.replace(k, v) for p in parts)
    return f"{parts[0]} {parts[1]}\n\n{parts[2]}"


def _pick_paragraph_rule() -> str:
    return random.choice(PARAGRAPH_RULES)


def _opening_phrase(text: str, n: int = 40) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip().lower())
    return clean[:n]


def _is_too_similar(text: str, recent: list[dict[str, Any]], threshold: float = 0.55) -> bool:
    if not text or not recent:
        return False
    opening = _opening_phrase(text)
    if any(opening.startswith(banned) for banned in BANNED_OPENINGS):
        return True
    words = set(re.findall(r"[а-яёa-z]{4,}", text.lower()))
    if not words:
        return False
    for entry in recent:
        prev = str(entry.get("text", ""))
        if not prev:
            continue
        if _opening_phrase(prev, len(opening)) == opening:
            return True
        prev_words = set(re.findall(r"[а-яёa-z]{4,}", prev.lower()))
        if not prev_words:
            continue
        overlap = len(words & prev_words) / max(len(words | prev_words), 1)
        if overlap >= threshold:
            return True
    return False

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

_REVIEW_MSG_TYPES = (
    MessageTypes.NEW_FEEDBACK,
    MessageTypes.FEEDBACK_CHANGED,
)

_REVIEW_DELETE_TYPES = (MessageTypes.FEEDBACK_DELETED,)

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


def _gemini_generation_config(model: str, temperature: float) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
    }
    if "2.5" in model or "3." in model:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    return cfg


def _sanitize_reply_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"[#*_`]+", "", text)
    text = text.replace("【", "").replace("】", "").replace("「", "").replace("」", "")
    text = CHINESE_RE.sub("", text)
    text = re.sub(r"\s+\n", "\n", text)
    return re.sub(r" {2,}", " ", text).strip()


def _is_incomplete_reply(text: str) -> bool:
    text = (text or "").strip()
    if len(text) < 40:
        return True
    if text[-1] in ".!?…:;)»\"":
        return False
    if text.endswith("…"):
        return False
    return True


def _format_for_funpay_review(text: str, max_len: int | None = None) -> str:
    """Форматирование под FunPay: до max_len символов и не более 9 переносов строк."""
    text = _sanitize_reply_text(text)
    max_l = max_len or MAX_REVIEW_LEN
    if len(text) <= max_l:
        trimmed = text
    else:
        trimmed = text[: max_l + 1]
        indexes: list[int] = []
        for char in (".", "!", "?", "\n"):
            idx = trimmed.rfind(char)
            indexes.extend([idx, trimmed[:idx].rfind(char) if idx >= 0 else -1])
        cut_at = max(indexes, key=lambda x: (x < len(trimmed) - 1, x))
        if cut_at > max_l // 2:
            trimmed = trimmed[: cut_at + 1].strip()
        else:
            trimmed = trimmed[:max_l].rsplit(" ", 1)[0].strip() + "…"
    while trimmed.count("\n") > 9 and trimmed.count("\n\n") > 1:
        trimmed = trimmed[::-1].replace("\n\n", "\n", 1)[::-1]
    if trimmed.count("\n") > 9:
        trimmed = trimmed[::-1].replace("\n", " ", trimmed.count("\n") - 9)[::-1]
    return trimmed.strip()


def _gemini_generate(
    api_key: str, proxy: str, model: str,
    system: str, prompt: str, temperature: float = 0.95,
    min_len: int = 0, max_len: int = 0,
) -> str | None:
    if not api_key.strip():
        return None
    proxies = _client_proxies(proxy)
    for try_model in [model] + [m for m in GEMINI_MODELS if m != model]:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": _gemini_generation_config(try_model, temperature),
        }
        try:
            resp = http_post(
                _gemini_url(try_model), json=payload,
                headers=_request_headers(api_key), proxies=proxies, timeout=90,
            )
            if resp.status_code in (404, 429):
                if resp.status_code == 429:
                    time.sleep(3)
                continue
            if resp.status_code != 200:
                logger.warning("%s Gemini %s: %s", _P, resp.status_code, resp.text[:180])
                continue
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                continue
            candidate = candidates[0]
            finish = str(candidate.get("finishReason") or "")
            parts = candidate.get("content", {}).get("parts", [])
            text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
            text = _sanitize_reply_text(text)
            if not text or _is_bad(text):
                continue
            if finish == "MAX_TOKENS" and _is_incomplete_reply(text):
                logger.warning(
                    "%s Gemini %s: обрезанный ответ (%s симв., finish=%s)",
                    _P, try_model, len(text), finish,
                )
                continue
            if _is_incomplete_reply(text):
                logger.warning("%s Gemini %s: незавершённый ответ (%s симв.)", _P, try_model, len(text))
                continue
            if min_len and len(text) < min_len:
                logger.warning(
                    "%s Gemini %s: короткий ответ (%s < %s симв.)",
                    _P, try_model, len(text), min_len,
                )
                continue
            if max_len and len(text) > max_len:
                logger.warning(
                    "%s Gemini %s: длинный ответ (%s > %s симв.)",
                    _P, try_model, len(text), max_len,
                )
                text = _format_for_funpay_review(text, max_len)
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


def _review_fingerprint(order: Order) -> str:
    review = order.review
    if not review:
        return ""
    normalized = re.sub(r"\s+", " ", str(review.text or "").strip().lower())
    payload = f"{review.stars or 0}|{normalized}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _format_datetime(dt: datetime) -> str:
    return f"{dt.day} {MONTHS_RU[dt.month - 1]} {dt.year} года, {dt.strftime('%H:%M')}"


def _escape(val: Any) -> str:
    return html.escape(str(val if val is not None else ""))


def _register_priority_cbq(tg, handler, predicate) -> None:
    """Регистрирует callback-хэндлер до catch-all default_cp в Cardinal."""
    tg.cbq_handler(handler, predicate)
    handlers = tg.bot.callback_query_handlers
    if handlers:
        handlers.insert(0, handlers.pop())


def _clean_reply_text(text: str | None, bot_suffixes: tuple[str, ...] = ()) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", "", str(text))
    for suf in bot_suffixes:
        if suf and cleaned.endswith(suf):
            cleaned = cleaned[: -len(suf)]
    cleaned = cleaned.replace("​‍‌", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _profile_reply_has_seller(reply: str | None, bot_suffixes: tuple[str, ...] = ()) -> bool:
    return len(_clean_reply_text(reply, bot_suffixes)) >= 2


def _seller_review_reply(review: Any) -> str | None:
    """Текст ответа продавца на отзыв (FunPayAPI: .reply, не .answer)."""
    if not review:
        return None
    return getattr(review, "reply", None) or getattr(review, "answer", None)


def _has_seller_reply(review: Any, bot_suffixes: tuple[str, ...] = ()) -> bool:
    return _profile_reply_has_seller(_seller_review_reply(review), bot_suffixes)


def _parse_profile_reviews_page(html: str) -> tuple[list[dict[str, Any]], str | None]:
    """Парсит HTML батча отзывов со страницы профиля (POST users/reviews)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        _pip("beautifulsoup4")
        from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "lxml")
    items: list[dict[str, Any]] = []
    for div in soup.select("div.review-container"):
        order_id = None
        order_div = div.select_one("div.review-item-order")
        if order_div:
            m = re.search(r"#([A-Z0-9]+)", order_div.get_text(" ", strip=True), re.I)
            if m:
                order_id = m.group(1).upper()
        reply = None
        reply_div = div.select_one("div.review-compiled-reply > div:not([class])")
        if reply_div:
            reply = reply_div.get_text(" ", strip=True) or None
        stars = None
        for i in range(5, 0, -1):
            if div.select_one(f"div.rating{i}"):
                stars = i
                break
        items.append({"order_id": order_id, "reply": reply, "stars": stars})
    nxt_el = soup.find("input", {"type": "hidden", "name": "continue"})
    next_id = (nxt_el.get("value") or "").strip() if nxt_el else ""
    return items, next_id or None


def _gemini_test_ping(api_key: str, proxy: str, model: str) -> tuple[bool, str]:
    """Короткий тест API без проверок длины ответа на отзыв."""
    if not api_key.strip():
        return False, "API key не задан"
    proxies = _client_proxies(proxy)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Ответь одним словом: OK"}]}],
        "generationConfig": _gemini_generation_config(model, 0.3),
    }
    last_err = "нет ответа"
    for try_model in [model] + [m for m in GEMINI_MODELS if m != model]:
        try:
            resp = http_post(
                _gemini_url(try_model), json=payload,
                headers=_request_headers(api_key), proxies=proxies, timeout=60,
            )
            if resp.status_code != 200:
                last_err = f"{try_model}: HTTP {resp.status_code} — {resp.text[:200]}"
                if resp.status_code in (404, 429):
                    continue
                continue
            data = resp.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return True, f"{try_model}: {text}"
            last_err = f"{try_model}: пустой ответ ({data})"[:200]
        except Exception as exc:
            last_err = f"{try_model}: {exc}"
    return False, last_err


def _short_product_label(order: Order, max_len: int = 45) -> str:
    """Короткое название без простыни эмодзи из лота."""
    raw = ""
    if order.short_description:
        raw = order.short_description.strip()
    elif order.subcategory:
        raw = order.subcategory.fullname.strip()
    elif order.lot_params_text:
        raw = order.lot_params_text.strip()[:80]
    else:
        raw = "товар"
    raw = re.sub(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" ,.-")
    if len(raw) > max_len:
        raw = raw[:max_len].rsplit(" ", 1)[0].strip() or raw[:max_len]
    return raw or "товар"


def _has_buyer_review(order: Order | None) -> bool:
    if not order or not order.review:
        return False
    review = order.review
    if review.hidden:
        return False
    stars = getattr(review, "stars", None)
    if stars is not None and int(stars) > 0:
        return True
    text = str(review.text or "").strip()
    return bool(text)


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
        self._last_variant_id: int | None = None
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
                old_ver = str(loaded.get("_cfg_version", "0"))
                for k, v in defaults.items():
                    loaded.setdefault(k, v)
                if old_ver < "3.0.4":
                    loaded["reply_on_changed"] = True
                if old_ver < "3.0.5":
                    loaded.setdefault("batch_only_unanswered", False)
                if old_ver < "3.0.7":
                    loaded["_cfg_version"] = "3.0.7"
                if old_ver < "3.0.8":
                    loaded["system_prompt"] = DEFAULT_SYSTEM_PROMPT
                    loaded["review_prompt"] = DEFAULT_REVIEW_PROMPT
                if old_ver < "3.0.9":
                    loaded["_cfg_version"] = "3.0.9"
                if old_ver < "3.1.0":
                    loaded["_cfg_version"] = "3.1.0"
                if old_ver < "3.1.1":
                    loaded.setdefault("min_review_len", DEFAULT_MIN_REVIEW_LEN)
                    loaded["_cfg_version"] = "3.1.1"
                if old_ver < "3.1.2":
                    loaded["system_prompt"] = DEFAULT_SYSTEM_PROMPT
                    loaded["review_prompt"] = DEFAULT_REVIEW_PROMPT
                    loaded["min_review_len"] = DEFAULT_MIN_REVIEW_LEN
                    loaded["_cfg_version"] = "3.1.2"
                if old_ver < "3.1.3":
                    loaded["system_prompt"] = DEFAULT_SYSTEM_PROMPT
                    loaded["review_prompt"] = DEFAULT_REVIEW_PROMPT
                    loaded.setdefault("min_review_len", DEFAULT_MIN_REVIEW_LEN)
                    loaded.setdefault("max_review_len", DEFAULT_MAX_REVIEW_LEN)
                    loaded.setdefault("use_system_variants", True)
                    loaded["_cfg_version"] = "3.1.3"
                if old_ver < "3.2.0":
                    loaded["system_prompt"] = DEFAULT_SYSTEM_PROMPT
                    loaded["review_prompt"] = DEFAULT_REVIEW_PROMPT
                    loaded["min_review_len"] = DEFAULT_MIN_REVIEW_LEN
                    loaded["max_review_len"] = DEFAULT_MAX_REVIEW_LEN
                    loaded.setdefault("use_system_variants", True)
                    loaded.setdefault("batch_unanswered_count", loaded.get("batch_count", 5))
                    loaded["_cfg_version"] = "3.2.0"
                if old_ver < "3.2.1":
                    loaded.setdefault("batch_scan_depth", 200)
                    loaded["_cfg_version"] = "3.2.1"
                if old_ver < "3.3.0":
                    loaded["_cfg_version"] = "3.3.0"
                else:
                    loaded.setdefault("_cfg_version", "3.3.0")
                self._cfg = loaded
                if old_ver < "3.3.0":
                    self._save_settings()
            else:
                self._cfg = defaults
                self._save_settings()
        except Exception as exc:
            logger.error("%s settings load: %s", _P, exc)
            self._cfg = defaults

    def _save_settings_dict(self, cfg: dict[str, Any]) -> None:
        with self._lock:
            tmp = f"{SETTINGS_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)

    def _save_settings(self) -> None:
        self._save_settings_dict(self._cfg)

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
            "temperature": "1.0",
            "min_review_len": DEFAULT_MIN_REVIEW_LEN,
            "max_review_len": DEFAULT_MAX_REVIEW_LEN,
            "use_system_variants": True,
            "send_chat_message": True,
            "reply_on_changed": True,
            "batch_count": 5,
            "batch_unanswered_count": 10,
            "batch_scan_depth": 200,
            "batch_only_unanswered": True,
            "recent_replies": [],
            "_cfg_version": "3.3.0",
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
            {"key": "system_prompt", "label": "Системный промпт", "type": "multiline", "default": DEFAULT_SYSTEM_PROMPT, "max_len": MAX_PROMPT_LEN},
            {"key": "review_prompt", "label": "Промпт ответа на отзыв", "type": "multiline", "default": DEFAULT_REVIEW_PROMPT, "max_len": MAX_PROMPT_LEN},
            {"key": "chat_system", "label": "Системный промпт (чат)", "type": "multiline", "default": DEFAULT_CHAT_SYSTEM, "max_len": MAX_PROMPT_LEN},
            {"key": "chat_prompt", "label": "Промпт благодарности в чат", "type": "multiline", "default": DEFAULT_CHAT_PROMPT, "max_len": MAX_PROMPT_LEN},
            {"key": "temperature", "label": "Temperature", "type": "text", "default": "1.0"},
            {
                "key": "min_review_len",
                "label": "Мин. длина ответа на отзыв (симв.)",
                "type": "int",
                "default": DEFAULT_MIN_REVIEW_LEN,
                "min": 50,
                "max": MAX_REVIEW_LEN,
            },
            {
                "key": "max_review_len",
                "label": "Макс. длина ответа на отзыв (симв.)",
                "type": "int",
                "default": DEFAULT_MAX_REVIEW_LEN,
                "min": 100,
                "max": MAX_REVIEW_LEN,
            },
            {
                "key": "use_system_variants",
                "label": "Случайные варианты системного промпта",
                "type": "bool",
                "default": True,
            },
            {"key": "send_chat_message", "label": "Благодарность в чат", "type": "bool", "default": True},
            {
                "key": "reply_on_changed",
                "label": "Отвечать при изменении/переписывании отзыва",
                "type": "bool",
                "default": True,
            },
            {"key": "batch_count", "label": "Кол-во последних отзывов (N)", "type": "int", "default": 5, "min": 1, "max": 50},
            {
                "key": "batch_unanswered_count",
                "label": "Кол-во неотвеченных отзывов (N)",
                "type": "int",
                "default": 10,
                "min": 1,
                "max": 50,
            },
            {
                "key": "batch_scan_depth",
                "label": "Глубина сканирования отзывов",
                "type": "int",
                "default": 200,
                "min": 25,
                "max": 500,
            },
            {
                "key": "batch_only_unanswered",
                "label": "Batch: только без ответа продавца",
                "type": "bool",
                "default": True,
            },
            {"key": "reply_unanswered", "label": "📭 Ответить на N неотвеченных", "type": "action"},
            {"key": "count_unanswered", "label": "🔍 Сколько неотвеченных?", "type": "action"},
            {"key": "reply_recent", "label": "▶ Ответить на N последних (все)", "type": "action"},
            {"key": "test_gemini", "label": "🧪 Тест Gemini API", "type": "action"},
            {"key": "check_proxy", "label": "🌐 Проверить прокси", "type": "action"},
        ]

    def _prompt_edit_intro(self, field: dict[str, Any], cur: str) -> str:
        label = _escape(field.get("label", field.get("key", "")))
        total = len(cur or "")
        preview = _escape(str(cur or "")[:PROMPT_PREVIEW_LEN])
        if total > PROMPT_PREVIEW_LEN:
            preview += "…"
        return (
            f"✏️ <b>{label}</b>\n\n"
            f"📏 Сохранено: <b>{total}</b> / {MAX_PROMPT_LEN} символов\n"
            f"👁 Превью:\n<code>{preview or '—'}</code>\n\n"
            f"Отправьте новый текст одним сообщением (до {MAX_PROMPT_LEN} симв.).\n"
            f"<code>/cancel</code> — отмена"
        )

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
        if self.get_cfg("use_system_variants", True):
            lines.append(
                f"🎲 <b>Варианты стиля:</b> {len(REPLY_VARIANTS)} шаблонов + "
                f"{len(_FB_OPENINGS) * len(_FB_MIDDLES) * len(_FB_CLOSINGS)} fallback"
            )
        lines.append(
            f"📏 <b>Длина ответа:</b> {self._min_review_len()}–{self._max_review_len()} симв."
        )
        unanswered_n = int(self.get_cfg("batch_unanswered_count", 10))
        lines.append(f"📭 <b>Неотвеченные:</b> кнопка обработает до {unanswered_n} шт.")
        if self.get_cfg("enabled"):
            lines.append("⚡ <b>Автоответ:</b> включён — отвечаем сразу при новом отзыве")
        else:
            lines.append("🔴 <b>Автоответ выключен</b> — только кнопка обработки N последних отзывов")
        if self.get_cfg("reply_on_changed"):
            lines.append("🔄 <b>Изменённые отзывы:</b> отвечаем при переписывании")
        else:
            lines.append("🔴 <b>Изменённые отзывы игнорируются</b>")
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

    def _bot_reply_suffixes(self) -> tuple[str, ...]:
        try:
            acc = self.cardinal.account
            return tuple(
                s for s in (
                    getattr(acc, "bot_character", "") or "",
                    getattr(acc, "zero_width_suffix", "") or "",
                ) if s
            )
        except Exception:
            return ()

    def _order_has_seller_reply(self, review: Any) -> bool:
        return _has_seller_reply(review, self._bot_reply_suffixes())

    def _fetch_profile_reviews_html(self, continue_id: str = "", filter_val: str = "") -> str:
        acc = self.cardinal.account
        headers = {
            "accept": "*/*",
            "x-requested-with": "XMLHttpRequest",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        payload = {
            "user_id": acc.id,
            "continue": continue_id or "",
            "filter": filter_val or "",
        }
        resp = acc.method("post", "users/reviews", headers, payload)
        return resp.content.decode(errors="replace")

    def _iter_profile_reviews(self, max_items: int) -> Any:
        """Итератор по отзывам профиля продавца (POST users/reviews, пагинация)."""
        max_items = max(1, min(500, max_items))
        seen_continue: set[str] = set()
        continue_id = ""
        total = 0
        while total < max_items:
            html = self._fetch_profile_reviews_html(continue_id)
            items, next_id = _parse_profile_reviews_page(html)
            if not items:
                break
            for item in items:
                yield item
                total += 1
                if total >= max_items:
                    return
            if not next_id or next_id in seen_continue:
                break
            seen_continue.add(next_id)
            continue_id = next_id
            time.sleep(0.25)

    def _orders_from_profile_reviews(
        self, limit: int, only_unanswered: bool,
    ) -> list[tuple[Order, datetime | None]]:
        """Берёт отзывы со страницы профиля FunPay (как в UI), а не из trade-списка."""
        limit = max(1, min(50, limit))
        scan_depth = int(self.get_cfg("batch_scan_depth", 200))
        suffixes = self._bot_reply_suffixes()
        result: list[tuple[Order, datetime | None]] = []
        seen_orders: set[str] = set()
        for item in self._iter_profile_reviews(scan_depth):
            if only_unanswered and _profile_reply_has_seller(item.get("reply"), suffixes):
                continue
            oid = item.get("order_id")
            if not oid or oid in seen_orders:
                continue
            seen_orders.add(oid)
            order = self._fetch_fresh_order(oid)
            if not order or not _has_buyer_review(order):
                continue
            if only_unanswered and self._order_has_seller_reply(order.review):
                continue
            result.append((order, None))
            if len(result) >= limit:
                break
        return result

    def _count_profile_unanswered(self, scan_depth: int | None = None) -> tuple[int, int]:
        scan = scan_depth or int(self.get_cfg("batch_scan_depth", 200))
        suffixes = self._bot_reply_suffixes()
        scanned = 0
        unanswered = 0
        for item in self._iter_profile_reviews(scan):
            scanned += 1
            if not _profile_reply_has_seller(item.get("reply"), suffixes):
                unanswered += 1
        return unanswered, scanned

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

    def _min_review_len(self) -> int:
        try:
            val = int(self.get_cfg("min_review_len", DEFAULT_MIN_REVIEW_LEN))
        except (TypeError, ValueError):
            val = DEFAULT_MIN_REVIEW_LEN
        return max(50, min(MAX_REVIEW_LEN, val))

    def _max_review_len(self) -> int:
        try:
            val = int(self.get_cfg("max_review_len", DEFAULT_MAX_REVIEW_LEN))
        except (TypeError, ValueError):
            val = DEFAULT_MAX_REVIEW_LEN
        return max(self._min_review_len(), min(MAX_REVIEW_LEN, val))

    def _fill(
        self, template: str, order: Order, chat_history: str, order_datetime: str,
        style_angle: str = "", paragraph_rule: str = "",
    ) -> str:
        review = order.review
        subs = {
            "{item}": self._product_name(order),
            "{item_short}": _short_product_label(order),
            "{product_name}": self._product_name(order),
            "{cost}": str(order.sum),
            "{currency}": str(order.currency),
            "{rating}": str(review.stars if review else 5),
            "{text}": str(review.text if review and review.text else "без текста"),
            "{chat_history}": chat_history,
            "{order_datetime}": order_datetime,
            "{min_review_len}": str(self._min_review_len()),
            "{max_review_len}": str(self._max_review_len()),
            "{style_angle}": style_angle or _pick_style_angle(),
            "{paragraph_rule}": paragraph_rule or _pick_paragraph_rule(),
        }
        for k, v in subs.items():
            template = template.replace(k, v)
        return template

    def _is_duplicate(self, text: str) -> bool:
        h = _reply_hash(text)
        return any(r.get("hash") == h for r in self.get_cfg("recent_replies", []))

    def _clear_order_reply_memory(self, order_id: str) -> None:
        recent = [r for r in self.get_cfg("recent_replies", []) if r.get("order_id") != order_id]
        self.set_cfg("recent_replies", recent)
        self.log("Память ответов для #%s сброшена (отзыв удалён)", order_id)

    def _remember_reply(self, text: str, order: Order) -> None:
        recent = list(self.get_cfg("recent_replies", []))
        recent = [r for r in recent if not (
            r.get("order_id") == order.id and r.get("review_hash") == _review_fingerprint(order)
        )]
        recent.append({
            "hash": _reply_hash(text),
            "order_id": order.id,
            "review_hash": _review_fingerprint(order),
            "text": text[:200],
            "variant_id": getattr(self, "_last_variant_id", None),
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.set_cfg("recent_replies", recent[-50:])

    def _generate_reply(self, order: Order, chat_history: str, order_datetime: str) -> str | None:
        try:
            temp = float(self.get_cfg("temperature", "1.0"))
        except (TypeError, ValueError):
            temp = 1.0
        min_len = self._min_review_len()
        max_len = self._max_review_len()
        recent = self.get_cfg("recent_replies", [])[-12:]
        api_key = str(self.get_cfg("gemini_api_key", ""))
        proxy = str(self.get_cfg("gemini_proxy", ""))
        model = str(self.get_cfg("gemini_model", "gemini-2.5-flash-lite"))
        history_block = ""
        if recent:
            history_block = "\n\nЗАПРЕЩЕНО повторять эти ответы (другой зачин и содержание):\n"
            for i, r in enumerate(recent, 1):
                history_block += f"{i}. {r.get('text', '')[:120]}\n"
        self._last_variant_id: int | None = None
        for attempt in range(5):
            variant_idx = _pick_variant_index(order.id, recent, len(REPLY_VARIANTS))
            if attempt > 0:
                variant_idx = (variant_idx + attempt * 31) % len(REPLY_VARIANTS)
            self._last_variant_id = variant_idx
            style_angle = _pick_style_angle(variant_idx)
            paragraph_rule = PARAGRAPH_RULES[variant_idx % len(PARAGRAPH_RULES)]
            if self.get_cfg("use_system_variants", True):
                system_tpl = _pick_system_prompt_template(variant_idx)
            else:
                system_tpl = str(self.get_cfg("system_prompt", DEFAULT_SYSTEM_PROMPT))
            system = self._fill(
                system_tpl, order, chat_history, order_datetime,
                style_angle=style_angle, paragraph_rule=paragraph_rule,
            )
            review_tpl = str(self.get_cfg("review_prompt", DEFAULT_REVIEW_PROMPT))
            if not review_tpl.strip():
                review_tpl = DEFAULT_REVIEW_PROMPT
            base_prompt = self._fill(
                review_tpl, order, chat_history, order_datetime,
                style_angle=style_angle, paragraph_rule=paragraph_rule,
            )
            extra = ""
            if attempt == 1:
                extra = (
                    f"\n\nПредыдущий ответ слишком короткий. "
                    f"Напиши заново: {min_len}–{max_len} символов."
                )
            elif attempt == 2:
                extra = (
                    f"\n\nПредыдущий ответ был шаблонным. "
                    f"Используй вариант #{variant_idx + 1} — другой зачин и структура."
                )
            elif attempt >= 3:
                extra = (
                    f"\n\nКРИТИЧНО: вариант #{variant_idx + 1} из {len(REPLY_VARIANTS)}. "
                    f"Другая структура, слова, эмодзи. Стиль: {style_angle[:120]}…"
                )
            length_rule = (
                f"\n\nДлина: {min_len}–{max_len} символов. "
                f"Без приветствий. Не копируй полное название товара."
            )
            attempt_temp = min(1.2, temp + attempt * 0.05)
            prompt = base_prompt + history_block + length_rule + extra
            reply = _gemini_generate(
                api_key, proxy, model, system, prompt, attempt_temp,
                min_len=min_len, max_len=max_len,
            )
            if reply and not _is_too_similar(reply, recent):
                return reply
            if reply:
                logger.warning("%s ответ похож на предыдущие — повтор генерации", _P)
        return None

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

    def _expand_reply_to_min(self, text: str, min_len: int, max_len: int | None = None) -> str:
        text = (text or "").strip()
        cap = max_len or self._max_review_len()
        if len(text) >= min_len:
            return text[:cap]
        fillers = [
            " Нам очень приятно, что вы нашли время поделиться впечатлением — для нас это действительно важно. 💫",
            " Мы всегда на связи и с радостью поможем, если понадобится что-то ещё. 🌟",
            " Будем рады видеть вас снова и постараемся каждый раз оправдывать доверие. ✨",
            " Спасибо, что выбираете нас — мы ценим каждого покупателя и стараемся работать на отлично. 🎉",
            " Если захотите повторить заказ — будем только рады помочь снова. 🔥",
            " Такие отзывы вдохновляют нас становиться ещё лучше каждый день! 💎",
            " Качество и забота о покупателе — наш главный приоритет. ❤️",
        ]
        pool = fillers.copy()
        random.shuffle(pool)
        for phrase in pool:
            if len(text) >= min_len:
                break
            if len(text) + len(phrase) > cap:
                break
            text = (text + phrase).strip()
        tail = " Ждём вас снова! ✨"
        while len(text) < min_len and len(text) + len(tail) <= cap:
            text += tail
        return text[:cap]

    def _fallback_reply(self, order: Order, order_datetime: str) -> str:
        min_len = self._min_review_len()
        max_len = self._max_review_len()
        review = order.review
        stars = int(review.stars if review else 5)
        item = _short_product_label(order)
        review_text = (review.text or "").strip() if review else ""
        seed = int(hashlib.md5(f"{order.id}:{review_text}".encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        if stars == 1:
            bases = [
                (
                    f"Жаль, что {item} ({order_datetime}) не зашёл как ожидалось. 😔 "
                    f"Разберёмся в чате и постараемся всё исправить — нам важен каждый покупатель. 🙏"
                ),
                (
                    f"Видим оценку по {item} и хотим разобраться. 💬 "
                    f"Напишите в чат — найдём решение и улучшим сервис. ✨"
                ),
            ]
            base = rng.choice(bases)
            return _format_for_funpay_review(
                self._expand_reply_to_min(base, min_len, max_len), max_len,
            )
        note = f"«{review_text}»" if review_text and review_text not in (".", "…") else "ваша оценка"
        fb_total = len(_FB_OPENINGS) * len(_FB_MIDDLES) * len(_FB_CLOSINGS)
        base = _compose_fallback_text(
            seed, note, item, order_datetime, order.sum, order.currency,
        )
        if "пив" in review_text.lower():
            base += " 🍺 Приятного отдыха!"
        logger.debug("%s fallback вариант %s / %s", _P, seed % fb_total, fb_total)
        return _format_for_funpay_review(
            self._expand_reply_to_min(base, min_len, max_len), max_len,
        )

    def _extract_order_id(self, obj: Any) -> str | None:
        for source in (
            getattr(obj, "text", None),
            getattr(obj, "last_message_text", None),
            str(obj),
        ):
            if not source:
                continue
            match = RegularExpressions().ORDER_ID.findall(str(source))
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

    def _fetch_order_with_review(self, order_id: str) -> Order | None:
        """Свежий get_order — без кэша get_order_from_object на объекте сообщения."""
        delays = (0, 0.3, 0.6, 1.0, 1.5, 2.5, 4.0, 6.0, 8.0, 10.0)
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                order = self.cardinal.account.get_order(order_id)
                if _has_buyer_review(order):
                    return order
            except Exception as exc:
                logger.debug("%s get_order #%s: %s", _P, order_id, exc)
        return None

    def _should_skip_review(self, order: Order, msg_type: MessageTypes) -> bool:
        """Пропуск только если на этот же текст отзыва уже отвечали."""
        if msg_type == MessageTypes.FEEDBACK_CHANGED:
            return False
        if not _has_seller_reply(order.review, self._bot_reply_suffixes()):
            return False
        fp = _review_fingerprint(order)
        if not fp:
            return False
        for entry in reversed(self.get_cfg("recent_replies", [])):
            if entry.get("order_id") == order.id and entry.get("review_hash") == fp:
                return True
        return False

    def _handle_review_deleted(self, obj: Any) -> None:
        order_id = self._extract_order_id(obj)
        if not order_id:
            return
        self._clear_order_reply_memory(order_id)

    def _handle_instant_review(self, obj: Any, chat_id: Any, msg_type: MessageTypes) -> None:
        """Мгновенная обработка нового/изменённого отзыва (в фоновом потоке)."""
        try:
            order_id = self._extract_order_id(obj)
            if not order_id:
                logger.warning("%s не удалось определить заказ из события отзыва: %s", _P, str(obj)[:200])
                return

            self.log(
                "⚡ Событие %s для #%s — отвечаю немедленно…",
                msg_type.name if hasattr(msg_type, "name") else msg_type,
                order_id,
            )
            order = self._fetch_order_with_review(order_id)
            if not _has_buyer_review(order):
                logger.error("%s отзыв #%s не появился на FunPay вовремя", _P, order_id)
                return

            if self._should_skip_review(order, msg_type):
                self.log("Отзыв #%s без изменений — пропуск", order_id)
                return

            if msg_type == MessageTypes.FEEDBACK_CHANGED:
                self.log("Отзыв #%s изменён — обновляю ответ продавца", order_id)

            self.process_order(order, chat_id, force_update=msg_type == MessageTypes.FEEDBACK_CHANGED)
        except Exception as exc:
            logger.error("%s ошибка мгновенного ответа: %s", _P, exc)
            logger.debug(traceback.format_exc())

    def _fetch_fresh_order(self, order_id: str) -> Order | None:
        try:
            order = self.cardinal.account.get_order(order_id)
            return order if _has_buyer_review(order) else None
        except Exception as exc:
            logger.debug("%s get_order #%s: %s", _P, order_id, exc)
            return None

    def process_order(
        self, order: Order, chat_id: Any = None,
        shortcut_date: datetime | None = None, force_update: bool = False,
        only_if_unanswered: bool = False,
    ) -> bool:
        if not _has_buyer_review(order):
            return False
        order = self._fetch_fresh_order(order.id) or order
        if only_if_unanswered and self._order_has_seller_reply(order.review):
            self.log("Отзыв #%s уже с ответом продавца — пропуск", order.id)
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
            min_len = self._min_review_len()
            max_len = self._max_review_len()
            reply = self._generate_reply(order, chat_history, order_dt)
            if not reply:
                reply = self._fallback_reply(order, order_dt)
            else:
                reply = _format_for_funpay_review(_strip_name(reply, buyer), max_len)
                if len(reply) < min_len:
                    reply = _format_for_funpay_review(
                        self._expand_reply_to_min(reply, min_len, max_len), max_len,
                    )
            if not reply or _is_incomplete_reply(reply):
                reply = self._fallback_reply(order, order_dt)
            elif len(reply) < min_len:
                reply = _format_for_funpay_review(
                    self._expand_reply_to_min(reply, min_len, max_len), max_len,
                )
            if self._is_duplicate(reply) and not force_update:
                regen = self._generate_reply(order, chat_history, order_dt)
                reply = regen or self._fallback_reply(order, order_dt)
                reply = _format_for_funpay_review(_strip_name(reply, buyer), max_len)
            self.cardinal.account.send_review(oid, reply)
            self._remember_reply(reply, order)
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

    def _orders_from_shortcuts(
        self, shortcuts: list[Any],
    ) -> list[tuple[Order, datetime | None]]:
        if not shortcuts:
            return []
        result: list[tuple[Order, datetime | None]] = []
        date_map = {s.id: getattr(s, "date", None) for s in shortcuts}
        ids = [s.id for s in shortcuts]
        orders_map: dict[str, Order] = {}
        try:
            orders_map = self.cardinal.account.get_orders_by_ids(
                *ids, include_review=True, include_details=True, include_users=True,
            )
        except Exception as exc:
            logger.warning("%s get_orders_by_ids batch: %s", _P, exc)
            for oid in ids:
                try:
                    orders_map[oid] = self.cardinal.account.get_order(oid)
                except Exception as one_exc:
                    logger.debug("%s get_order #%s: %s", _P, oid, one_exc)
        for oid in ids:
            order = orders_map.get(oid)
            if not _has_buyer_review(order):
                continue
            result.append((order, date_map.get(oid)))
        return result

    def fetch_last_reviews(self, limit: int) -> list[tuple[Order, datetime | None]]:
        """Последние N отзывов с профиля продавца."""
        only_unanswered = bool(self.get_cfg("batch_only_unanswered", True))
        result = self._orders_from_profile_reviews(limit, only_unanswered)
        mode = "без ответа" if only_unanswered else "все"
        self.log("Найдено %s отзывов с профиля (%s, запрошено %s)", len(result), mode, limit)
        return result

    def fetch_unanswered_reviews(self, limit: int) -> list[tuple[Order, datetime | None]]:
        """N неотвеченных отзывов с профиля FunPay."""
        result = self._orders_from_profile_reviews(limit, only_unanswered=True)
        self.log("Найдено %s неотвеченных отзывов с профиля (запрошено %s)", len(result), limit)
        return result

    def count_unanswered_reviews(self, scan_limit: int | None = None) -> tuple[int, int]:
        """(неотвеченных, всего просмотрено) по профилю."""
        return self._count_profile_unanswered(scan_limit)

    def batch_reply_recent(self, notify_chat_id: int | None = None) -> None:
        """Ручная обработка N последних отзывов (все или только без ответа — по настройке)."""
        if self._batch_running:
            return
        self._batch_running = True
        count = int(self.get_cfg("batch_count", 5))
        count = max(1, min(50, count))
        only_unanswered = bool(self.get_cfg("batch_only_unanswered", True))
        try:
            mode = "неотвеченных" if only_unanswered else "последних"
            self.log("Обработка %s %s отзывов…", count, mode)
            orders = self.fetch_last_reviews(count)
            self._run_batch(orders, notify_chat_id, only_if_unanswered=only_unanswered)
        finally:
            self._batch_running = False

    def batch_reply_unanswered(self, notify_chat_id: int | None = None) -> None:
        """Ответить на N неотвеченных отзывов."""
        if self._batch_running:
            return
        self._batch_running = True
        count = int(self.get_cfg("batch_unanswered_count", 10))
        count = max(1, min(50, count))
        try:
            self.log("Обработка %s неотвеченных отзывов…", count)
            orders = self.fetch_unanswered_reviews(count)
            self._run_batch(orders, notify_chat_id, only_if_unanswered=True)
        finally:
            self._batch_running = False

    def _run_batch(
        self,
        orders: list[tuple[Order, datetime | None]],
        notify_chat_id: int | None,
        only_if_unanswered: bool = False,
    ) -> None:
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        if not orders:
            msg = "📭 Неотвеченных отзывов не найдено. Попробуйте увеличить N или проверьте закрытые заказы."
            if not only_if_unanswered:
                msg = "📭 Отзывы не найдены. Проверьте закрытые заказы на FunPay."
            self.log(msg)
            if bot and notify_chat_id:
                bot.send_message(notify_chat_id, msg)
            return
        ok, fail, skipped = 0, 0, 0
        if bot and notify_chat_id:
            bot.send_message(
                notify_chat_id,
                f"⏳ <b>Обрабатываю {len(orders)} отзыв(ов)…</b>",
                parse_mode="HTML",
            )
        for order, sdate in orders:
            if only_if_unanswered and self._order_has_seller_reply(order.review):
                skipped += 1
                continue
            if self.process_order(
                order, shortcut_date=sdate, force_update=not only_if_unanswered,
                only_if_unanswered=only_if_unanswered,
            ):
                ok += 1
            else:
                fail += 1
                logger.warning("%s не удалось обработать отзыв #%s", _P, order.id)
            time.sleep(2)
        summary = (
            f"✅ <b>Готово:</b> успешно <b>{ok}</b>, ошибок <b>{fail}</b>"
            f"{f', пропущено <b>{skipped}</b>' if skipped else ''} из <b>{len(orders)}</b>"
        )
        self.log(summary.replace("<b>", "").replace("</b>", ""))
        if bot and notify_chat_id:
            bot.send_message(notify_chat_id, summary, parse_mode="HTML")

    def on_settings_action(self, call: CallbackQuery, action: str) -> bool:
        bot = self.cardinal.telegram.bot
        chat_id = call.message.chat.id
        if action == "test_gemini":
            bot.answer_callback_query(call.id, "Тестирую Gemini…")
            ok, info = _gemini_test_ping(
                str(self.get_cfg("gemini_api_key", "")),
                str(self.get_cfg("gemini_proxy", "")),
                str(self.get_cfg("gemini_model", "gemini-2.5-flash-lite")),
            )
            if ok:
                bot.send_message(chat_id, f"✅ <b>Gemini OK:</b>\n\n<code>{_escape(info)}</code>", parse_mode="HTML")
            else:
                bot.send_message(
                    chat_id,
                    f"❌ <b>Ошибка Gemini</b>\n\n<code>{_escape(info)}</code>\n\n"
                    f"Прокси: <code>{_escape(_normalize_proxy(str(self.get_cfg('gemini_proxy', ''))) or '—')}</code>",
                    parse_mode="HTML",
                )
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
            bot.answer_callback_query(call.id, f"Запуск: {n} последних отзывов…")
            threading.Thread(
                target=self.batch_reply_recent, args=(chat_id,), daemon=True,
            ).start()
            return True
        if action == "reply_unanswered":
            if self._batch_running:
                bot.answer_callback_query(call.id, "Уже выполняется…", show_alert=True)
                return True
            n = int(self.get_cfg("batch_unanswered_count", 10))
            bot.answer_callback_query(call.id, f"Запуск: {n} неотвеченных…")
            threading.Thread(
                target=self.batch_reply_unanswered, args=(chat_id,), daemon=True,
            ).start()
            return True
        if action == "count_unanswered":
            bot.answer_callback_query(call.id, "Сканирую профиль…")
            threading.Thread(
                target=self._notify_unanswered_count, args=(chat_id,), daemon=True,
            ).start()
            return True
        return False

    def _notify_unanswered_count(self, chat_id: int) -> None:
        bot = self.cardinal.telegram.bot if self.cardinal.telegram else None
        if not bot:
            return
        try:
            count, scanned = self.count_unanswered_reviews()
            n = int(self.get_cfg("batch_unanswered_count", 10))
            depth = int(self.get_cfg("batch_scan_depth", 200))
            bot.send_message(
                chat_id,
                f"🔍 <b>Неотвеченных отзывов:</b> <b>{count}</b>\n"
                f"📋 Просмотрено с профиля: <b>{scanned}</b> (глубина до {depth})\n\n"
                f"Кнопка «📭 Ответить на N неотвеченных» обработает до <b>{n}</b>.",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error("%s count unanswered: %s", _P, exc)
            bot.send_message(chat_id, f"❌ Ошибка сканирования: {_escape(exc)}", parse_mode="HTML")

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
                if ftype == "multiline":
                    prompt = plugin._prompt_edit_intro(field, str(cur))
                else:
                    max_len = field.get("max_len", 500)
                    preview = _escape(str(cur)[:max_len])
                    if len(str(cur)) > max_len:
                        preview += "…"
                    prompt = (
                        f"✏️ <b>{_escape(label)}</b>\n\nТекущее:\n<code>{preview}</code>\n\n"
                        f"Введите новое значение.\n/cancel — отмена"
                    )
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
            elif field.get("type") == "multiline":
                max_len = int(field.get("max_len", MAX_PROMPT_LEN))
                if len(text) > max_len:
                    bot.reply_to(
                        message,
                        f"⚠️ Слишком длинно: {len(text)} / {max_len} символов. "
                        f"Сократите промпт или разбейте шаблон.",
                    )
                    return
                plugin.set_cfg(key, text)
            else:
                plugin.set_cfg(key, text.strip())
            tg.clear_state(message.chat.id, message.from_user.id)
            if field.get("type") == "multiline":
                bot.reply_to(
                    message,
                    f"✅ Сохранено: <b>{field.get('label', key)}</b> ({len(text)} симв.)",
                    parse_mode="HTML",
                )
            else:
                bot.reply_to(message, f"✅ Сохранено: <b>{field.get('label', key)}</b>", parse_mode="HTML")

        def on_plugin_settings(call: CallbackQuery) -> None:
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            try:
                show_settings(call.message.chat.id, call.message.message_id, 0)
            except Exception as exc:
                plugin.log("ошибка открытия настроек: %s", exc)
                logger.debug("TRACEBACK", exc_info=True)
                try:
                    bot.send_message(
                        call.message.chat.id,
                        f"⚠️ Не удалось открыть настройки: <code>{_escape(exc)[:180]}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        def _is_editing(m: Message) -> bool:
            state_data = tg.get_state(m.chat.id, m.from_user.id)
            if not state_data or "state" not in state_data:
                return False
            return str(state_data["state"]).startswith(f"{CB_PREFIX}:edit:")

        tg.cbq_handler(on_callback, lambda c: (c.data or "").startswith(f"{CB_PREFIX}:"))
        _register_priority_cbq(
            tg,
            on_plugin_settings,
            lambda c: (c.data or "").startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"),
        )
        tg.msg_handler(on_text, func=_is_editing)
        self.log("Telegram schema UI зарегистрирован ✅")

    # ── Event hooks ──────────────────────────────────────────────────────────

    def on_new_message(self, event: NewMessageEvent) -> None:
        msg_type = event.message.type

        if msg_type in _REVIEW_DELETE_TYPES:
            if event.message.i_am_buyer:
                return
            threading.Thread(
                target=self._handle_review_deleted,
                args=(event.message,),
                daemon=True,
            ).start()
            return

        if not self.get_cfg("enabled"):
            return

        if msg_type not in _REVIEW_MSG_TYPES:
            return
        if msg_type == MessageTypes.FEEDBACK_CHANGED and not self.get_cfg("reply_on_changed"):
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
        if not self.cardinal.old_mode_enabled:
            return
        chat = event.chat

        if chat.last_message_type in _REVIEW_DELETE_TYPES:
            if f" {self.cardinal.account.username} " in str(chat):
                return
            threading.Thread(
                target=self._handle_review_deleted,
                args=(chat,),
                daemon=True,
            ).start()
            return

        if not self.get_cfg("enabled"):
            return
        if chat.last_message_type not in _REVIEW_MSG_TYPES:
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
