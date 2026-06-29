"""
VexBoost AutoSMM — нативный плагин Starvell Cardinal (порт FPC v2.4.4).

События:
  @on_pre_delivery  — отмена автовыдачи для SMM-лотов (ID: в описании)
  @on_order_paid    — новый оплаченный заказ: приветствие, ожидание ссылки
  @on_message       — только чат с покупателем: ссылка, +/-, #статус, #рефилл
  @on_order_completed — архивация при завершении на Starvell

Лот: ID: 1234  и опционально  #Quan: 1
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from aiogram.types import InlineKeyboardMarkup

from config import BASE_DIR
from core.plugins.context import _resolve_order_price
from starvell_sdk import (
    DeliveryContext,
    MessageContext,
    OrderContext,
    StarvellPlugin,
    on_message,
    on_order_completed,
    on_order_paid,
    on_pre_delivery,
)

NAME = "VexBoost AutoSMM"
VERSION = "3.2.0"
DESCRIPTION = "Автонакрутка SMM через VexBoost для Starvell"
CREDITS = "@xei1y"
UUID = "a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e"
SETTINGS_PAGE = True
STV_ORDER_URL = "https://starvell.com/order/{order_id}"

TELEGRAM_COMMANDS = [
    {"command": "vexboost", "description": "панель VexBoost AutoSMM"},
    {"command": "vb_stats", "description": "статистика VexBoost AutoSMM"},
    {"command": "vb_balance", "description": "баланс VexBoost AutoSMM"},
]

SERVICE_ID_RE = re.compile(r"ID:\s*(\d+)", re.IGNORECASE)
QUAN_RE = re.compile(r"#Quan:\s*(\d+)", re.IGNORECASE)
LINK_RE = re.compile(
    r"https?://(?:[a-zA-Z0-9]|[$-_@.&+]|[%][0-9a-fA-F]{2})+",
    re.IGNORECASE,
)

CONFIRM_PLUS = {"+", "➕", "✅", "yes", "да", "ok", "подтверждаю"}
CONFIRM_MINUS = {"-", "➖", "❌", "no", "нет", "отмена"}

SUPPORTED_DOMAINS = (
    "t.me", "telegram.me", "telegram.org",
    "tiktok.com", "vm.tiktok.com",
    "youtube.com", "youtu.be",
    "instagram.com", "instagr.am",
    "vk.com", "vk.ru",
    "twitter.com", "x.com",
    "facebook.com", "fb.com", "fb.watch",
    "twitch.tv", "discord.gg", "discord.com",
    "ok.ru", "odnoklassniki.ru",
    "likee.video", "likee.com",
    "snapchat.com", "pinterest.com",
    "threads.net", "kick.com",
)

DEFAULT_TEMPLATES: dict[str, str] = {
    "welcome_message": (
        "👋 Спасибо за заказ!\n"
        "Отправьте ссылку на аккаунт или пост для накрутки."
    ),
    "confirmation_message": (
        "📋 Проверьте детали заказа:\n\n"
        "🛒 {lot}\n"
        "💵 Сумма: {price}\n"
        "🔢 Количество: {amount} шт.\n"
        "🔗 {link}\n\n"
        "✅ + подтвердить\n"
        "❌ - отменить и вернуть средства\n"
        "🔄 Или отправьте новую ссылку"
    ),
    "creating_order_message": "⏳ Создаю заказ, подождите...",
    "order_created_message": (
        "📊 Заказ создан и отправлен SMM-сервису!\n"
        "🆔 ID: {smm_id}\n\n"
        "📋 Команды:\n"
        "⠀∟ #статус {smm_id}\n"
        "⠀∟ #рефилл {smm_id}\n\n"
        "⌛ Время выполнения: от нескольких минут до 48 часов."
    ),
    "order_cancelled_message": "❌ Заказ отменён. Средства будут возвращены.",
    "order_canceled_message": "❌ Заказ #{order_id} отменён.\nСредства будут возвращены.",
    "completion_message": (
        "✅ Заказ #{order_id} выполнен!\n\n"
        "Пожалуйста, подтвердите выполнение на Starvell:\n"
        "🔗 {order_url}\n\n"
        "Спасибо за покупку! 🙏"
    ),
    "pending_hint_message": "⚪️ Отправьте + для подтверждения, - для отмены или новую ссылку.",
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
    "refill_error_message": "🔴 Рефилл недоступен для этой услуги.",
    "partial_paused_message": (
        "⚠️ Заказ #{order_id} приостановлен.\n"
        "Остаток: {remains} ед.\n"
        "Обратитесь к продавцу."
    ),
    "partial_continued_message": (
        "📈 Заказ #{order_id} продолжен.\n"
        "⏳ Остаток к выполнению: {partial_amount} ед."
    ),
}

logger = logging.getLogger("starvell.plugin.vexboost")

# Starvell: ~40 req/min + delay 1.5s между запросами (см. config.py)
STV_MIN_API_GAP = 1.5
MESSAGE_DEDUP_TTL = 5.0


class _MessageDedup:
    """Не обрабатывает одно сообщение дважды (poll чата каждые ~5 с)."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def is_duplicate(self, chat_id: str, message_id: str, text: str) -> bool:
        key = f"{chat_id}:{message_id}" if message_id else f"{chat_id}:{hash(text.strip())}"
        now = time.monotonic()
        expired = [k for k, ts in self._seen.items() if now - ts > MESSAGE_DEDUP_TTL]
        for k in expired:
            del self._seen[k]
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


_dedup = _MessageDedup()


async def _starvell_pause(core: Any, extra: float = 0.0) -> None:
    """Пауза под rate-limit Starvell API."""
    delay = STV_MIN_API_GAP
    try:
        delay = max(STV_MIN_API_GAP, float(getattr(core.settings, "api_delay_seconds", 1.5)))
    except (TypeError, ValueError):
        pass
    await asyncio.sleep(delay + extra)


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _storage_dir() -> Path:
    p = BASE_DIR / "storage" / "plugins" / UUID
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_json(name: str, default: Any) -> Any:
    path = _storage_dir() / name
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(name: str, data: Any) -> None:
    path = _storage_dir() / name
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _format_rub(amount: Any) -> str:
    try:
        return f"{float(amount):.2f} ₽"
    except (TypeError, ValueError):
        return "0.00 ₽"


def _order_description(order: dict) -> str:
    offer = order.get("offerDetails") or {}
    desc = (offer.get("descriptions") or {}).get("rus") or {}
    parts = [
        str(desc.get("description") or ""),
        str(desc.get("briefDescription") or ""),
        str(offer.get("title") or ""),
    ]
    return "\n".join(p for p in parts if p)


def _extract_links(text: str) -> list[str]:
    return LINK_RE.findall(text or "")


def _is_private_tg(link: str) -> bool:
    return "t.me" in link and ("/c/" in link or "+" in link)


def _confirm_action(text: str) -> str | None:
    cleaned = text.strip().strip("\ufeff").lower()
    if cleaned in CONFIRM_PLUS:
        return "+"
    if cleaned in CONFIRM_MINUS:
        return "-"
    return None


def _buyer_error(error: Any) -> str:
    text = str(error or "").strip().lower()
    if "invalid link" in text or "ссылк" in text:
        return "Некорректная ссылка. Проверьте и отправьте снова."
    if "quantity" in text or "количеств" in text:
        return "Некорректное количество. Обратитесь к продавцу."
    if "service" in text or "услуг" in text:
        return "Ошибка параметров заказа. Обратитесь к продавцу."
    if "fund" in text or "средств" in text or "баланс" in text:
        return "Заказ временно не может быть выполнен. Обратитесь к продавцу."
    return "Не удалось выполнить заказ. Продавец уведомлён."


def _status_label(status: Any) -> str:
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


def _sanitize_buyer_text(text: str) -> str:
    for token in ("vexboost", "VexBoost", "socpanel", "SocPanel"):
        text = text.replace(token, "")
    return text.strip()


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _default_stats() -> dict[str, Any]:
    return {
        "total": {
            "created": 0, "completed": 0, "canceled": 0,
            "failed": 0, "refunded": 0,
            "revenue": 0.0, "cost": 0.0, "profit": 0.0,
        },
        "daily": {},
        "by_service": {},
    }


class OrderValidator:
    @staticmethod
    def validate(link: str) -> tuple[bool, str]:
        if not link or not link.startswith(("http://", "https://")):
            return False, "Ссылка должна начинаться с http:// или https://"
        try:
            host = urlparse(link).netloc.lower().replace("www.", "")
        except Exception:
            return False, "Некорректный URL"
        if not host:
            return False, "Не удалось определить домен ссылки"
        if any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS):
            return True, ""
        return False, f"Домен {host} не поддерживается для накрутки"


class StatisticsManager:
    @staticmethod
    def _ensure_daily(stats: dict, day: str) -> dict:
        if day not in stats["daily"]:
            stats["daily"][day] = {
                "created": 0, "completed": 0, "canceled": 0,
                "failed": 0, "refunded": 0,
                "revenue": 0.0, "cost": 0.0, "profit": 0.0,
            }
        return stats["daily"][day]

    @staticmethod
    def _ensure_service(stats: dict, service_id: int) -> dict:
        key = str(service_id)
        if key not in stats["by_service"]:
            stats["by_service"][key] = {
                "count": 0, "completed": 0,
                "revenue": 0.0, "cost": 0.0, "profit": 0.0,
            }
        return stats["by_service"][key]

    @classmethod
    def record_created(cls, service_id: int, revenue: float) -> None:
        stats = _load_json("stats.json", _default_stats())
        day = _today_key()
        cls._ensure_daily(stats, day)
        cls._ensure_service(stats, service_id)
        stats["total"]["created"] += 1
        stats["daily"][day]["created"] += 1
        stats["by_service"][str(service_id)]["count"] += 1
        stats["total"]["revenue"] += revenue
        stats["daily"][day]["revenue"] += revenue
        stats["by_service"][str(service_id)]["revenue"] += revenue
        _save_json("stats.json", stats)

    @classmethod
    def record_completed(cls, service_id: int, revenue: float, cost: float) -> float:
        stats = _load_json("stats.json", _default_stats())
        day = _today_key()
        daily = cls._ensure_daily(stats, day)
        svc = cls._ensure_service(stats, service_id)
        profit = revenue - cost
        stats["total"]["completed"] += 1
        stats["total"]["cost"] += cost
        stats["total"]["profit"] += profit
        daily["completed"] += 1
        daily["cost"] += cost
        daily["profit"] += profit
        svc["completed"] += 1
        svc["cost"] += cost
        svc["profit"] += profit
        _save_json("stats.json", stats)
        return profit

    @classmethod
    def record_canceled(cls, refunded: bool = False) -> None:
        stats = _load_json("stats.json", _default_stats())
        day = _today_key()
        daily = cls._ensure_daily(stats, day)
        stats["total"]["canceled"] += 1
        daily["canceled"] += 1
        if refunded:
            stats["total"]["refunded"] += 1
            daily["refunded"] += 1
        _save_json("stats.json", stats)

    @classmethod
    def record_failed(cls) -> None:
        stats = _load_json("stats.json", _default_stats())
        day = _today_key()
        daily = cls._ensure_daily(stats, day)
        stats["total"]["failed"] += 1
        daily["failed"] += 1
        _save_json("stats.json", stats)

    @classmethod
    def period_stats(cls, days: int = 0) -> dict[str, Any]:
        stats = _load_json("stats.json", _default_stats())
        if days == 0:
            return dict(stats["total"])
        result = {
            "created": 0, "completed": 0, "canceled": 0,
            "failed": 0, "refunded": 0,
            "revenue": 0.0, "cost": 0.0, "profit": 0.0,
        }
        cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        for day, data in stats.get("daily", {}).items():
            if day >= cutoff:
                for key in result:
                    result[key] += data.get(key, 0)
        return result


class OrderHistory:
    @staticmethod
    def add(entry: dict) -> None:
        history = _load_json("history.json", [])
        entry["archived_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        history.append(entry)
        if len(history) > 5000:
            history = history[-5000:]
        _save_json("history.json", history)

    @staticmethod
    def recent(limit: int = 10) -> list[dict]:
        history = _load_json("history.json", [])
        return list(reversed(history[-limit:]))


# ── VexBoost API (async, 3 режима) ───────────────────────────────────────────

class VexBoostAPI:
    ERROR_MESSAGES = {
        "user_inactive": "API-ключ неактивен",
        "incorrect api key": "Неверный API KEY",
        "invalid api key": "Неверный API KEY",
        "not enough funds": "Недостаточно средств на балансе",
        "incorrect service id": "Неверный ID услуги",
        "invalid link": "Некорректная ссылка",
        "quantity out of range": "Количество вне диапазона",
    }

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._session: httpx.AsyncClient | None = None
        self._session_expires = 0.0

    def _mode(self) -> str:
        mode = str(self.cfg.get("auth_mode", "api_key")).strip().lower()
        return mode if mode in ("login", "token", "api_key") else "api_key"

    def _panel_url(self) -> str:
        url = str(self.cfg.get("panel_url") or self.cfg.get("api_url") or "https://vexboost.ru")
        url = url.rstrip("/")
        if url.endswith("/api/v2"):
            url = url[:-7]
        return url

    def _api_url(self) -> str:
        return str(self.cfg.get("api_url", "https://vexboost.ru/api/v2")).rstrip("/")

    def _api_key(self) -> str:
        return str(self.cfg.get("api_key", "")).strip()

    def _format_error(self, error: Any) -> str:
        text = str(error or "").strip()
        return self.ERROR_MESSAGES.get(text.lower(), text)

    async def _get_session(self, force: bool = False) -> tuple[httpx.AsyncClient | None, str]:
        mode = self._mode()
        if mode == "api_key":
            return None, ""

        now = time.time()
        if not force and self._session and self._session_expires > now:
            return self._session, ""

        if mode == "token":
            token = str(self.cfg.get("auth_token", "")).strip()
            if "=" in token and token.lower().startswith(("socpanel_session=", "authtoken=")):
                token = token.split("=", 1)[1].strip()
            token = token.strip('"').strip("'")
            if not token:
                return None, "AuthToken не задан"
            cookie_name = str(self.cfg.get("cookie_name", "socpanel_session"))
            client = httpx.AsyncClient(
                timeout=45.0,
                headers={"User-Agent": "VexBoostAutoSMM/3.1", "Accept": "application/json"},
            )
            host = urlparse(self._panel_url()).netloc
            client.cookies.set(cookie_name, unquote(token), domain=host)
            try:
                await client.get(f"{self._panel_url()}/api/csrf-cookie")
                xsrf = client.cookies.get("XSRF-TOKEN")
                if xsrf:
                    client.headers["X-XSRF-TOKEN"] = unquote(xsrf)
            except Exception as exc:
                await client.aclose()
                return None, f"Ошибка CSRF: {exc}"
            self._session = client
            self._session_expires = now + int(self.cfg.get("session_ttl", 5400))
            return client, ""

        login = str(self.cfg.get("vexboost_login", "")).strip()
        password = str(self.cfg.get("vexboost_password", "")).strip()
        if not login or not password:
            return None, "Логин/пароль не заданы"
        client = httpx.AsyncClient(
            timeout=45.0,
            headers={"User-Agent": "VexBoostAutoSMM/3.1", "Accept": "application/json"},
        )
        try:
            await client.get(f"{self._panel_url()}/api/csrf-cookie")
            xsrf = client.cookies.get("XSRF-TOKEN")
            if xsrf:
                client.headers["X-XSRF-TOKEN"] = unquote(xsrf)
            resp = await client.post(
                f"{self._panel_url()}/api/login",
                json={"login": login, "password": password},
            )
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400 or data.get("error"):
                await client.aclose()
                return None, self._format_error(data.get("error", f"HTTP {resp.status_code}"))
        except Exception as exc:
            await client.aclose()
            return None, f"Ошибка входа: {exc}"
        self._session = client
        self._session_expires = now + max(600, int(self.cfg.get("session_ttl", 5400)))
        return client, ""

    async def _request_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = self._api_key()
        if not key:
            return {"error": "API KEY не задан"}
        data = {**payload, "key": key}
        action = payload.get("action", "")

        # add/refill/cancel — один POST, без retry (иначе дубли заказов)
        if action in ("add", "refill", "cancel"):
            try:
                async with httpx.AsyncClient(timeout=45.0) as client:
                    resp = await client.post(self._api_url(), data=data)
                    result = resp.json()
            except Exception as exc:
                return {"error": str(exc)}
            if isinstance(result, dict) and result.get("error"):
                result["error"] = self._format_error(result["error"])
            return result if isinstance(result, dict) else {"raw": result}

        retries = int(self.cfg.get("api_retry_count", 3))
        delay = int(self.cfg.get("api_retry_delay", 2))
        last_err = "Нет ответа"
        for attempt in range(1, retries + 1):
            for method, url, body in (
                ("POST", self._api_url(), data),
                ("GET", f"{self._api_url()}?key={key}&action={action}", None),
            ):
                try:
                    async with httpx.AsyncClient(timeout=45.0) as client:
                        if method == "POST":
                            resp = await client.post(url, data=body)
                        else:
                            params = {k: v for k, v in data.items()}
                            resp = await client.get(self._api_url(), params=params)
                        result = resp.json()
                except Exception as exc:
                    last_err = str(exc)
                    continue
                if isinstance(result, dict):
                    if result.get("error"):
                        result["error"] = self._format_error(result["error"])
                    return result
            if attempt < retries:
                await asyncio.sleep(delay * attempt)
        return {"error": last_err}

    async def _request_token(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(2):
            session, err = await self._get_session(force=attempt > 0)
            if not session:
                return {"error": err}
            api_path = path.lstrip("/")
            if not api_path.startswith("api/"):
                api_path = f"api/{api_path}"
            url = f"{self._panel_url()}/{api_path}"
            try:
                resp = await session.request(method.upper(), url, **kwargs)
                if resp.status_code in (401, 419) and attempt == 0:
                    self._session = None
                    self._session_expires = 0
                    continue
                return resp.json() if resp.content else {}
            except Exception as exc:
                return {"error": str(exc)}
        return {"error": "Сессия VexBoost истекла"}

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._mode() in ("login", "token"):
            action = payload.get("action", "")
            if action == "balance":
                data = await self._request_token("GET", "user")
                if data.get("error"):
                    return data
                for key in ("balance", "wallet", "funds"):
                    if key in data and data[key] is not None:
                        return {"balance": data[key], "currency": data.get("currency", "RUB")}
                user = data.get("user")
                if isinstance(user, dict) and user.get("balance") is not None:
                    return {"balance": user["balance"], "currency": user.get("currency", "RUB")}
                return {"error": "Баланс не найден"}
            if action == "add":
                body = {
                    "service_id": int(payload["service"]),
                    "link": str(payload["link"]),
                    "quantity": int(payload["quantity"]),
                }
                data = await self._request_token("POST", "orders", json=body)
                if data.get("error"):
                    return data
                for key in ("id", "order_id", "orderId", "order"):
                    val = data.get(key)
                    if isinstance(val, dict):
                        val = val.get("id")
                    if val is not None and str(val).isdigit():
                        return {"order": int(val)}
                return {"error": "ID заказа не найден в ответе"}
            if action == "status":
                oid = int(payload["order"])
                data = await self._request_token("GET", f"orders/{oid}")
                if data.get("error"):
                    return data
                order = data.get("order") if isinstance(data.get("order"), dict) else data
                return {
                    "status": order.get("status", "Unknown"),
                    "remains": order.get("remains", order.get("remainder", 0)),
                    "start_count": order.get("start_count", 0),
                    "charge": order.get("charge", order.get("cost", 0)),
                    "currency": order.get("currency", "RUB"),
                }
            if action == "refill":
                data = await self._request_token("POST", f"orders/{int(payload['order'])}/refill")
                return {"refill": data.get("refill") or data.get("id") or True} if not data.get("error") else data
            if action == "cancel":
                data = await self._request_token("DELETE", f"orders/{int(payload['order'])}")
                return {"cancel": True} if not data.get("error") else data
            return {"error": f"Неизвестное действие: {action}"}
        return await self._request_key(payload)

    async def balance(self) -> tuple[float | None, str, str]:
        data = await self._request({"action": "balance"})
        if data.get("error"):
            return None, "", str(data["error"])
        raw = str(data.get("balance", "0"))
        m = re.search(r"[\d.]+", raw)
        if not m:
            return None, "", "Неверный ответ баланса"
        return float(m.group()), str(data.get("currency", "RUB")), ""

    async def create_order(self, service_id: int, link: str, quantity: int) -> int | str:
        data = await self._request({
            "action": "add", "service": service_id, "link": link, "quantity": quantity,
        })
        if "order" in data:
            return int(data["order"])
        return self._format_error(data.get("error", "Неизвестная ошибка"))

    async def status(self, order_id: int) -> dict[str, Any] | None:
        data = await self._request({"action": "status", "order": order_id})
        if data.get("error"):
            return None
        return data

    async def refill(self, order_id: int) -> bool:
        data = await self._request({"action": "refill", "order": order_id})
        return "refill" in data

    async def close(self) -> None:
        if self._session:
            await self._session.aclose()
            self._session = None


# ── Плагин ────────────────────────────────────────────────────────────────────

class Plugin(StarvellPlugin):
    NAME = NAME
    UUID = UUID
    VERSION = VERSION
    DESCRIPTION = DESCRIPTION
    CREDITS = CREDITS
    SETTINGS_PAGE = True
    TELEGRAM_COMMANDS = TELEGRAM_COMMANDS

    def __init__(self, core, config=None):
        super().__init__(core, config)
        self._status_task: asyncio.Task | None = None
        self._locks: set[str] = set()
        self._pending: dict[str, dict] = {}
        self._api_client: VexBoostAPI | None = None

    async def on_startup(self) -> None:
        self._pending = _load_json("pending.json", {})
        self._status_task = asyncio.create_task(self._status_loop())
        if await self.get_cfg("notify_startup", True):
            await self.core.notify(
                f"✅ <b>{NAME} v{VERSION}</b> запущен\n"
                f"⚙️ /vexboost · 📊 /vb_stats · 💰 /vb_balance",
                "notify_orders",
            )
        self.log("VexBoost AutoSMM v%s запущен", VERSION)

    async def on_shutdown(self) -> None:
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass
        if self._api_client:
            await self._api_client.close()

    def get_settings_schema(self) -> list[dict]:
        return [
            {"key": "enabled", "label": "Авто-SMM", "type": "bool", "default": True},
            {"key": "auth_mode", "label": "Режим API", "type": "select", "default": "api_key",
             "options": ["api_key", "login", "token"]},
            {"key": "panel_url", "label": "URL панели", "type": "text", "default": "https://vexboost.ru"},
            {"key": "vexboost_login", "label": "Логин VexBoost", "type": "text", "default": ""},
            {"key": "vexboost_password", "label": "Пароль VexBoost", "type": "text", "default": ""},
            {"key": "auth_token", "label": "AuthToken", "type": "text", "default": ""},
            {"key": "api_url", "label": "API URL (key)", "type": "text", "default": "https://vexboost.ru/api/v2"},
            {"key": "api_key", "label": "API Key", "type": "text", "default": ""},
            {"key": "status_interval", "label": "Интервал проверки (сек)", "type": "int", "default": 60, "min": 30, "max": 600},
            {"key": "auto_refund_on_error", "label": "Авто-возврат при ошибке", "type": "bool", "default": True},
            {"key": "auto_refund_on_cancel", "label": "Авто-возврат при отмене SMM", "type": "bool", "default": True},
            {"key": "allow_private_tg", "label": "Приватные TG-ссылки", "type": "bool", "default": False},
            {"key": "recreate_partial", "label": "Пересоздавать Partial-заказы", "type": "bool", "default": False},
            {"key": "commission_percent", "label": "Комиссия (%)", "type": "int", "default": 6, "min": 0, "max": 50},
            {"key": "notify_admin", "label": "Уведомления в TG", "type": "bool", "default": True},
            {"key": "notify_startup", "label": "Уведомление при старте", "type": "bool", "default": True},
            {"key": "notify_new_order", "label": "Уведомление о новом заказе", "type": "bool", "default": True},
            {"key": "notify_complete", "label": "Уведомление о выполнении", "type": "bool", "default": True},
            {"key": "low_balance_threshold", "label": "Порог низкого баланса", "type": "int", "default": 50, "min": 0, "max": 100000},
            {"key": "notify_low_balance", "label": "Уведомление о низком балансе", "type": "bool", "default": True},
            {"key": "welcome_message", "label": "Шаблон: приветствие", "type": "multiline",
             "default": DEFAULT_TEMPLATES["welcome_message"]},
            {"key": "confirmation_message", "label": "Шаблон: подтверждение", "type": "multiline",
             "default": DEFAULT_TEMPLATES["confirmation_message"]},
            {"key": "completion_message", "label": "Шаблон: выполнение", "type": "multiline",
             "default": DEFAULT_TEMPLATES["completion_message"]},
        ]

    async def _cfg_all(self) -> dict[str, Any]:
        schema = {s["key"]: s.get("default") for s in self.get_settings_schema()}
        for key in schema:
            schema[key] = await self.get_cfg(key, schema[key])
        for key, val in DEFAULT_TEMPLATES.items():
            schema.setdefault(key, val)
            if key not in {s["key"] for s in self.get_settings_schema()}:
                schema[key] = await self.get_cfg(key, val)
        return schema

    async def _api(self) -> VexBoostAPI | None:
        cfg = await self._cfg_all()
        mode = str(cfg.get("auth_mode", "api_key"))
        if mode == "api_key" and not str(cfg.get("api_key", "")).strip():
            return None
        if mode == "login" and not (cfg.get("vexboost_login") and cfg.get("vexboost_password")):
            return None
        if mode == "token" and not str(cfg.get("auth_token", "")).strip():
            return None
        if self._api_client is None:
            self._api_client = VexBoostAPI(cfg)
        else:
            self._api_client.cfg = cfg
        return self._api_client

    async def on_setting_change(self, key: str, value: Any) -> None:
        if key in ("auth_mode", "api_key", "auth_token", "vexboost_login", "vexboost_password", "panel_url", "api_url"):
            if self._api_client:
                await self._api_client.close()
            self._api_client = None

    async def _starvell_send(self, chat_id: str, text: str) -> None:
        api = self.core.get_api()
        if not api or not chat_id:
            return
        await _starvell_pause(self.core)
        await api.send_message(chat_id, text)

    async def _check_low_balance(self, ctx: OrderContext, api: VexBoostAPI) -> None:
        if not await self.get_cfg("notify_low_balance", True):
            return
        threshold = int(await self.get_cfg("low_balance_threshold", 50))
        bal, cur, err = await api.balance()
        if bal is None or bal >= threshold:
            return
        await ctx.notify(
            f"⚠️ <b>VexBoost — низкий баланс</b>\n"
            f"💰 {bal:.2f} {cur} (порог {threshold})\n"
            f"📇 Заказ #{ctx.order_id}",
            "notify_orders",
            order_id=ctx.order_id,
        )

    async def _enabled(self) -> bool:
        return bool(await self.get_cfg("enabled", True))

    async def _msg(self, key: str, **kwargs) -> str:
        default = DEFAULT_TEMPLATES.get(key, "")
        tpl = await self.get_cfg(key, default)
        try:
            return _sanitize_buyer_text(str(tpl).format(**kwargs))
        except Exception:
            return _sanitize_buyer_text(str(tpl))

    def _parse_lot(self, order: dict) -> tuple[int, int] | None:
        desc = _order_description(order)
        m = SERVICE_ID_RE.search(desc)
        if not m:
            return None
        service_id = int(m.group(1))
        mult = 1
        qm = QUAN_RE.search(desc)
        if qm:
            mult = max(1, int(qm.group(1)))
        qty = max(1, int(order.get("quantity") or 1)) * mult
        return service_id, qty

    # ── @on_pre_delivery — отмена автовыдачи для SMM-лотов ─────────────────

    @on_pre_delivery
    async def block_autodelivery(self, ctx: DeliveryContext) -> None:
        if self._parse_lot(ctx.order):
            ctx.cancel()
            self.log("Автовыдача отменена для SMM-заказа #%s", ctx.order_id)

    # ── @on_order_paid — новый заказ (НЕ сообщение) ─────────────────────────

    @on_order_paid
    async def on_new_order(self, ctx: OrderContext) -> None:
        if not await self._enabled():
            return
        parsed = self._parse_lot(ctx.order)
        if not parsed:
            return
        service_id, quantity = parsed
        if not await self._api():
            self.log("VexBoost не настроен", level="warning")
            return

        order_id = ctx.order_id
        price_rub = _resolve_order_price(ctx.order)

        submitted = _load_json("submitted.json", {})
        if order_id in submitted:
            return
        waiting = _load_json("waiting.json", [])
        if any(str(o.get("order_id")) == order_id for o in waiting):
            return

        entry = {
            "order_id": order_id,
            "service_id": service_id,
            "quantity": quantity,
            "buyer": ctx.buyer_username,
            "buyer_id": ctx.buyer_id,
            "chat_id": ctx.chat_id or "",
            "product": ctx.product_name,
            "price_rub": price_rub,
            "created_at": int(time.time()),
        }
        if not entry["chat_id"] and ctx.buyer_id:
            api = ctx.api()
            if api:
                entry["chat_id"] = await api.find_chat_by_buyer(int(ctx.buyer_id)) or ""

        waiting.append(entry)
        _save_json("waiting.json", waiting)
        StatisticsManager.record_created(service_id, float(price_rub))

        welcome = await self._msg("welcome_message", order_id=order_id)
        if entry["chat_id"]:
            await ctx.send_to_buyer(welcome)
        else:
            await ctx.notify(f"⚠️ VexBoost #{order_id}: чат не найден", "notify_orders")

        if await self.get_cfg("notify_admin", True) and await self.get_cfg("notify_new_order", True):
            api = await self._api()
            bal_txt = "н/д"
            if api:
                bal, cur, err = await api.balance()
                bal_txt = f"{bal:.2f} {cur}" if bal is not None else err
                await self._check_low_balance(ctx, api)
            await ctx.notify(
                f"📥 <b>VexBoost — новый заказ</b>\n\n"
                f"🛒 {ctx.product_name}\n"
                f"🙍 {ctx.buyer_username}\n"
                f"💵 Сумма: <b>{_format_rub(price_rub)}</b>\n"
                f"🔍 Service ID: <code>{service_id}</code>\n"
                f"🔢 Кол-во: <b>{quantity}</b>\n"
                f"💰 Баланс VexBoost: {bal_txt}\n"
                f"📇 Starvell: <code>#{order_id}</code>",
                "notify_orders",
                order_id=order_id,
            )
        self.log("Новый заказ #%s service=%s qty=%s price=%s", order_id, service_id, quantity, price_rub)

    # ── @on_message — только чат покупателя ────────────────────────────────

    @on_message
    async def on_buyer_chat(self, ctx: MessageContext) -> None:
        if not await self._enabled():
            return
        text = (ctx.text or "").strip()
        if not text:
            return

        if _dedup.is_duplicate(ctx.chat_id, ctx.message_id, text):
            ctx.mark_handled()
            return

        low = text.lower()
        if "вернул" in low and ("деньг" in low or "средств" in low):
            self._clear_buyer_state(ctx.username, ctx.chat_id, ctx.author_id)
            ctx.mark_handled()
            return

        if low.startswith("#статус"):
            await self._cmd_status(ctx, text)
            ctx.mark_handled()
            return
        if low.startswith("#рефилл") or low.startswith("#refill"):
            await self._cmd_refill(ctx, text)
            ctx.mark_handled()
            return

        action = _confirm_action(text)
        pkey = self._pending_key(ctx)
        if action or pkey in self._pending:
            await self._handle_pending(ctx, text, action)
            ctx.mark_handled()
            return

        waiting = _load_json("waiting.json", [])
        order = self._find_waiting(waiting, ctx)
        if not order:
            return

        links = _extract_links(text)
        if not links:
            return
        await self._request_link_confirm(ctx, order, links[0])
        ctx.mark_handled()

    def _pending_key(self, ctx: MessageContext) -> str:
        uid = ctx.author_id or ctx.username or ctx.chat_id
        return f"{ctx.chat_id}:{uid}"

    def _find_waiting(self, waiting: list, ctx: MessageContext) -> dict | None:
        author_id = ctx.author_id
        username = (ctx.username or "").strip()
        chat_id = str(ctx.chat_id)
        for o in waiting:
            if author_id and o.get("buyer_id") == author_id:
                return o
        if username:
            for o in waiting:
                if (o.get("buyer") or "").lower() == username.lower():
                    return o
        for o in waiting:
            if str(o.get("chat_id")) == chat_id:
                return o
        return None

    async def _request_link_confirm(self, ctx: MessageContext, order: dict, link: str) -> None:
        allow_private = await self.get_cfg("allow_private_tg", False)
        if not allow_private and _is_private_tg(link):
            await ctx.reply(await self._msg("private_telegram_message"))
            return
        ok, err = OrderValidator.validate(link)
        if not ok:
            await ctx.reply(await self._msg("invalid_link_message", error=err))
            return

        order["link"] = link
        order["chat_id"] = ctx.chat_id
        pkey = self._pending_key(ctx)
        self._pending[pkey] = order
        _save_json("pending.json", self._pending)

        display_link = link.replace("https://", "").replace("http://", "")
        await ctx.reply(await self._msg(
            "confirmation_message",
            lot=order.get("product", "товар"),
            price=_format_rub(order.get("price_rub", 0)),
            amount=order.get("quantity", 1),
            link=display_link,
        ))

    async def _handle_pending(self, ctx: MessageContext, text: str, action: str | None) -> None:
        pkey = self._pending_key(ctx)
        order = self._pending.get(pkey)
        if not order:
            for k, v in list(self._pending.items()):
                if v.get("buyer") == ctx.username:
                    pkey, order = k, v
                    break
        if not order:
            return

        action = action or _confirm_action(text)
        if action == "+":
            if not order.get("link"):
                await ctx.reply(await self._msg("send_link_first_message"))
                return
            self._pending.pop(pkey, None)
            _save_json("pending.json", self._pending)
            await self._submit_order(ctx, order)
            return

        if action == "-":
            self._pending.pop(pkey, None)
            _save_json("pending.json", self._pending)
            self._remove_waiting(order.get("order_id"))
            await self._refund_starvell(ctx, str(order.get("order_id", "")))
            await ctx.reply(await self._msg("order_cancelled_message"))
            StatisticsManager.record_canceled(refunded=True)
            return

        links = _extract_links(text)
        if links:
            await self._request_link_confirm(ctx, order, links[0])
            return
        await ctx.reply(await self._msg("pending_hint_message"))

    async def _submit_order(self, ctx: MessageContext, order: dict) -> None:
        oid = str(order.get("order_id", ""))
        if oid in self._locks:
            return
        self._locks.add(oid)

        api = await self._api()
        if not api:
            await ctx.reply("❌ API не настроен. Обратитесь к продавцу.")
            self._locks.discard(oid)
            return

        submitted = _load_json("submitted.json", {})
        if oid in submitted:
            await ctx.reply(f"ℹ️ Заказ уже создан: SMM #{submitted[oid]}")
            self._locks.discard(oid)
            return

        await ctx.reply(await self._msg("creating_order_message"))

        result = await api.create_order(
            int(order["service_id"]), str(order["link"]), int(order["quantity"]),
        )

        if isinstance(result, int):
            smm_id = result
            submitted[oid] = smm_id
            _save_json("submitted.json", submitted)

            status_data = await api.status(smm_id) or {}
            cost = float(status_data.get("charge", 0) or 0)

            active = _load_json("active.json", {})
            active[str(smm_id)] = {
                **order,
                "smm_id": smm_id,
                "status": "Pending",
                "cost": cost,
                "created_at": int(time.time()),
            }
            _save_json("active.json", active)
            self._remove_waiting(oid)

            await ctx.reply(await self._msg(
                "order_created_message", smm_id=smm_id, order_id=oid,
            ))

            if await self.get_cfg("notify_admin", True) and await self.get_cfg("notify_new_order", True):
                commission = int(await self.get_cfg("commission_percent", 6))
                revenue = float(order.get("price_rub", 0))
                profit = revenue - cost
                profit_net = profit * (1 - commission / 100)
                await ctx.notify(
                    f"✅ <b>SMM заказ создан</b>\n\n"
                    f"📇 Starvell: <code>#{oid}</code>\n"
                    f"🆔 VexBoost: <code>{smm_id}</code>\n"
                    f"💵 Сумма: <b>{_format_rub(revenue)}</b>\n"
                    f"💳 Расход: <b>{cost:.2f}</b>\n"
                    f"💰 Прибыль: <b>{profit:.2f} ₽</b> (с комиссией {commission}%: <b>{profit_net:.2f} ₽</b>)",
                    "notify_orders",
                    order_id=oid,
                )
        else:
            err = _buyer_error(result)
            await ctx.reply(await self._msg("error_message", error=err))
            StatisticsManager.record_failed()
            OrderHistory.add({
                "order_id": oid, "status": "Failed", "error": str(result),
                "buyer": order.get("buyer"), "service_id": order.get("service_id"),
            })
            if await self.get_cfg("notify_error", True):
                await ctx.notify(
                    f"❌ <b>VexBoost ошибка</b>\n📇 #{oid}\n⚠️ <code>{result}</code>",
                    "notify_orders", order_id=oid,
                )
            if await self.get_cfg("auto_refund_on_error", True):
                await self._refund_starvell(ctx, oid)

        self._locks.discard(oid)

    async def _refund_starvell(self, ctx: MessageContext | OrderContext, order_id: str) -> bool:
        if not order_id:
            return False
        api = ctx.api() if hasattr(ctx, "api") else self.core.get_api()
        if not api:
            return False
        try:
            await api.refund_order(order_id)
            self.log("Возврат Starvell #%s", order_id)
            return True
        except Exception as exc:
            self.log("Ошибка возврата #%s: %s", order_id, exc, level="error")
            return False

    def _remove_waiting(self, order_id: str | None) -> None:
        if not order_id:
            return
        waiting = _load_json("waiting.json", [])
        waiting = [o for o in waiting if str(o.get("order_id")) != str(order_id)]
        _save_json("waiting.json", waiting)

    def _clear_buyer_state(self, buyer: str, chat_id: str, buyer_id: int | None = None) -> None:
        waiting = _load_json("waiting.json", [])
        waiting = [
            o for o in waiting
            if o.get("buyer") != buyer
            and str(o.get("chat_id")) != str(chat_id)
            and (buyer_id is None or o.get("buyer_id") != buyer_id)
        ]
        _save_json("waiting.json", waiting)
        self._pending = {
            k: v for k, v in self._pending.items()
            if v.get("buyer") != buyer
            and str(v.get("chat_id")) != str(chat_id)
            and (buyer_id is None or v.get("buyer_id") != buyer_id)
        }
        _save_json("pending.json", self._pending)

    async def _cmd_status(self, ctx: MessageContext, text: str) -> None:
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await ctx.reply(await self._msg("status_usage_message"))
            return
        api = await self._api()
        if not api:
            await ctx.reply("❌ API не настроен")
            return
        smm_id = int(parts[1])
        data = await api.status(smm_id)
        if not data:
            await ctx.reply(await self._msg("status_error_message"))
            return
        start = data.get("start_count", 0)
        display_start = "*" if start in (0, "0") else str(start)
        await ctx.reply(await self._msg(
            "status_message",
            smm_id=smm_id,
            status=_status_label(data.get("status")),
            start_count=display_start,
            remains=data.get("remains", "—"),
        ))

    async def _cmd_refill(self, ctx: MessageContext, text: str) -> None:
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await ctx.reply(await self._msg("refill_usage_message"))
            return
        api = await self._api()
        if not api:
            await ctx.reply("❌ API не настроен")
            return
        ok = await api.refill(int(parts[1]))
        await ctx.reply(
            await self._msg("refill_success_message" if ok else "refill_error_message")
        )

    # ── @on_order_completed — завершение на Starvell ────────────────────────

    @on_order_completed
    async def on_starvell_completed(self, ctx: OrderContext) -> None:
        waiting = _load_json("waiting.json", [])
        waiting = [o for o in waiting if str(o.get("order_id")) != ctx.order_id]
        _save_json("waiting.json", waiting)

    # ── Фоновая проверка SMM-статусов ─────────────────────────────────────

    async def _status_loop(self) -> None:
        while True:
            try:
                interval = int(await self.get_cfg("status_interval", 60))
                await asyncio.sleep(max(30, interval))
                if await self._enabled():
                    await self._check_active_orders()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log("status_loop: %s", exc, level="error")

    async def _check_active_orders(self) -> None:
        api = await self._api()
        if not api:
            return
        active = _load_json("active.json", {})
        if not active:
            return

        recreate = await self.get_cfg("recreate_partial", False)
        auto_refund = await self.get_cfg("auto_refund_on_cancel", True)
        starvell_api = self.core.get_api()

        for smm_id, info in list(active.items()):
            data = await api.status(int(smm_id))
            if not data:
                continue
            status = str(data.get("status", "")).lower()
            info["status"] = status
            active[smm_id] = info

            chat_id = str(info.get("chat_id") or "")
            starvell_id = str(info.get("order_id", ""))
            revenue = float(info.get("price_rub", 0))
            cost = float(data.get("charge", info.get("cost", 0)) or 0)
            service_id = int(info.get("service_id", 0))

            if status == "completed":
                order_url = STV_ORDER_URL.format(order_id=starvell_id)
                msg = await self._msg(
                    "completion_message", order_id=starvell_id, order_url=order_url,
                )
                if chat_id and starvell_api:
                    try:
                        await _starvell_pause(self.core)
                        await starvell_api.send_message(chat_id, msg)
                    except Exception as exc:
                        self.log("notify complete: %s", exc, level="warning")
                profit = StatisticsManager.record_completed(service_id, revenue, cost)
                OrderHistory.add({
                    "order_id": starvell_id, "smm_id": smm_id,
                    "status": "Completed", "profit": profit, "buyer": info.get("buyer"),
                })
                if await self.get_cfg("notify_complete", True):
                    await self.core.notify(
                        f"🎉 <b>VexBoost выполнен</b>\n"
                        f"📇 #{starvell_id} · 🆔 {smm_id}\n"
                        f"💰 Прибыль: <b>{profit:.2f} ₽</b>",
                        "notify_orders",
                    )
                del active[smm_id]

            elif status in ("canceled", "cancelled"):
                if chat_id and starvell_api:
                    try:
                        await _starvell_pause(self.core)
                        await starvell_api.send_message(
                            chat_id,
                            await self._msg("order_canceled_message", order_id=starvell_id),
                        )
                    except Exception:
                        pass
                if auto_refund and starvell_api:
                    try:
                        await _starvell_pause(self.core)
                        await starvell_api.refund_order(starvell_id)
                        StatisticsManager.record_canceled(refunded=True)
                    except Exception as exc:
                        self.log("refund on cancel: %s", exc, level="warning")
                OrderHistory.add({
                    "order_id": starvell_id, "smm_id": smm_id,
                    "status": "Canceled", "buyer": info.get("buyer"),
                })
                del active[smm_id]

            elif status == "partial":
                remains = int(data.get("remains", 0) or 0)
                if recreate and remains > 0 and info.get("link"):
                    result = await api.create_order(service_id, str(info["link"]), remains)
                    if isinstance(result, int):
                        new_id = result
                        active[str(new_id)] = {
                            **info, "smm_id": new_id, "quantity": remains,
                            "status": "Pending", "partial_from": smm_id,
                        }
                        if chat_id and starvell_api:
                            await _starvell_pause(self.core)
                            await starvell_api.send_message(
                                chat_id,
                                await self._msg(
                                    "partial_continued_message",
                                    order_id=starvell_id, partial_amount=remains,
                                ),
                            )
                        del active[smm_id]
                    else:
                        if chat_id and starvell_api:
                            await _starvell_pause(self.core)
                            await starvell_api.send_message(
                                chat_id,
                                await self._msg("partial_paused_message", order_id=starvell_id, remains=remains),
                            )
                elif chat_id and starvell_api and not info.get("partial_notified"):
                    await _starvell_pause(self.core)
                    await starvell_api.send_message(
                        chat_id,
                        await self._msg("partial_paused_message", order_id=starvell_id, remains=remains),
                    )
                    info["partial_notified"] = True
                    active[smm_id] = info

        _save_json("active.json", active)

    # ── Telegram панель ─────────────────────────────────────────────────────

    async def render_plugin_card_extras(self) -> str:
        stats = StatisticsManager.period_stats(0)
        active = _load_json("active.json", {})
        waiting = _load_json("waiting.json", [])
        api = await self._api()
        bal_line = "API: ❌ не настроен"
        if api:
            bal, cur, err = await api.balance()
            bal_line = f"💰 <b>{bal:.2f}</b> {cur}" if bal is not None else f"💰 ⚠️ {err}"
        return (
            f"{bal_line}\n"
            f"📋 Активных: <b>{len(active)}</b> · ⏳ Ожидают: <b>{len(waiting)}</b>\n"
            f"📊 Создано: {stats.get('created', 0)} · ✅ {stats.get('completed', 0)} · "
            f"💵 {_format_rub(stats.get('revenue', 0))}"
        )

    async def render_plugin_panel(self) -> tuple[str, InlineKeyboardMarkup]:
        stats = StatisticsManager.period_stats(0)
        today = StatisticsManager.period_stats(1)
        active = _load_json("active.json", {})
        waiting = _load_json("waiting.json", [])
        enabled = await self._enabled()
        api = await self._api()
        commission = int(await self.get_cfg("commission_percent", 6))

        bal_txt = "—"
        if api:
            bal, cur, err = await api.balance()
            bal_txt = f"{bal:.2f} {cur}" if bal is not None else f"ошибка: {err}"

        profit_net = float(stats.get("profit", 0)) * (1 - commission / 100)

        text = (
            f"📊 <b>{NAME}</b> v{VERSION}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{'🟢' if enabled else '🔴'} {'Включён' if enabled else 'Выключен'}\n"
            f"💰 VexBoost: <code>{bal_txt}</code>\n"
            f"📋 Активные: <b>{len(active)}</b> · ⏳ Ожидают: <b>{len(waiting)}</b>\n\n"
            f"<b>Сегодня:</b> {today.get('created', 0)} заказов · "
            f"{_format_rub(today.get('revenue', 0))}\n"
            f"<b>Всего:</b> ✅ {stats.get('completed', 0)} · "
            f"❌ {stats.get('canceled', 0)} · ⚠️ {stats.get('failed', 0)}\n"
            f"💵 Выручка: <b>{_format_rub(stats.get('revenue', 0))}</b>\n"
            f"💰 Прибыль: <b>{stats.get('profit', 0):.2f} ₽</b> "
            f"(с комиссией: <b>{profit_net:.2f} ₽</b>)\n\n"
            f"<i>Лот: <code>ID: 1234</code> · <code>#Quan: 1</code></i>"
        )
        rows = [
            [self.panel_btn("📊 Статистика", self.UUID, "stats")],
            [self.panel_btn("📋 Активные", self.UUID, "active"),
             self.panel_btn("⏳ Ожидают", self.UUID, "waiting")],
            [self.panel_btn("📜 История", self.UUID, "history"),
             self.panel_btn("🏆 Топ услуг", self.UUID, "top")],
            [self.panel_btn("💰 Баланс", self.UUID, "balance")],
            [self.panel_btn("⚙️ Настройки", self.UUID, "settings")],
            [self.panel_btn("🔄 Обновить", self.UUID, "refresh")],
            [self.panel_back_btn(self.UUID)],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def on_panel_action(self, call, action: str) -> bool:
        if action == "settings":
            from handlers.tg.plugin_settings import _show_settings
            pm = self.core.plugin_manager
            if pm:
                await _show_settings(call, pm, self.UUID)
            return True
        if action == "refresh":
            text, kb = await self.render_plugin_panel()
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            await call.answer("Обновлено")
            return True
        if action == "balance":
            api = await self._api()
            if not api:
                await call.answer("API не настроен", show_alert=True)
                return True
            bal, cur, err = await api.balance()
            msg = f"Баланс: {bal:.2f} {cur}" if bal is not None else f"Ошибка: {err}"
            await call.answer(msg, show_alert=True)
            return True
        if action == "stats":
            commission = int(await self.get_cfg("commission_percent", 6))
            lines = []
            for days, label in ((1, "Сегодня"), (7, "7 дней"), (30, "30 дней"), (0, "Всё время")):
                s = StatisticsManager.period_stats(days)
                p = float(s.get("profit", 0)) * (1 - commission / 100)
                lines.append(
                    f"<b>{label}:</b> {s.get('created', 0)} зак. · "
                    f"{_format_rub(s.get('revenue', 0))} · "
                    f"прибыль {p:.2f} ₽"
                )
            await call.message.answer("\n".join(lines), parse_mode="HTML")
            await call.answer()
            return True
        if action == "active":
            active = _load_json("active.json", {})
            lines = [f"<b>Активные SMM ({len(active)})</b>\n"]
            for sid, info in list(active.items())[:15]:
                lines.append(
                    f"• #{sid} → Starvell #{info.get('order_id')} "
                    f"({_status_label(info.get('status'))}) · "
                    f"{_format_rub(info.get('price_rub', 0))}"
                )
            await call.message.answer("\n".join(lines) or "<i>Пусто</i>", parse_mode="HTML")
            await call.answer()
            return True
        if action == "waiting":
            waiting = _load_json("waiting.json", [])
            lines = [f"<b>Ожидают ссылку ({len(waiting)})</b>\n"]
            for o in waiting[:15]:
                lines.append(
                    f"• #{o.get('order_id')} — {o.get('buyer')} · "
                    f"svc {o.get('service_id')} · {_format_rub(o.get('price_rub', 0))}"
                )
            await call.message.answer("\n".join(lines) or "<i>Пусто</i>", parse_mode="HTML")
            await call.answer()
            return True
        if action == "history":
            recent = OrderHistory.recent(10)
            lines = ["<b>Последние заказы</b>\n"]
            for item in recent:
                icon = {"Completed": "✅", "Canceled": "❌", "Failed": "⚠️"}.get(item.get("status"), "📦")
                lines.append(
                    f"{icon} #{item.get('order_id')} · VB {item.get('smm_id', '—')} · "
                    f"{item.get('status', '?')}"
                )
            await call.message.answer("\n".join(lines) or "<i>Пусто</i>", parse_mode="HTML")
            await call.answer()
            return True
        if action == "top":
            stats = _load_json("stats.json", _default_stats())
            services = stats.get("by_service", {})
            if not services:
                await call.message.answer("🏆 Нет данных по услугам")
            else:
                sorted_svc = sorted(services.items(), key=lambda x: x[1].get("profit", 0), reverse=True)[:5]
                lines = ["🏆 <b>Топ услуг по прибыли</b>\n"]
                for i, (sid, d) in enumerate(sorted_svc, 1):
                    lines.append(f"{i}. ID <code>{sid}</code> — ✅ {d.get('completed', 0)} · {d.get('profit', 0):.2f} ₽")
                await call.message.answer("\n".join(lines), parse_mode="HTML")
            await call.answer()
            return True
        return False

    async def on_telegram_command(self, call, command: str) -> bool:
        if command == "vexboost":
            text, kb = await self.render_plugin_panel()
            try:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
            return True
        if command == "vb_balance":
            api = await self._api()
            if not api:
                await call.message.answer("❌ API не настроен")
                return True
            bal, cur, err = await api.balance()
            if bal is not None:
                await call.message.answer(f"💰 Баланс VexBoost: <b>{bal:.2f}</b> {cur}", parse_mode="HTML")
            else:
                await call.message.answer(f"❌ {err}")
            return True
        if command == "vb_stats":
            commission = int(await self.get_cfg("commission_percent", 6))
            s = StatisticsManager.period_stats(0)
            p = float(s.get("profit", 0)) * (1 - commission / 100)
            await call.message.answer(
                f"📊 <b>Статистика VexBoost</b>\n\n"
                f"📦 Создано: {s.get('created', 0)}\n"
                f"✅ Выполнено: {s.get('completed', 0)}\n"
                f"❌ Отменено: {s.get('canceled', 0)}\n"
                f"⚠️ Ошибок: {s.get('failed', 0)}\n"
                f"💸 Возвратов: {s.get('refunded', 0)}\n\n"
                f"💵 Выручка: <b>{_format_rub(s.get('revenue', 0))}</b>\n"
                f"💳 Расход: <b>{s.get('cost', 0):.2f}</b>\n"
                f"💰 Прибыль: <b>{s.get('profit', 0):.2f} ₽</b>\n"
                f"💰 С комиссией {commission}%: <b>{p:.2f} ₽</b>",
                parse_mode="HTML",
            )
            return True
        return False
