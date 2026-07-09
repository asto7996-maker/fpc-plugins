"""
FTS-Starwell — асинхронный плагин автопродажи Telegram Stars через Fragment API для Starvell Cardinal.

Порт логики FTS-Plugin (FunPay Cardinal) на нативную архитектуру Starvell:
  @on_order_paid  — новый оплаченный заказ
  @on_message     — @username, +/-, !бэк
  @on_pre_delivery — отмена автовыдачи Starvell для Stars-лотов
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import json
import logging
import os
import re
import time
import traceback
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import BASE_DIR
from starvell_api import StarvellAPI, StarvellAPIError
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

# ═══════════════════════════════════════════════════════════════════════════════
#  Метаданные (защищённые константы — не редактировать в runtime)
# ═══════════════════════════════════════════════════════════════════════════════

NAME: Final[str] = "FTS-Starwell"
VERSION: Final[str] = "1.1.0"
DESCRIPTION: Final[str] = "Автопродажа Telegram Stars через Fragment API для Starvell"
UUID: Final[str] = "FTS-Starwell"
SETTINGS_PAGE: Final[bool] = True

_META_AUTHOR: Final[str] = "@xvimp"
_META_UUID: Final[str] = "FTS-Starwell"
_META_VERSION: Final[str] = VERSION
CREDITS: Final[str] = _META_AUTHOR

FRAGMENT_API_BASE: Final[str] = "https://api.fragment-api.com"
FTS_AUTODUMP_STEP_RUB: Final[float] = 0.01
LITESERVER_RETRY: Final[int] = 3
STV_ORDER_URL: Final[str] = "https://starvell.com/order/{order_id}"
CB_PREFIX: Final[str] = f"fts_{UUID[:8]}"

TELEGRAM_COMMANDS = [
    {"command": "fts", "description": "панель FTS-Starwell"},
    {"command": "fts_stats", "description": "статистика FTS-Starwell"},
    {"command": "fts_balance", "description": "баланс Fragment кошелька"},
]

QUEUE_MODES = ("strict", "skip_ready", "timeout_end")
QUEUE_MODE_LABELS = {
    "strict": "Строгая (по порядку)",
    "skip_ready": "Пропуск готовых",
    "timeout_end": "Таймаут в конец",
}

# ── Регулярные выражения (из FTS-Plugin) ─────────────────────────────────────

INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
USERNAME_RE = re.compile(
    r"(?<![\w.])(?:@|https?://(?:www\.)?t\.me/|t\.me/)([a-zA-Z][a-zA-Z0-9_]{3,31})\b",
    re.IGNORECASE,
)
STARS_AMOUNT_RE = re.compile(
    r"(\d[\d\s\u00a0.,]*)\s*(?:stars?|зв[ёе]зд|⭐|🌟)",
    re.IGNORECASE,
)
NUMBER_TOKEN_RE = re.compile(r"[\d\s\u00a0.,]+")
BACK_CMD_RE = re.compile(r"^!\s*б[еe]к\b", re.IGNORECASE)
CONFIRM_PLUS = frozenset({"+", "➕", "✅", "yes", "да", "ok", "подтверждаю"})
CONFIRM_MINUS = frozenset({"-", "➖", "❌", "no", "нет", "отмена"})

FRAGMENT_PENDING = frozenset({"PENDING", "pending", "PROCESSING", "processing", "QUEUED", "queued"})
FRAGMENT_SENT = frozenset({"BLOCKCHAIN_SENT", "blockchain_sent", "SENT", "sent"})
FRAGMENT_OK = frozenset({"OK", "ok", "COMPLETED", "completed", "SUCCESS", "success", "DONE", "done"})
LITESERVER_MARKERS = ("liteserver", "lite server", "lite-server", "LITE_SERVER")

DEFAULT_TEMPLATES: dict[str, str] = {
    "welcome": (
        "⭐ Спасибо за покупку Telegram Stars!\n\n"
        "Отправьте @username получателя (или подтвердите ник из заказа)."
    ),
    "ask_username": "📨 Укажите @username получателя Stars:",
    "confirm_username": (
        "📋 Подтвердите получателя:\n\n"
        "👤 @{username}\n"
        "⭐ Количество: {stars}\n"
        "💵 Сумма: {price}\n\n"
        "✅ + — отправить\n"
        "❌ - — отмена и возврат"
    ),
    "sending": "⏳ Отправляю {stars} Stars на @{username}...",
    "success": (
        "✅ {stars} Stars успешно отправлены на @{username}!\n\n"
        "Подтвердите выполнение заказа на Starvell:\n"
        "🔗 {order_url}\n\n"
        "Спасибо за покупку! 🙏"
    ),
    "error": "❌ Не удалось отправить Stars: {reason}",
    "refunded": "💸 Средства по заказу #{order_id} возвращены.",
    "queue_position": "📋 Вы в очереди на позиции {pos}. Ожидайте, пожалуйста.",
    "timeout_moved": "⏱ Вы не указали @username вовремя — заказ перенесён в конец очереди.",
    "invalid_username": "❌ Ник @{username} не найден или недоступен для Stars.",
    "back_ok": "💸 Возврат по команде !бэк выполнен.",
    "back_denied": "❌ Возврат недоступен — Stars уже отправлены или заказ закрыт.",
    "reminder": "🔔 Напоминание: отправьте @username для получения {stars} Stars.",
    "review_request": "⭐ Будем благодарны за отзыв после получения Stars!",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "plugin_enabled": True,
    "lots_enabled": True,
    "auto_refund": True,
    "auto_deactivate": True,
    "back_command_enabled": True,
    "back_priority": False,
    "preorder_username": True,
    "auto_send_without_plus": False,
    "check_username": True,
    "liteserver_retry": True,
    "usdt_fallback_to_ton": True,
    "queue_mode": "strict",
    "queue_timeout_sec": 300,
    "min_balance_ton": 0.5,
    "min_balance_usdt": 1.0,
    "markup_percent": 15.0,
    "autoprice_enabled": False,
    "autoprice_interval_sec": 600,
    "autodump_enabled": False,
    "autodump_interval_sec": 900,
    "autodump_min_price": 0.5,
    "balance_check_interval_sec": 120,
    "api_token": "",
    "category_url": "",
    "templates": copy.deepcopy(DEFAULT_TEMPLATES),
    "lots": {},
    "stats": {
        "total_orders": 0,
        "completed": 0,
        "failed": 0,
        "refunded": 0,
        "stars_sent": 0,
        "revenue_rub": 0.0,
        "cost_ton": 0.0,
        "errors": {},
        "buyers": {},
    },
}


def _meta_guard() -> None:
    """Проверка целостности метаданных плагина."""
    digest = hashlib.sha256(f"{_META_AUTHOR}:{_META_UUID}:{_META_VERSION}".encode()).hexdigest()[:16]
    if digest != hashlib.sha256(f"{CREDITS}:{UUID}:{VERSION}".encode()).hexdigest()[:16]:
        raise RuntimeError(_tamper_text())


def _tamper_text() -> str:
    return (
        "⚠️ Метаданные плагина изменены. Восстановите оригинальные CREDITS/UUID/VERSION "
        f"или обратитесь к автору {_META_AUTHOR}."
    )


_meta_guard()

# ═══════════════════════════════════════════════════════════════════════════════
#  Человекочитаемое логирование → log.txt
# ═══════════════════════════════════════════════════════════════════════════════


class _HumanFormatter(logging.Formatter):
    """Форматирование логов для log.txt (как в FTS-Plugin)."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%d.%m.%Y %H:%M:%S")
        level = record.levelname.ljust(5)
        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = f"{msg}\n{record.exc_text}"
        return f"[{ts}] {level} | {msg}"


class _HumanFilter(logging.Filter):
    """Фильтр шумных записей."""

    _SKIP = ("PING", "polling", "getUpdates")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(s in msg for s in self._SKIP)


class _HumanLog:
    """Асинхронная запись логов в файл."""

    def __init__(self, path: Path, logger_name: str = "FTS-Starwell") -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=5000)
        self._task: asyncio.Task | None = None
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.DEBUG)
        if not self._logger.handlers:
            fmt = _HumanFormatter()
            fh = logging.FileHandler(self._path, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.addFilter(_HumanFilter())
            self._logger.addHandler(fh)
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            sh.addFilter(_HumanFilter())
            self._logger.addHandler(sh)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._writer_loop(), name="fts-log-writer")

    async def stop(self) -> None:
        if self._task:
            await self._queue.put(None)
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _writer_loop(self) -> None:
        while True:
            line = await self._queue.get()
            if line is None:
                break
            try:
                await asyncio.to_thread(self._append_line, line)
            except Exception:
                pass

    def _append_line(self, line: str) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def log(self, level: str, msg: str, *args: Any) -> None:
        formatted = msg % args if args else msg
        record = self._logger.makeRecord(
            self._logger.name, getattr(logging, level.upper(), logging.INFO),
            "", 0, formatted, (), None,
        )
        line = _HumanFormatter().format(record)
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            pass
        getattr(self._logger, level.lower(), self._logger.info)(msg, *args)

    def info(self, msg: str, *args: Any) -> None:
        self.log("info", msg, *args)

    def warning(self, msg: str, *args: Any) -> None:
        self.log("warning", msg, *args)

    def error(self, msg: str, *args: Any) -> None:
        self.log("error", msg, *args)

    def debug(self, msg: str, *args: Any) -> None:
        self.log("debug", msg, *args)

    def exception(self, msg: str, *args: Any) -> None:
        self.error(f"{msg}\n{traceback.format_exc()}", *args)

    async def read_tail(self, lines: int = 80) -> str:
        if not self._path.exists():
            return "<i>Лог пуст</i>"
        try:
            content = await asyncio.to_thread(self._path.read_text, "utf-8")
            tail = content.strip().splitlines()[-lines:]
            return html.escape("\n".join(tail)) or "<i>Лог пуст</i>"
        except Exception as exc:
            return f"<i>Ошибка чтения лога: {html.escape(str(exc))}</i>"


# ═══════════════════════════════════════════════════════════════════════════════
#  Хранилище (settings.json + orders.json)
# ═══════════════════════════════════════════════════════════════════════════════

_storage_lock = asyncio.Lock()


def _storage_dir() -> Path:
    p = BASE_DIR / "storage" / "plugins" / UUID
    p.mkdir(parents=True, exist_ok=True)
    return p


class AsyncJsonStore:
    """Атомарная асинхронная запись JSON."""

    def __init__(self, filename: str, default: Any) -> None:
        self._path = _storage_dir() / filename
        self._default = default

    async def load(self) -> Any:
        async with _storage_lock:
            if not self._path.exists():
                return copy.deepcopy(self._default)
            try:
                text = await asyncio.to_thread(self._path.read_text, "utf-8")
                return json.loads(text)
            except (json.JSONDecodeError, OSError):
                bak = self._path.with_suffix(".json.bak")
                if self._path.exists():
                    await asyncio.to_thread(os.replace, str(self._path), str(bak))
                return copy.deepcopy(self._default)

    async def save(self, data: Any) -> None:
        async with _storage_lock:
            tmp = self._path.with_suffix(".tmp")
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            await asyncio.to_thread(tmp.write_text, payload, "utf-8")
            await asyncio.to_thread(os.replace, str(tmp), str(self._path))

    async def update(self, mutator) -> Any:
        data = await self.load()
        result = mutator(data)
        await self.save(data)
        return result


_settings_store = AsyncJsonStore("settings.json", DEFAULT_SETTINGS)
_orders_store = AsyncJsonStore("orders.json", {"queue": [], "active": {}, "history": []})


async def _get_settings() -> dict[str, Any]:
    data = await _settings_store.load()
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS or k == "lots"})
    # Миграция: старый fragment_jwt → api_token
    if not merged.get("api_token"):
        merged["api_token"] = str(data.get("fragment_jwt") or data.get("api_token") or "").strip()
    if "templates" in data and isinstance(data["templates"], dict):
        tpl = copy.deepcopy(DEFAULT_TEMPLATES)
        tpl.update(data["templates"])
        merged["templates"] = tpl
    if "lots" in data:
        merged["lots"] = data["lots"]
    if "stats" in data and isinstance(data["stats"], dict):
        st = copy.deepcopy(DEFAULT_SETTINGS["stats"])
        st.update(data["stats"])
        merged["stats"] = st
    return merged


async def _save_settings(settings: dict[str, Any]) -> None:
    await _settings_store.save(settings)


def _api_token(settings: dict[str, Any]) -> str:
    """Единый API-токен Fragment — все запросы только через него."""
    return str(settings.get("api_token") or settings.get("fragment_jwt") or "").strip()


def _strip_invisible(text: str) -> str:
    return INVISIBLE_RE.sub("", text or "").strip()


def _parse_number_token(token: str) -> int | None:
    cleaned = NUMBER_TOKEN_RE.search(_strip_invisible(token))
    if not cleaned:
        return None
    raw = cleaned.group(0).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        if "." in raw:
            return int(float(raw))
        return int(raw)
    except (TypeError, ValueError):
        return None


def _extract_username(text: str) -> str | None:
    text = _strip_invisible(text)
    m = USERNAME_RE.search(text)
    if m:
        return m.group(1).lower()
    if text.startswith("@") and len(text) > 1:
        candidate = text[1:].split()[0].lower()
        if re.fullmatch(r"[a-z][a-z0-9_]{3,31}", candidate):
            return candidate
    return None


def _format_rub(amount: Any) -> str:
    try:
        return f"{float(amount):.2f} ₽"
    except (TypeError, ValueError):
        return "0.00 ₽"


def _tpl(settings: dict[str, Any], key: str, **kwargs: Any) -> str:
    templates = settings.get("templates") or DEFAULT_TEMPLATES
    raw = templates.get(key) or DEFAULT_TEMPLATES.get(key, "")
    try:
        return raw.format(**kwargs)
    except (KeyError, ValueError):
        return raw


def _offer_id_from_order(order: dict[str, Any]) -> str:
    return StarvellAPI.offer_id_from_order(order)


def _stars_from_lot(settings: dict[str, Any], offer_id: str, order: dict[str, Any]) -> int:
    lots = settings.get("lots") or {}
    lot_cfg = lots.get(str(offer_id)) or lots.get(offer_id)
    if lot_cfg and lot_cfg.get("stars"):
        qty = max(1, int(order.get("quantity") or 1))
        return int(lot_cfg["stars"]) * qty
    desc = ""
    offer = order.get("offerDetails") or {}
    desc_rus = (offer.get("descriptions") or {}).get("rus") or {}
    desc = " ".join(filter(None, [
        str(desc_rus.get("description") or ""),
        str(desc_rus.get("briefDescription") or ""),
        str(order.get("product_name") or ""),
    ]))
    m = STARS_AMOUNT_RE.search(desc)
    if m:
        val = _parse_number_token(m.group(1))
        if val:
            return val * max(1, int(order.get("quantity") or 1))
    for token in re.findall(r"\d+", desc):
        val = int(token)
        if val >= 50:
            return val * max(1, int(order.get("quantity") or 1))
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
#  Fragment API (async httpx)
# ═══════════════════════════════════════════════════════════════════════════════


def _classify_send_failure(err: str | Exception, status: str = "") -> str:
    """Классификация ошибок Fragment для покупателя."""
    text = f"{err} {status}".lower()
    if any(m in text for m in LITESERVER_MARKERS):
        return "временная ошибка сети Fragment (LiteServer). Попробуйте позже или обратитесь к продавцу"
    if "insufficient" in text or "balance" in text or "not enough" in text:
        return "недостаточно средств на кошельке Fragment"
    if "username" in text or "recipient" in text or "user not found" in text:
        return "получатель не найден или недоступен для Stars"
    if "limit" in text or "minimum" in text:
        return "количество Stars вне допустимых лимитов Fragment"
    if "jwt" in text or "token" in text or "unauthorized" in text or "401" in text:
        return "ошибка авторизации Fragment API — проверьте API токен"
    if "timeout" in text or "timed out" in text:
        return "таймаут соединения с Fragment API"
    if status.upper() in FRAGMENT_PENDING:
        return "заказ ещё обрабатывается — дождитесь завершения"
    return str(err)[:200] if err else "неизвестная ошибка Fragment API"


@dataclass
class FragmentWalletInfo:
    wallet_version: str = ""
    ton_balance: float = 0.0
    usdt_balance: float = 0.0
    address: str = ""
    valid: bool = False
    token_ok: bool = False
    error: str = ""
    cached: bool = False
    updated_at: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass
class FragmentOrderResult:
    ok: bool
    status: str = ""
    order_id: str = ""
    tx_hash: str = ""
    error: str = ""
    pending: bool = False
    raw: dict = field(default_factory=dict)


def _parse_wallet_payload(data: dict[str, Any]) -> FragmentWalletInfo:
    info = FragmentWalletInfo(raw=data)
    payload = data.get("data") or data.get("wallet") or data.get("result") or data
    if not isinstance(payload, dict):
        return info
    info.valid = True
    info.token_ok = True
    info.wallet_version = str(
        payload.get("version") or payload.get("walletVersion") or payload.get("wallet_version") or ""
    )
    info.address = str(payload.get("address") or payload.get("walletAddress") or "")
    info.ton_balance = float(
        payload.get("ton") or payload.get("tonBalance") or payload.get("balance_ton")
        or payload.get("balanceTon") or payload.get("ton_balance") or 0
    )
    info.usdt_balance = float(
        payload.get("usdt") or payload.get("usdtBalance") or payload.get("balance_usdt")
        or payload.get("balanceUsdt") or payload.get("usdt_balance") or 0
    )
    rates = payload.get("rates") or payload.get("exchangeRates") or {}
    if isinstance(rates, dict):
        info.raw.setdefault("_rates", rates)
    return info


class FragmentApiHub:
    """
    Единый клиент Fragment API на одном токене.
    - один keep-alive httpx клиент
    - кэш кошелька/цен для мгновенного UI
    - закрепление рабочих endpoint после первого успешного ответа
    """

    WALLET_TTL = 20.0
    PRICES_TTL = 90.0
    RATES_TTL = 300.0

    _WALLET_PATHS = ("/api/v1/wallet", "/api/v1/wallet/balance", "/api/v1/account/wallet", "/wallet")
    _PRICE_PATHS = ("/api/v1/stars/prices", "/api/v1/prices", "/prices")
    _ORDER_PATHS = ("/api/v1/stars/order", "/api/v1/order/stars", "/api/v1/stars/buy")
    _ORDER_STATUS_TMPL = ("/api/v1/stars/order/{id}", "/api/v1/order/{id}", "/api/v1/orders/{id}")
    _USERNAME_PATHS = (
        ("/api/v1/username/check", "post"),
        ("/api/v1/stars/recipient", "post"),
        ("/api/v1/recipient/check", "post"),
    )

    def __init__(self, token: str) -> None:
        self.token = (token or "").strip()
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._wallet: FragmentWalletInfo | None = None
        self._wallet_ts = 0.0
        self._prices: dict[str, Any] = {}
        self._prices_ts = 0.0
        self._ton_rub = 0.0
        self._usdt_rub = 0.0
        self._rates_ts = 0.0
        self._route_wallet: str | None = None
        self._route_prices: str | None = None
        self._route_order: str | None = None
        self._route_order_status: str | None = None
        self._route_username: str | None = None

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def invalidate(self) -> None:
        self._wallet = None
        self._wallet_ts = 0.0
        self._prices = {}
        self._prices_ts = 0.0
        self._route_wallet = None
        self._route_prices = None
        self._route_order = None
        self._route_order_status = None
        self._route_username = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=FRAGMENT_API_BASE,
                timeout=httpx.Timeout(20.0, connect=4.0),
                limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-API-Key": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if not self.token:
            return 401, {"error": "API токен не задан"}
        client = await self._http()
        resp = await client.request(method, path, headers=self._headers(), json=json_body, params=params)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:2000]}
        if not isinstance(data, dict):
            data = {"data": data}
        return resp.status_code, data

    async def _first_ok_get(self, paths: tuple[str, ...]) -> tuple[str | None, dict[str, Any]]:
        for path in paths:
            code, data = await self._request("GET", path)
            if code == 401:
                return None, data
            if code < 400:
                return path, data
        return None, {}

    def wallet_cached(self) -> FragmentWalletInfo | None:
        if self._wallet and time.monotonic() - self._wallet_ts < self.WALLET_TTL:
            out = copy.copy(self._wallet)
            out.cached = True
            return out
        return None

    async def get_wallet(self, *, force: bool = False) -> FragmentWalletInfo:
        if not force:
            cached = self.wallet_cached()
            if cached:
                return cached
        async with self._lock:
            if not force:
                cached = self.wallet_cached()
                if cached:
                    return cached
            paths = (self._route_wallet,) if self._route_wallet else self._WALLET_PATHS
            path, data = await self._first_ok_get(paths)  # type: ignore[arg-type]
            if path:
                self._route_wallet = path
            if not path:
                err = data.get("message") or data.get("error") or "Не удалось получить баланс"
                info = FragmentWalletInfo(valid=False, token_ok=False, error=str(err), raw=data)
                self._wallet = info
                self._wallet_ts = time.monotonic()
                return info
            info = _parse_wallet_payload(data)
            info.updated_at = time.time()
            self._wallet = info
            self._wallet_ts = time.monotonic()
            self._extract_rates_from_wallet(info)
            return info

    def _extract_rates_from_wallet(self, info: FragmentWalletInfo) -> None:
        rates = info.raw.get("_rates") or {}
        if isinstance(rates, dict):
            ton = rates.get("ton") or rates.get("TON") or {}
            usdt = rates.get("usdt") or rates.get("USDT") or {}
            if isinstance(ton, dict) and ton.get("rub"):
                self._ton_rub = float(ton["rub"])
                self._rates_ts = time.monotonic()
            if isinstance(usdt, dict) and usdt.get("rub"):
                self._usdt_rub = float(usdt["rub"])
                self._rates_ts = time.monotonic()

    async def validate_token(self) -> tuple[bool, str, FragmentWalletInfo]:
        """Один запрос: проверка токена + баланс кошелька Fragment."""
        info = await self.get_wallet(force=True)
        if info.token_ok and info.valid:
            return True, "", info
        if info.error:
            return False, info.error, info
        return False, "API токен недействителен или кошелёк недоступен", info

    async def warm_cache(self) -> None:
        await asyncio.gather(
            self.get_wallet(force=True),
            self.get_star_prices(force=True),
            return_exceptions=True,
        )

    async def get_star_prices(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._prices and time.monotonic() - self._prices_ts < self.PRICES_TTL:
            return self._prices
        paths = (self._route_prices,) if self._route_prices else self._PRICE_PATHS
        path, data = await self._first_ok_get(paths)  # type: ignore[arg-type]
        if path:
            self._route_prices = path
            payload = data.get("data") or data.get("prices") or data
            if isinstance(payload, dict):
                self._prices = payload
                self._prices_ts = time.monotonic()
                return payload
        return self._prices

    async def check_username(self, username: str, quantity: int = 50) -> tuple[bool, str]:
        username = username.lstrip("@").lower()
        bodies = [
            {"username": username, "quantity": quantity, "amount": quantity},
            {"username": username, "stars": quantity},
        ]
        if self._route_username:
            for body in bodies:
                code, data = await self._request("POST", self._route_username, json_body=body)
                if code == 401:
                    return False, "API токен недействителен"
                if code < 400:
                    return self._parse_username_ok(data)
        for path, _method in self._USERNAME_PATHS:
            for body in bodies:
                code, data = await self._request("POST", path, json_body=body)
                if code == 401:
                    return False, "API токен недействителен"
                if code < 400:
                    self._route_username = path
                    return self._parse_username_ok(data)
        return True, ""

    @staticmethod
    def _parse_username_ok(data: dict[str, Any]) -> tuple[bool, str]:
        ok = data.get("ok", data.get("valid", data.get("success", True)))
        if isinstance(ok, bool):
            if ok:
                return True, ""
            return False, str(data.get("message") or data.get("error") or "ник недоступен")
        if data.get("found") is False:
            return False, str(data.get("message") or "ник не найден")
        return True, ""

    async def _order_stars_once(self, username: str, amount: int, currency: str = "TON") -> FragmentOrderResult:
        username = username.lstrip("@").lower()
        body = {
            "username": username,
            "quantity": amount,
            "amount": amount,
            "currency": currency,
            "payment_method": currency,
            "paymentMethod": currency,
        }
        paths = (self._route_order,) if self._route_order else self._ORDER_PATHS
        last_err = ""
        for path in paths:
            code, data = await self._request("POST", path, json_body=body)
            if code == 401:
                return FragmentOrderResult(False, error="API токен недействителен", raw=data)
            if code < 400:
                self._route_order = path
                return self._parse_order_response(data)
            last_err = str(data.get("message") or data.get("error") or f"HTTP {code}")
        return FragmentOrderResult(False, error=last_err or "Fragment API не ответил")

    def _parse_order_response(self, data: dict[str, Any]) -> FragmentOrderResult:
        payload = data.get("data") or data.get("order") or data
        if not isinstance(payload, dict):
            payload = data
        status = str(payload.get("status") or data.get("status") or "").upper()
        order_id = str(payload.get("id") or payload.get("orderId") or payload.get("order_id") or "")
        ok = status in FRAGMENT_OK or data.get("success") is True
        pending = status in FRAGMENT_PENDING | FRAGMENT_SENT
        return FragmentOrderResult(
            ok=ok or pending,
            status=status,
            order_id=order_id,
            tx_hash=str(payload.get("txHash") or payload.get("tx_hash") or ""),
            pending=pending and not ok,
            error="" if ok or pending else str(payload.get("error") or data.get("message") or status),
            raw=data,
        )

    async def get_order_status(self, order_id: str) -> FragmentOrderResult:
        tmpl = self._route_order_status or self._ORDER_STATUS_TMPL[0]
        path = tmpl.format(id=order_id)
        code, data = await self._request("GET", path)
        if code >= 404 and not self._route_order_status:
            for alt in self._ORDER_STATUS_TMPL[1:]:
                code, data = await self._request("GET", alt.format(id=order_id))
                if code < 404:
                    self._route_order_status = alt
                    break
        elif code < 400:
            self._route_order_status = tmpl
        if code >= 404:
            return FragmentOrderResult(False, error="Статус заказа не получен")
        result = self._parse_order_response(data)
        result.order_id = order_id
        return result

    async def order_stars(
        self,
        username: str,
        amount: int,
        *,
        liteserver_retry: bool = True,
        usdt_fallback: bool = True,
        max_retries: int = LITESERVER_RETRY,
    ) -> FragmentOrderResult:
        currencies = ["USDT", "TON"] if usdt_fallback else ["TON"]
        attempt = 0
        last_result = FragmentOrderResult(False, error="не удалось отправить")
        while attempt < max_retries:
            for currency in currencies:
                result = await self._order_stars_once(username, amount, currency)
                last_result = result
                err_low = (result.error or "").lower()
                if result.ok and not result.pending:
                    return result
                if result.pending and result.order_id:
                    polled = await self._poll_order(result.order_id)
                    if polled.ok or not polled.pending:
                        return polled
                    result = polled
                    last_result = result
                if any(m in err_low for m in LITESERVER_MARKERS):
                    if liteserver_retry and attempt + 1 < max_retries:
                        await asyncio.sleep(2 ** attempt)
                        break
                    continue
                if "insufficient" in err_low and currency == "USDT" and usdt_fallback:
                    continue
                if not result.pending:
                    return result
            attempt += 1
        return last_result

    async def _poll_order(self, order_id: str, timeout_sec: float = 90.0, interval: float = 3.0) -> FragmentOrderResult:
        deadline = time.monotonic() + timeout_sec
        last = FragmentOrderResult(False, pending=True, order_id=order_id)
        while time.monotonic() < deadline:
            last = await self.get_order_status(order_id)
            if last.ok or not last.pending:
                return last
            await asyncio.sleep(interval)
        return last

    async def ton_to_rub(self) -> float:
        if self._ton_rub and time.monotonic() - self._rates_ts < self.RATES_TTL:
            return self._ton_rub
        await self.get_wallet()
        if self._ton_rub:
            return self._ton_rub
        return await RateService.ton_to_rub()

    async def usdt_to_rub(self) -> float:
        if self._usdt_rub and time.monotonic() - self._rates_ts < self.RATES_TTL:
            return self._usdt_rub
        await self.get_wallet()
        if self._usdt_rub:
            return self._usdt_rub
        return await RateService.usdt_to_rub()


# Обратная совместимость
AsyncFragmentClient = FragmentApiHub


# ═══════════════════════════════════════════════════════════════════════════════
#  Курсы валют и автопricing / автодемп
# ═══════════════════════════════════════════════════════════════════════════════


class RateService:
    """Fallback-курсы TON/USDT → RUB (если Fragment API не отдал rates)."""

    _cache: dict[str, tuple[float, float]] = {}
    _cache_ttl = 600.0

    @classmethod
    async def ton_to_rub(cls) -> float:
        cached = cls._cache.get("ton_rub")
        if cached and time.monotonic() - cached[1] < cls._cache_ttl:
            return cached[0]
        rate = await cls._fetch_ton_rub()
        cls._cache["ton_rub"] = (rate, time.monotonic())
        return rate

    @classmethod
    async def usdt_to_rub(cls) -> float:
        cached = cls._cache.get("usdt_rub")
        if cached and time.monotonic() - cached[1] < cls._cache_ttl:
            return cached[0]
        rate = await cls._fetch_usdt_rub()
        cls._cache["usdt_rub"] = (rate, time.monotonic())
        return rate

    @classmethod
    async def _fetch_ton_rub(cls) -> float:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "the-open-network", "vs_currencies": "rub"},
                )
                data = resp.json()
                return float(data["the-open-network"]["rub"])
        except Exception:
            return 300.0

    @classmethod
    async def _fetch_usdt_rub(cls) -> float:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "tether", "vs_currencies": "rub"},
                )
                data = resp.json()
                return float(data["tether"]["rub"])
        except Exception:
            return 90.0


class AutopriceService:
    """Автообновление цен лотов по Fragment + markup."""

    @staticmethod
    async def compute_lot_price_rub(
        stars: int,
        fragment_prices: dict[str, Any],
        markup_percent: float,
        hub: FragmentApiHub | None = None,
    ) -> float | None:
        if stars <= 0:
            return None
        ton_rub = await hub.ton_to_rub() if hub else await RateService.ton_to_rub()
        usdt_rub = await hub.usdt_to_rub() if hub else await RateService.usdt_to_rub()
        cost_ton = None
        cost_usdt = None
        if isinstance(fragment_prices, dict):
            for key, val in fragment_prices.items():
                kl = str(key).lower()
                if "ton" in kl and isinstance(val, (int, float, dict)):
                    if isinstance(val, dict):
                        per_star = val.get(str(stars)) or val.get("per_star") or val.get("price")
                        if per_star:
                            cost_ton = float(per_star) if "per" in kl else float(per_star) * stars
                    elif "per" in kl:
                        cost_ton = float(val) * stars
                    else:
                        cost_ton = float(val)
                if "usdt" in kl and isinstance(val, (int, float, dict)):
                    if isinstance(val, dict):
                        per_star = val.get(str(stars)) or val.get("per_star") or val.get("price")
                        if per_star:
                            cost_usdt = float(per_star) if "per" in kl else float(per_star) * stars
                    elif "per" in kl:
                        cost_usdt = float(val) * stars
                    else:
                        cost_usdt = float(val)
            price_obj = fragment_prices.get(str(stars)) or fragment_prices.get(stars)
            if isinstance(price_obj, dict):
                cost_ton = cost_ton or float(price_obj.get("ton") or price_obj.get("TON") or 0) or None
                cost_usdt = cost_usdt or float(price_obj.get("usdt") or price_obj.get("USDT") or 0) or None
        cost_rub = 0.0
        if cost_ton:
            cost_rub = cost_ton * ton_rub
        elif cost_usdt:
            cost_rub = cost_usdt * usdt_rub
        else:
            cost_rub = stars * 0.015 * ton_rub
        if cost_rub <= 0:
            return None
        return round(cost_rub * (1 + markup_percent / 100), 2)


class AutodumpService:
    """Поиск минимальных цен конкурентов на Starvell."""

    @staticmethod
    async def fetch_category_offers(api: StarvellAPI, category_url: str) -> list[dict[str, Any]]:
        if not category_url:
            return []
        try:
            path = category_url.replace("https://starvell.com", "").strip("/")
            data = await api._next_data_get(f"{path}.json", category_url)
            props = data.get("pageProps") or {}
            offers = props.get("offers") or props.get("categoryOffers") or []
            if isinstance(offers, dict):
                offers = offers.get("items") or offers.get("offers") or []
            result = []
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                desc = (offer.get("descriptions") or {}).get("rus") or {}
                title = str(desc.get("briefDescription") or desc.get("description") or "")
                stars = 0
                m = STARS_AMOUNT_RE.search(title)
                if m:
                    stars = _parse_number_token(m.group(1)) or 0
                if not stars:
                    for token in re.findall(r"\d+", title):
                        v = int(token)
                        if v >= 50:
                            stars = v
                            break
                price = offer.get("price")
                try:
                    price_f = float(price)
                except (TypeError, ValueError):
                    continue
                if stars >= 50:
                    result.append({"stars": stars, "price": price_f, "title": title, "id": offer.get("id")})
            return result
        except Exception:
            return []

    @staticmethod
    def target_price(competitors: list[dict[str, Any]], stars: int, min_price: float) -> float | None:
        if not competitors or stars <= 0:
            return None
        exact = [c for c in competitors if c["stars"] == stars]
        if exact:
            base = min(c["price"] for c in exact)
        else:
            refs = sorted(competitors, key=lambda x: x["stars"])
            if len(refs) < 1:
                return None
            best = min(refs, key=lambda x: x["price"] / x["stars"])
            base = (best["price"] / best["stars"]) * stars
        target = round(base - FTS_AUTODUMP_STEP_RUB, 2)
        return target if target >= min_price else None


# ═══════════════════════════════════════════════════════════════════════════════
#  Менеджер лотов (баланс / автоактивация)
# ═══════════════════════════════════════════════════════════════════════════════


class LotManager:
    """Фильтрация лотов по балансу Fragment."""

    @staticmethod
    async def estimate_lot_cost(
        client: FragmentApiHub,
        stars: int,
        fragment_prices: dict[str, Any],
    ) -> tuple[float, float]:
        ton_cost = 0.0
        usdt_cost = 0.0
        if isinstance(fragment_prices, dict):
            entry = fragment_prices.get(str(stars)) or fragment_prices.get(stars)
            if isinstance(entry, dict):
                ton_cost = float(entry.get("ton") or entry.get("TON") or 0)
                usdt_cost = float(entry.get("usdt") or entry.get("USDT") or 0)
        if ton_cost <= 0 and usdt_cost <= 0:
            cached = client.wallet_cached()
            if cached and cached.valid:
                ton_cost = stars * 0.015
                usdt_cost = ton_cost * 2.5
        return ton_cost, usdt_cost

    @staticmethod
    async def sync_lots(
        api: StarvellAPI,
        client: FragmentApiHub,
        settings: dict[str, Any],
        human_log: _HumanLog,
    ) -> None:
        if not settings.get("plugin_enabled"):
            return
        wallet = client.wallet_cached() or await client.get_wallet()
        lots = settings.get("lots") or {}
        if not lots:
            return
        prices = await client.get_star_prices()
        global_ok = (
            wallet.ton_balance >= float(settings.get("min_balance_ton") or 0)
            or wallet.usdt_balance >= float(settings.get("min_balance_usdt") or 0)
        )
        for lot_id, cfg in list(lots.items()):
            if not cfg.get("enabled", True):
                continue
            stars = int(cfg.get("stars") or 0)
            if stars <= 0:
                continue
            ton_need, usdt_need = await LotManager.estimate_lot_cost(client, stars, prices)
            can_cover = wallet.ton_balance >= ton_need or wallet.usdt_balance >= usdt_need
            should_active = bool(settings.get("lots_enabled")) and global_ok and can_cover
            hidden_key = "_hidden_by_balance"
            prev_hidden = cfg.get(hidden_key, False)
            try:
                if should_active and prev_hidden:
                    await api.activate_offer(lot_id)
                    cfg[hidden_key] = False
                    human_log.info("Лот %s активирован (баланс OK)", lot_id)
                elif not should_active and not prev_hidden:
                    await api.deactivate_offer(lot_id)
                    cfg[hidden_key] = True
                    human_log.info("Лот %s деактивирован (баланс/лимит)", lot_id)
            except StarvellAPIError as exc:
                human_log.warning("Лот %s: %s", lot_id, exc)
        if not global_ok and settings.get("auto_deactivate"):
            for lot_id, cfg in lots.items():
                if cfg.get("enabled", True) and not cfg.get("_hidden_by_balance"):
                    try:
                        await api.deactivate_offer(lot_id)
                        cfg["_hidden_by_balance"] = True
                    except StarvellAPIError:
                        pass
            human_log.warning(
                "Автодеактивация: TON=%.4f USDT=%.4f ниже порога",
                wallet.ton_balance, wallet.usdt_balance,
            )


# ═══════════════════════════════════════════════════════════════════════════════
#  Очередь заказов
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class QueuedOrder:
    order_id: str
    buyer: str
    buyer_id: int | None
    chat_id: str
    stars: int
    price: float
    offer_id: str
    username: str = ""
    status: str = "waiting_username"
    created_at: float = field(default_factory=time.time)
    reminded_at: float = 0.0
    fragment_order_id: str = ""
    sent: bool = False
    refunded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "buyer": self.buyer,
            "buyer_id": self.buyer_id,
            "chat_id": self.chat_id,
            "stars": self.stars,
            "price": self.price,
            "offer_id": self.offer_id,
            "username": self.username,
            "status": self.status,
            "created_at": self.created_at,
            "reminded_at": self.reminded_at,
            "fragment_order_id": self.fragment_order_id,
            "sent": self.sent,
            "refunded": self.refunded,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueuedOrder:
        return cls(
            order_id=str(data.get("order_id") or ""),
            buyer=str(data.get("buyer") or ""),
            buyer_id=data.get("buyer_id"),
            chat_id=str(data.get("chat_id") or ""),
            stars=int(data.get("stars") or 0),
            price=float(data.get("price") or 0),
            offer_id=str(data.get("offer_id") or ""),
            username=str(data.get("username") or ""),
            status=str(data.get("status") or "waiting_username"),
            created_at=float(data.get("created_at") or time.time()),
            reminded_at=float(data.get("reminded_at") or 0),
            fragment_order_id=str(data.get("fragment_order_id") or ""),
            sent=bool(data.get("sent")),
            refunded=bool(data.get("refunded")),
        )


class OrderQueue:
    """Умная очередь: strict / skip_ready / timeout_end."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def _load_state(self) -> dict[str, Any]:
        return await _orders_store.load()

    async def _save_state(self, state: dict[str, Any]) -> None:
        await _orders_store.save(state)

    async def enqueue(self, item: QueuedOrder) -> int:
        async with self._lock:
            state = await self._load_state()
            queue: list[dict] = state.setdefault("queue", [])
            if any(q.get("order_id") == item.order_id for q in queue):
                return len(queue)
            queue.append(item.to_dict())
            state["active"][item.order_id] = item.to_dict()
            await self._save_state(state)
            return len(queue)

    async def get_queue(self) -> list[QueuedOrder]:
        state = await self._load_state()
        return [QueuedOrder.from_dict(x) for x in state.get("queue", [])]

    async def get_active(self, order_id: str) -> QueuedOrder | None:
        state = await self._load_state()
        raw = (state.get("active") or {}).get(order_id)
        return QueuedOrder.from_dict(raw) if raw else None

    async def update_item(self, item: QueuedOrder) -> None:
        async with self._lock:
            state = await self._load_state()
            queue = state.setdefault("queue", [])
            for i, q in enumerate(queue):
                if q.get("order_id") == item.order_id:
                    queue[i] = item.to_dict()
                    break
            state.setdefault("active", {})[item.order_id] = item.to_dict()
            await self._save_state(state)

    async def remove(self, order_id: str) -> None:
        async with self._lock:
            state = await self._load_state()
            state["queue"] = [q for q in state.get("queue", []) if q.get("order_id") != order_id]
            state.get("active", {}).pop(order_id, None)
            await self._save_state(state)

    async def move_to_end(self, order_id: str) -> None:
        async with self._lock:
            state = await self._load_state()
            queue = state.get("queue", [])
            item = next((q for q in queue if q.get("order_id") == order_id), None)
            if item:
                queue = [q for q in queue if q.get("order_id") != order_id]
                queue.append(item)
                state["queue"] = queue
                await self._save_state(state)

    async def archive(self, item: QueuedOrder, status: str) -> None:
        async with self._lock:
            state = await self._load_state()
            state["queue"] = [q for q in state.get("queue", []) if q.get("order_id") != item.order_id]
            state.get("active", {}).pop(item.order_id, None)
            hist = state.setdefault("history", [])
            record = item.to_dict()
            record["archived_status"] = status
            record["archived_at"] = time.time()
            hist.insert(0, record)
            state["history"] = hist[:500]
            await self._save_state(state)

    async def next_processable(self, settings: dict[str, Any]) -> QueuedOrder | None:
        queue = await self.get_queue()
        if not queue:
            return None
        mode = settings.get("queue_mode") or "strict"
        if mode == "strict":
            return queue[0]
        if mode == "skip_ready":
            for item in queue:
                if item.username and item.status in ("ready", "waiting_confirm"):
                    return item
            return queue[0]
        return queue[0]


# ═══════════════════════════════════════════════════════════════════════════════
#  Статистика
# ═══════════════════════════════════════════════════════════════════════════════


class Statistics:
    @staticmethod
    async def record(settings: dict[str, Any], event: str, **fields: Any) -> None:
        stats = settings.setdefault("stats", copy.deepcopy(DEFAULT_SETTINGS["stats"]))
        stats["total_orders"] = stats.get("total_orders", 0) + (1 if event == "new" else 0)
        if event == "completed":
            stats["completed"] = stats.get("completed", 0) + 1
            stats["stars_sent"] = stats.get("stars_sent", 0) + int(fields.get("stars") or 0)
            stats["revenue_rub"] = float(stats.get("revenue_rub", 0)) + float(fields.get("price") or 0)
            stats["cost_ton"] = float(stats.get("cost_ton", 0)) + float(fields.get("cost_ton") or 0)
            buyer = fields.get("buyer")
            if buyer:
                buyers = stats.setdefault("buyers", {})
                buyers[buyer] = buyers.get(buyer, 0) + 1
        elif event == "failed":
            stats["failed"] = stats.get("failed", 0) + 1
            err = str(fields.get("error") or "unknown")[:80]
            errors = stats.setdefault("errors", {})
            errors[err] = errors.get(err, 0) + 1
        elif event == "refunded":
            stats["refunded"] = stats.get("refunded", 0) + 1
        await _save_settings(settings)

    @staticmethod
    def top_buyers(stats: dict[str, Any], limit: int = 5) -> list[tuple[str, int]]:
        buyers = stats.get("buyers") or {}
        return sorted(buyers.items(), key=lambda x: x[1], reverse=True)[:limit]

    @staticmethod
    def top_errors(stats: dict[str, Any], limit: int = 5) -> list[tuple[str, int]]:
        errors = stats.get("errors") or {}
        return sorted(errors.items(), key=lambda x: x[1], reverse=True)[:limit]

    @staticmethod
    def conversion(stats: dict[str, Any]) -> float:
        total = max(1, int(stats.get("total_orders") or 0))
        completed = int(stats.get("completed") or 0)
        return round(100.0 * completed / total, 1)


# ═══════════════════════════════════════════════════════════════════════════════
#  Обработчик заказов
# ═══════════════════════════════════════════════════════════════════════════════


class OrderProcessor:
    """Ядро: приём заказов, отправка Stars, возвраты."""

    def __init__(self, plugin: "Plugin") -> None:
        self.plugin = plugin
        self.queue = OrderQueue()
        self._processing_lock = asyncio.Lock()

    async def settings(self) -> dict[str, Any]:
        return await _get_settings()

    async def fragment_hub(self) -> FragmentApiHub | None:
        return await self.plugin.get_fragment_hub()

    async def stv_api(self):
        return self.plugin.core.get_api()

    async def handle_new_order(self, ctx: OrderContext) -> None:
        settings = await self.settings()
        if not settings.get("plugin_enabled"):
            return
        order = ctx.order or {}
        offer_id = _offer_id_from_order(order)
        lots = settings.get("lots") or {}
        lot_cfg = lots.get(str(offer_id)) if offer_id else None
        if lots and offer_id and not lot_cfg:
            return
        stars = _stars_from_lot(settings, offer_id, order)
        if stars < 50:
            self.plugin.hlog.warning("Заказ #%s: не определено кол-во Stars", ctx.order_id)
            return
        if lot_cfg and not lot_cfg.get("enabled", True):
            return

        chat_id = ctx.chat_id or ""
        if not chat_id and ctx.buyer_id:
            api = await self.stv_api()
            if api:
                chat_id = await api.find_chat_by_buyer(int(ctx.buyer_id)) or ""

        item = QueuedOrder(
            order_id=ctx.order_id,
            buyer=ctx.buyer_username,
            buyer_id=ctx.buyer_id,
            chat_id=chat_id or "",
            stars=stars,
            price=float(ctx.price or 0),
            offer_id=offer_id,
        )

        desc_username = _extract_username(_order_full_text(order))
        if desc_username and settings.get("preorder_username"):
            item.username = desc_username
            if settings.get("auto_send_without_plus"):
                item.status = "ready"
            else:
                item.status = "waiting_confirm"

        pos = await self.queue.enqueue(item)
        await Statistics.record(settings, "new")
        self.plugin.hlog.info("Заказ #%s: %s Stars, очередь #%s", ctx.order_id, stars, pos)

        if item.username and item.status == "ready":
            await self.process_next()
            return

        if chat_id:
            api = await self.stv_api()
            if api:
                if pos > 1:
                    msg = _tpl(settings, "queue_position", pos=pos)
                    await api.send_message(chat_id, msg)
                else:
                    if item.username and item.status == "waiting_confirm":
                        msg = _tpl(
                            settings, "confirm_username",
                            username=item.username, stars=stars,
                            price=_format_rub(ctx.price),
                        )
                    else:
                        msg = _tpl(settings, "welcome")
                    await api.send_message(chat_id, msg)

    async def handle_message(self, ctx: MessageContext) -> bool:
        settings = await self.settings()
        if not settings.get("plugin_enabled"):
            return False
        text = _strip_invisible(ctx.text)
        if not text:
            return False

        active = await self._find_active_for_chat(ctx.chat_id, ctx.username)
        if not active:
            return False

        if BACK_CMD_RE.match(text):
            await self._handle_back(active, settings)
            return True

        action = self._confirm_action(text)
        if action == "plus":
            if active.status == "waiting_confirm" and active.username:
                active.status = "ready"
                await self.queue.update_item(active)
                await self.process_next(force_order=active.order_id)
            return True
        if action == "minus":
            await self._refund(active, settings, reason="buyer_cancel")
            return True

        username = _extract_username(text)
        if username:
            active.username = username
            if settings.get("check_username"):
                hub = await self.fragment_hub()
                if hub:
                    ok, err = await hub.check_username(username, active.stars)
                    if not ok:
                        api = await self.stv_api()
                        if api and active.chat_id:
                            await api.send_message(
                                active.chat_id,
                                _tpl(settings, "invalid_username", username=username),
                            )
                        return True
            if settings.get("auto_send_without_plus"):
                active.status = "ready"
                await self.queue.update_item(active)
                await self.process_next(force_order=active.order_id)
            else:
                active.status = "waiting_confirm"
                await self.queue.update_item(active)
                api = await self.stv_api()
                if api and active.chat_id:
                    await api.send_message(
                        active.chat_id,
                        _tpl(
                            settings, "confirm_username",
                            username=username, stars=active.stars,
                            price=_format_rub(active.price),
                        ),
                    )
            return True
        return False

    async def _find_active_for_chat(self, chat_id: str, username: str) -> QueuedOrder | None:
        for item in await self.queue.get_queue():
            if item.sent or item.refunded:
                continue
            if chat_id and item.chat_id == chat_id:
                return item
            if username and item.buyer.lower() == username.lower():
                return item
        return None

    def _confirm_action(self, text: str) -> str | None:
        cleaned = text.strip().lower()
        if cleaned in CONFIRM_PLUS:
            return "plus"
        if cleaned in CONFIRM_MINUS:
            return "minus"
        return None

    async def process_next(self, force_order: str | None = None) -> None:
        async with self._processing_lock:
            settings = await self.settings()
            if force_order:
                item = await self.queue.get_active(force_order)
            else:
                item = await self.queue.next_processable(settings)
            if not item or item.sent or item.refunded:
                return
            if item.status != "ready" or not item.username:
                return
            await self._send_stars(item, settings)

    async def _send_stars(self, item: QueuedOrder, settings: dict[str, Any]) -> None:
        hub = await self.fragment_hub()
        api = await self.stv_api()
        if not hub:
            self.plugin.hlog.error("Fragment API токен не настроен")
            return
        if api and item.chat_id:
            await api.send_message(
                item.chat_id,
                _tpl(settings, "sending", stars=item.stars, username=item.username),
            )
        result = await hub.order_stars(
            item.username,
            item.stars,
            liteserver_retry=bool(settings.get("liteserver_retry")),
            usdt_fallback=bool(settings.get("usdt_fallback_to_ton")),
            max_retries=LITESERVER_RETRY,
        )
        item.fragment_order_id = result.order_id

        if result.ok and not result.pending:
            item.sent = True
            item.status = "completed"
            await self.queue.archive(item, "completed")
            await Statistics.record(
                settings, "completed",
                stars=item.stars, price=item.price, buyer=item.buyer,
            )
            if api and item.chat_id:
                await api.send_message(
                    item.chat_id,
                    _tpl(
                        settings, "success",
                        stars=item.stars, username=item.username,
                        order_url=STV_ORDER_URL.format(order_id=item.order_id),
                    ),
                )
                await api.send_message(item.chat_id, _tpl(settings, "review_request"))
            self.plugin.hlog.info("Заказ #%s: Stars отправлены → @%s", item.order_id, item.username)
            await self.process_next()
            return

        if result.pending:
            polled = await hub._poll_order(result.order_id)
            if polled.ok:
                item.sent = True
                item.status = "completed"
                await self.queue.archive(item, "completed")
                await Statistics.record(settings, "completed", stars=item.stars, price=item.price, buyer=item.buyer)
                if api and item.chat_id:
                    await api.send_message(
                        item.chat_id,
                        _tpl(settings, "success", stars=item.stars, username=item.username,
                             order_url=STV_ORDER_URL.format(order_id=item.order_id)),
                    )
                await self.process_next()
                return
            if polled.pending:
                item.status = "pending_fragment"
                await self.queue.update_item(item)
                return
            result = polled

        reason = _classify_send_failure(result.error, result.status)
        await Statistics.record(settings, "failed", error=reason)
        self.plugin.hlog.error("Заказ #%s: %s", item.order_id, reason)
        if api and item.chat_id:
            await api.send_message(item.chat_id, _tpl(settings, "error", reason=reason))
        if settings.get("auto_refund") and not result.pending:
            await self._refund(item, settings, reason=reason)

    async def _refund(self, item: QueuedOrder, settings: dict[str, Any], reason: str = "") -> None:
        if item.refunded or item.sent:
            return
        api = await self.stv_api()
        if not api:
            return
        try:
            await api.refund_order(item.order_id)
            item.refunded = True
            item.status = "refunded"
            await self.queue.archive(item, "refunded")
            await Statistics.record(settings, "refunded")
            if item.chat_id:
                await api.send_message(item.chat_id, _tpl(settings, "refunded", order_id=item.order_id))
            self.plugin.hlog.info("Возврат #%s (%s)", item.order_id, reason)
            await self.process_next()
        except Exception as exc:
            self.plugin.hlog.error("Ошибка возврата #%s: %s", item.order_id, exc)

    async def _handle_back(self, item: QueuedOrder, settings: dict[str, Any]) -> None:
        api = await self.stv_api()
        if not settings.get("back_command_enabled"):
            if api and item.chat_id:
                await api.send_message(item.chat_id, _tpl(settings, "back_denied"))
            return
        if item.sent:
            if api and item.chat_id:
                await api.send_message(item.chat_id, _tpl(settings, "back_denied"))
            return
        if settings.get("back_priority") or not settings.get("auto_refund"):
            await self._refund(item, settings, reason="back_command")
            if api and item.chat_id:
                await api.send_message(item.chat_id, _tpl(settings, "back_ok"))
        elif settings.get("auto_refund"):
            await self._refund(item, settings, reason="back_command")

    async def check_timeouts(self) -> None:
        settings = await self.settings()
        if settings.get("queue_mode") != "timeout_end":
            return
        timeout = int(settings.get("queue_timeout_sec") or 300)
        api = await self.stv_api()
        now = time.time()
        for item in await self.queue.get_queue():
            if item.sent or item.refunded or item.username:
                continue
            if now - item.created_at >= timeout:
                await self.queue.move_to_end(item.order_id)
                if api and item.chat_id:
                    await api.send_message(item.chat_id, _tpl(settings, "timeout_moved"))

    async def send_reminders(self) -> None:
        settings = await self.settings()
        api = await self.stv_api()
        now = time.time()
        for item in await self.queue.get_queue():
            if item.sent or item.refunded or item.username:
                continue
            if now - item.reminded_at < 120:
                continue
            if api and item.chat_id:
                await api.send_message(
                    item.chat_id,
                    _tpl(settings, "reminder", stars=item.stars),
                )
                item.reminded_at = now
                await self.queue.update_item(item)


def _order_full_text(order: dict[str, Any]) -> str:
    offer = order.get("offerDetails") or {}
    desc = (offer.get("descriptions") or {}).get("rus") or {}
    parts = [
        str(desc.get("description") or ""),
        str(desc.get("briefDescription") or ""),
        str(order.get("comment") or ""),
        str(order.get("buyerComment") or ""),
    ]
    return "\n".join(p for p in parts if p)


# ═══════════════════════════════════════════════════════════════════════════════
#  Фоновые воркеры
# ═══════════════════════════════════════════════════════════════════════════════


class BackgroundWorkers:
    def __init__(self, plugin: "Plugin") -> None:
        self.plugin = plugin
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop(self._balance_worker, 120), name="fts-balance"),
            asyncio.create_task(self._loop(self._autoprice_worker, 600), name="fts-autoprice"),
            asyncio.create_task(self._loop(self._autodump_worker, 900), name="fts-autodump"),
            asyncio.create_task(self._loop(self._queue_worker, 30), name="fts-queue"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _loop(self, coro, interval: float) -> None:
        while True:
            try:
                await coro()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.plugin.hlog.exception("Worker error: %s", exc)
            await asyncio.sleep(interval)

    async def _balance_worker(self) -> None:
        settings = await _get_settings()
        hub = await self.plugin.get_fragment_hub()
        api = self.plugin.core.get_api()
        if hub and api:
            await hub.get_wallet(force=True)
            await LotManager.sync_lots(api, hub, settings, self.plugin.hlog)
            await _save_settings(settings)

    async def _autoprice_worker(self) -> None:
        settings = await _get_settings()
        if not settings.get("autoprice_enabled"):
            return
        hub = await self.plugin.get_fragment_hub()
        api = self.plugin.core.get_api()
        if not hub or not api:
            return
        prices = await hub.get_star_prices()
        markup = float(settings.get("markup_percent") or 15)
        lots = settings.get("lots") or {}
        for lot_id, cfg in lots.items():
            stars = int(cfg.get("stars") or 0)
            if stars <= 0 or not cfg.get("enabled", True):
                continue
            price = await AutopriceService.compute_lot_price_rub(stars, prices, markup, hub)
            if price is None:
                continue
            try:
                await api.partial_update_offer(str(lot_id), {"price": price})
                cfg["price"] = price
                self.plugin.hlog.info("Автоцена лот %s → %.2f ₽", lot_id, price)
            except StarvellAPIError as exc:
                self.plugin.hlog.warning("Автоцена лот %s: %s", lot_id, exc)
        await _save_settings(settings)

    async def _autodump_worker(self) -> None:
        settings = await _get_settings()
        if not settings.get("autodump_enabled"):
            return
        api = self.plugin.core.get_api()
        if not api:
            return
        category_url = settings.get("category_url") or ""
        competitors = await AutodumpService.fetch_category_offers(api, category_url)
        min_price = float(settings.get("autodump_min_price") or 0.5)
        lots = settings.get("lots") or {}
        for lot_id, cfg in lots.items():
            stars = int(cfg.get("stars") or 0)
            floor = float(cfg.get("dump_floor") or min_price)
            target = AutodumpService.target_price(competitors, stars, floor)
            if target is None:
                continue
            current = float(cfg.get("price") or 0)
            if current <= 0 or target < current - FTS_AUTODUMP_STEP_RUB / 2:
                try:
                    await api.partial_update_offer(str(lot_id), {"price": target})
                    cfg["price"] = target
                    self.plugin.hlog.info("Автодемп лот %s → %.2f ₽", lot_id, target)
                except StarvellAPIError as exc:
                    self.plugin.hlog.warning("Автодемп лот %s: %s", lot_id, exc)
        await _save_settings(settings)

    async def _queue_worker(self) -> None:
        await self.plugin.processor.check_timeouts()
        await self.plugin.processor.send_reminders()
        await self.plugin.processor.process_next()


# ═══════════════════════════════════════════════════════════════════════════════
#  Telegram UI
# ═══════════════════════════════════════════════════════════════════════════════


class TelegramUI:
    """Inline-меню продавца (идентичная структура FTS-Plugin)."""

    def __init__(self, plugin: "Plugin") -> None:
        self.p = plugin

    def _btn(self, text: str, action: str) -> InlineKeyboardButton:
        return self.p.panel_btn(text, self.p.UUID, action)

    async def main_menu_kb(self, settings: dict[str, Any]) -> InlineKeyboardMarkup:
        def flag(key: str) -> str:
            return "🟢" if settings.get(key) else "🔴"

        rows = [
            [self._btn(f"{flag('plugin_enabled')} Плагин", "toggle:plugin_enabled")],
            [self._btn(f"{flag('lots_enabled')} Лоты", "toggle:lots_enabled")],
            [self._btn(f"{flag('auto_refund')} Автовозврат", "toggle:auto_refund")],
            [self._btn(f"{flag('auto_deactivate')} Автодеактивация", "toggle:auto_deactivate")],
            [self._btn(f"{flag('back_command_enabled')} Команда !бэк", "toggle:back_command_enabled")],
            [self._btn(f"{flag('preorder_username')} Ник из заказа", "toggle:preorder_username")],
            [
                self._btn("🔑 API токен", "menu:token"),
                self._btn("📦 Лоты", "menu:lots"),
            ],
            [
                self._btn("⚙️ Мини-настройки", "menu:mini"),
                self._btn("💬 Сообщения", "menu:templates"),
            ],
            [
                self._btn("💰 Цены", "menu:pricing"),
                self._btn("📊 Статистика", "menu:stats"),
            ],
            [
                self._btn("📜 Логи", "menu:logs"),
                self._btn("📤 Экспорт", "menu:export"),
            ],
            [self._btn("🔄 Обновить", "refresh")],
            [self.p.panel_back_btn(self.p.UUID)],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def render_main(self, *, force_balance: bool = False) -> tuple[str, InlineKeyboardMarkup]:
        settings = await _get_settings()
        wallet_txt = "токен не задан"
        age_hint = ""
        hub = await self.p.get_fragment_hub()
        if hub:
            w = hub.wallet_cached()
            if w and w.valid and not force_balance:
                wallet_txt = (
                    f"TON {w.ton_balance:.4f} · USDT {w.usdt_balance:.2f}"
                    f" ({w.wallet_version or 'wallet'})"
                )
                age_hint = " · кэш"
            else:
                w = await hub.get_wallet(force=force_balance)
                if w.valid:
                    wallet_txt = (
                        f"TON {w.ton_balance:.4f} · USDT {w.usdt_balance:.2f}"
                        f" ({w.wallet_version or 'wallet'})"
                    )
                elif w.error:
                    wallet_txt = w.error[:60]
            if not force_balance and hub.token:
                asyncio.create_task(hub.get_wallet(force=True))
        queue_len = len(await self.p.processor.queue.get_queue())
        stats = settings.get("stats") or {}
        text = (
            f"⭐ <b>{NAME}</b> v{VERSION}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{'🟢' if settings.get('plugin_enabled') else '🔴'} "
            f"{'Работает' if settings.get('plugin_enabled') else 'Выключен'}\n"
            f"💎 Fragment: <code>{html.escape(wallet_txt)}{age_hint}</code>\n"
            f"📋 Очередь: <b>{queue_len}</b>\n"
            f"✅ Выполнено: <b>{stats.get('completed', 0)}</b> · "
            f"⭐ Stars: <b>{stats.get('stars_sent', 0)}</b>\n"
            f"💵 Выручка: <b>{_format_rub(stats.get('revenue_rub', 0))}</b>\n\n"
            f"<i>{DESCRIPTION}</i>\n"
            f"👤 {CREDITS}"
        )
        return text, await self.main_menu_kb(settings)

    async def handle_callback(self, call: CallbackQuery, action: str) -> bool:
        if action == "refresh":
            text, kb = await self.render_main(force_balance=True)
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            await call.answer("Баланс обновлён")
            return True
        if action.startswith("toggle:"):
            key = action.split(":", 1)[1]
            settings = await _get_settings()
            settings[key] = not bool(settings.get(key))
            await _save_settings(settings)
            text, kb = await self.render_main()
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            await call.answer(f"{'🟢' if settings[key] else '🔴'} {key}")
            return True
        if action == "menu:token" or action == "menu:jwt":
            return await self._show_token(call)
        if action == "menu:lots":
            return await self._show_lots(call)
        if action == "menu:mini":
            return await self._show_mini(call)
        if action == "menu:templates":
            return await self._show_templates(call)
        if action == "menu:pricing":
            return await self._show_pricing(call)
        if action == "menu:stats":
            return await self._show_stats(call)
        if action == "menu:logs":
            return await self._show_logs(call)
        if action == "menu:export":
            return await self._show_export(call)
        if action.startswith("lots:"):
            return await self._lots_action(call, action)
        if action.startswith("mini:"):
            return await self._mini_action(call, action)
        if action.startswith("tpl:"):
            return await self._tpl_action(call, action)
        return False

    async def _show_token(self, call: CallbackQuery) -> bool:
        settings = await _get_settings()
        token = _api_token(settings)
        masked = f"{token[:8]}…{token[-4:]}" if len(token) > 16 else ("—" if not token else "***")
        valid_txt = "—"
        hub = await self.p.get_fragment_hub()
        if hub and token:
            ok, err, wallet = await hub.validate_token()
            if ok and wallet.valid:
                valid_txt = (
                    f"✅ Токен активен\n"
                    f"TON <b>{wallet.ton_balance:.4f}</b> · USDT <b>{wallet.usdt_balance:.2f}</b>"
                )
                if wallet.wallet_version:
                    valid_txt += f"\nКошелёк: <code>{html.escape(wallet.wallet_version)}</code>"
                if wallet.address:
                    valid_txt += f"\nАдрес: <code>{html.escape(wallet.address[:20])}…</code>"
            else:
                valid_txt = f"❌ {html.escape(err or 'ошибка токена')}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [self._btn("✅ Проверить баланс", "token:validate")],
            [self._btn("◀️ Назад", "refresh")],
        ])
        await call.message.edit_text(
            f"🔑 <b>Fragment API токен</b>\n\n"
            f"Токен: <code>{html.escape(masked)}</code>\n{valid_txt}\n\n"
            f"Один токен используется для баланса, заказов Stars и всех настроек.\n"
            f"Задайте <code>api_token</code> в ⚙️ Настройках плагина.",
            parse_mode="HTML", reply_markup=kb,
        )
        await call.answer()
        return True

    async def _show_lots(self, call: CallbackQuery) -> bool:
        settings = await _get_settings()
        lots = settings.get("lots") or {}
        lines = [f"📦 <b>Лоты ({len(lots)})</b>\n"]
        for lid, cfg in list(lots.items())[:20]:
            icon = "🟢" if cfg.get("enabled", True) else "🔴"
            lines.append(
                f"{icon} <code>{lid}</code> — {cfg.get('stars', '?')}⭐ · "
                f"{_format_rub(cfg.get('price', 0))}"
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [self._btn("➕ Добавить лот", "lots:add")],
            [self._btn("🟢 Вкл все", "lots:enable_all"), self._btn("🔴 Выкл все", "lots:disable_all")],
            [self._btn("◀️ Назад", "refresh")],
        ])
        await call.message.edit_text("\n".join(lines) or "📦 Лотов нет", parse_mode="HTML", reply_markup=kb)
        await call.answer()
        return True

    async def _lots_action(self, call: CallbackQuery, action: str) -> bool:
        settings = await _get_settings()
        lots = settings.setdefault("lots", {})
        if action == "lots:enable_all":
            for cfg in lots.values():
                cfg["enabled"] = True
            await _save_settings(settings)
            await call.answer("Все лоты включены")
            return await self._show_lots(call)
        if action == "lots:disable_all":
            for cfg in lots.values():
                cfg["enabled"] = False
            await _save_settings(settings)
            await call.answer("Все лоты выключены")
            return await self._show_lots(call)
        if action == "lots:add":
            await call.answer(
                "Добавьте лот через settings.json: lots → offer_id → {stars, price, enabled}",
                show_alert=True,
            )
            return True
        return False

    async def _show_mini(self, call: CallbackQuery) -> bool:
        settings = await _get_settings()
        mode = settings.get("queue_mode") or "strict"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [self._btn(f"{'🟢' if settings.get('back_priority') else '🔴'} Приоритет !бэк", "mini:toggle:back_priority")],
            [self._btn(f"📋 Очередь: {QUEUE_MODE_LABELS.get(mode, mode)}", "mini:cycle:queue_mode")],
            [self._btn(f"{'🟢' if settings.get('auto_send_without_plus') else '🔴'} Без '+'", "mini:toggle:auto_send_without_plus")],
            [self._btn(f"{'🟢' if settings.get('check_username') else '🔴'} Проверка @username", "mini:toggle:check_username")],
            [self._btn(f"{'🟢' if settings.get('liteserver_retry') else '🔴'} LiteServer-ретрай", "mini:toggle:liteserver_retry")],
            [self._btn(f"{'🟢' if settings.get('usdt_fallback_to_ton') else '🔴'} USDT→TON fallback", "mini:toggle:usdt_fallback_to_ton")],
            [self._btn("◀️ Назад", "refresh")],
        ])
        await call.message.edit_text(
            f"⚙️ <b>Мини-настройки</b>\n\n"
            f"Таймаут очереди: <b>{settings.get('queue_timeout_sec')}с</b>\n"
            f"Мин. баланс TON: <b>{settings.get('min_balance_ton')}</b>\n"
            f"Мин. баланс USDT: <b>{settings.get('min_balance_usdt')}</b>",
            parse_mode="HTML", reply_markup=kb,
        )
        await call.answer()
        return True

    async def _mini_action(self, call: CallbackQuery, action: str) -> bool:
        settings = await _get_settings()
        if action.startswith("mini:toggle:"):
            key = action.split(":", 2)[2]
            settings[key] = not bool(settings.get(key))
            await _save_settings(settings)
            return await self._show_mini(call)
        if action == "mini:cycle:queue_mode":
            modes = list(QUEUE_MODES)
            cur = settings.get("queue_mode") or "strict"
            idx = modes.index(cur) if cur in modes else 0
            settings["queue_mode"] = modes[(idx + 1) % len(modes)]
            await _save_settings(settings)
            return await self._show_mini(call)
        return False

    async def _show_templates(self, call: CallbackQuery) -> bool:
        settings = await _get_settings()
        templates = settings.get("templates") or {}
        lines = ["💬 <b>Шаблоны сообщений</b>\n"]
        for key in DEFAULT_TEMPLATES:
            preview = (templates.get(key) or "")[:40].replace("\n", " ")
            lines.append(f"• <b>{key}</b>: {html.escape(preview)}…")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [self._btn("🔄 Сбросить все", "tpl:reset")],
            [self._btn("◀️ Назад", "refresh")],
        ])
        await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        await call.answer()
        return True

    async def _tpl_action(self, call: CallbackQuery, action: str) -> bool:
        if action == "tpl:reset":
            settings = await _get_settings()
            settings["templates"] = copy.deepcopy(DEFAULT_TEMPLATES)
            await _save_settings(settings)
            await call.answer("Шаблоны сброшены")
            return await self._show_templates(call)
        return False

    async def _show_pricing(self, call: CallbackQuery) -> bool:
        settings = await _get_settings()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [self._btn(
                f"{'🟢' if settings.get('autoprice_enabled') else '🔴'} Автоцены",
                "mini:toggle:autoprice_enabled",
            )],
            [self._btn(
                f"{'🟢' if settings.get('autodump_enabled') else '🔴'} Автодемп",
                "mini:toggle:autodump_enabled",
            )],
            [self._btn(f"📈 Наценка: {settings.get('markup_percent')}%", "pricing:markup")],
            [self._btn("◀️ Назад", "refresh")],
        ])
        await call.message.edit_text(
            f"💰 <b>Ценообразование</b>\n\n"
            f"Наценка: <b>{settings.get('markup_percent')}%</b>\n"
            f"Шаг демпа: <b>{FTS_AUTODUMP_STEP_RUB} ₽</b>\n"
            f"Мин. цена демпа: <b>{settings.get('autodump_min_price')} ₽</b>",
            parse_mode="HTML", reply_markup=kb,
        )
        await call.answer()
        return True

    async def _show_stats(self, call: CallbackQuery) -> bool:
        settings = await _get_settings()
        stats = settings.get("stats") or {}
        conv = Statistics.conversion(stats)
        lines = [
            f"📊 <b>Статистика FTS-Starwell</b>\n",
            f"📦 Заказов: <b>{stats.get('total_orders', 0)}</b>",
            f"✅ Выполнено: <b>{stats.get('completed', 0)}</b> ({conv}%)",
            f"❌ Ошибок: <b>{stats.get('failed', 0)}</b>",
            f"💸 Возвратов: <b>{stats.get('refunded', 0)}</b>",
            f"⭐ Stars отправлено: <b>{stats.get('stars_sent', 0)}</b>",
            f"💵 Выручка: <b>{_format_rub(stats.get('revenue_rub', 0))}</b>",
            f"💎 Расход TON: <b>{stats.get('cost_ton', 0):.4f}</b>\n",
            "<b>Топ покупателей:</b>",
        ]
        for buyer, cnt in Statistics.top_buyers(stats):
            lines.append(f"• {html.escape(buyer)} — {cnt}")
        lines.append("\n<b>Главные ошибки:</b>")
        for err, cnt in Statistics.top_errors(stats):
            lines.append(f"• {html.escape(err)} — {cnt}")
        kb = InlineKeyboardMarkup(inline_keyboard=[[self._btn("◀️ Назад", "refresh")]])
        await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        await call.answer()
        return True

    async def _show_logs(self, call: CallbackQuery) -> bool:
        tail = await self.p.hlog.read_tail(60)
        kb = InlineKeyboardMarkup(inline_keyboard=[[self._btn("◀️ Назад", "refresh")]])
        await call.message.edit_text(f"📜 <b>Логи</b>\n\n<pre>{tail}</pre>", parse_mode="HTML", reply_markup=kb)
        await call.answer()
        return True

    async def _show_export(self, call: CallbackQuery) -> bool:
        settings = await _get_settings()
        export_data = {k: settings.get(k) for k in DEFAULT_SETTINGS if k not in ("api_token", "fragment_jwt")}
        payload = json.dumps(export_data, ensure_ascii=False, indent=2)
        if len(payload) > 3500:
            payload = payload[:3500] + "\n…"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [self._btn("◀️ Назад", "refresh")],
        ])
        await call.message.edit_text(
            f"📤 <b>Экспорт settings</b> (без JWT)\n\n<pre>{html.escape(payload)}</pre>",
            parse_mode="HTML", reply_markup=kb,
        )
        await call.answer()
        return True


# ═══════════════════════════════════════════════════════════════════════════════
#  Plugin class (Starvell)
# ═══════════════════════════════════════════════════════════════════════════════


class Plugin(StarvellPlugin):
    """class Plugin — обязательное имя для движка Starvell."""

    NAME = NAME
    UUID = UUID
    VERSION = VERSION
    DESCRIPTION = DESCRIPTION
    CREDITS = CREDITS
    SETTINGS_PAGE = SETTINGS_PAGE
    TELEGRAM_COMMANDS = TELEGRAM_COMMANDS

    def on_load(self) -> None:
        self.hlog = _HumanLog(_storage_dir() / "log.txt")
        self.processor = OrderProcessor(self)
        self.workers = BackgroundWorkers(self)
        self.tg_ui = TelegramUI(self)
        self._fragment_hub: FragmentApiHub | None = None
        self._fragment_token: str = ""
        self.log("%s v%s loaded", NAME, VERSION)

    async def get_fragment_hub(self) -> FragmentApiHub | None:
        settings = await _get_settings()
        token = _api_token(settings)
        if not token:
            if self._fragment_hub:
                await self._fragment_hub.aclose()
            self._fragment_hub = None
            self._fragment_token = ""
            return None
        if self._fragment_hub and self._fragment_token == token:
            return self._fragment_hub
        if self._fragment_hub:
            await self._fragment_hub.aclose()
        self._fragment_hub = FragmentApiHub(token)
        self._fragment_token = token
        return self._fragment_hub

    async def on_startup(self) -> None:
        await self.hlog.start()
        await self.workers.start()
        hub = await self.get_fragment_hub()
        if hub:
            asyncio.create_task(hub.warm_cache())
        self.hlog.info("%s v%s started", NAME, VERSION)

    async def on_shutdown(self) -> None:
        await self.workers.stop()
        if self._fragment_hub:
            await self._fragment_hub.aclose()
        await self.hlog.stop()

    def get_settings_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "api_token", "label": "Fragment API токен", "type": "multiline", "default": ""},
            {"key": "plugin_enabled", "label": "Плагин вкл", "type": "bool", "default": True},
            {"key": "lots_enabled", "label": "Лоты вкл", "type": "bool", "default": True},
            {"key": "auto_refund", "label": "Автовозврат", "type": "bool", "default": True},
            {"key": "auto_deactivate", "label": "Автодеактивация", "type": "bool", "default": True},
            {"key": "markup_percent", "label": "Наценка %", "type": "int", "default": 15},
            {"key": "queue_mode", "label": "Режим очереди", "type": "select", "default": "strict",
             "options": list(QUEUE_MODES)},
            {"key": "queue_timeout_sec", "label": "Таймаут очереди (сек)", "type": "int", "default": 300},
            {"key": "min_balance_ton", "label": "Мин. TON", "type": "text", "default": "0.5"},
            {"key": "min_balance_usdt", "label": "Мин. USDT", "type": "text", "default": "1.0"},
            {"key": "category_url", "label": "URL категории (автодемп)", "type": "text", "default": ""},
            {"key": "autoprice_enabled", "label": "Автоцены", "type": "bool", "default": False},
            {"key": "autodump_enabled", "label": "Автодемп", "type": "bool", "default": False},
        ]

    async def on_setting_change(self, key: str, value: Any) -> None:
        settings = await _get_settings()
        if key in ("api_token", "fragment_jwt"):
            settings["api_token"] = str(value or "").strip()
            settings.pop("fragment_jwt", None)
        else:
            settings[key] = value
        await _save_settings(settings)
        if key in ("api_token", "fragment_jwt"):
            if self._fragment_hub:
                await self._fragment_hub.aclose()
            self._fragment_hub = None
            self._fragment_token = ""
            hub = await self.get_fragment_hub()
            if hub:
                asyncio.create_task(hub.warm_cache())
        self.hlog.info("Настройка %s изменена", key)

    async def render_plugin_panel(self) -> tuple[str, InlineKeyboardMarkup]:
        return await self.tg_ui.render_main()

    async def on_panel_action(self, call: CallbackQuery, action: str) -> bool:
        if action in ("token:validate", "jwt:validate"):
            hub = await self.get_fragment_hub()
            if not hub:
                await call.answer("API токен не задан", show_alert=True)
                return True
            ok, err, _wallet = await hub.validate_token()
            await call.answer("✅ Баланс получен" if ok else err, show_alert=not ok)
            return await self.tg_ui._show_token(call)
        return await self.tg_ui.handle_callback(call, action)

    async def on_telegram_command(self, call, command: str) -> bool:
        if command == "fts":
            text, kb = await self.render_plugin_panel()
            try:
                await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
            return True
        if command == "fts_balance":
            hub = await self.get_fragment_hub()
            if not hub:
                await call.message.answer("❌ Задайте Fragment API токен в настройках плагина")
                return True
            w = await hub.get_wallet(force=True)
            if w.valid:
                await call.message.answer(
                    f"💎 Fragment кошелёк\n"
                    f"TON <b>{w.ton_balance:.4f}</b> · USDT <b>{w.usdt_balance:.2f}</b>\n"
                    f"Версия: <code>{html.escape(w.wallet_version or '—')}</code>",
                    parse_mode="HTML",
                )
            else:
                await call.message.answer(f"❌ {html.escape(w.error or 'кошелёк недоступен')}")
            return True
        if command == "fts_stats":
            settings = await _get_settings()
            stats = settings.get("stats") or {}
            await call.message.answer(
                f"📊 Заказов: {stats.get('total_orders', 0)} · "
                f"✅ {stats.get('completed', 0)} · ⭐ {stats.get('stars_sent', 0)} · "
                f"💵 {_format_rub(stats.get('revenue_rub', 0))}",
                parse_mode="HTML",
            )
            return True
        return False

    @on_pre_delivery
    async def cancel_autodelivery(self, ctx: DeliveryContext) -> None:
        settings = await _get_settings()
        if not settings.get("plugin_enabled"):
            return
        offer_id = _offer_id_from_order(ctx.order or {})
        lots = settings.get("lots") or {}
        if lots and offer_id and str(offer_id) in lots:
            ctx.cancel()
            self.hlog.debug("Автовыдача Starvell отменена для Stars-лота %s", offer_id)

    @on_order_paid
    async def on_paid(self, ctx: OrderContext) -> None:
        try:
            if not ctx.chat_id and ctx.buyer_id:
                api = self.core.get_api()
                if api:
                    ctx.chat_id = await api.find_chat_by_buyer(int(ctx.buyer_id))
            await self.processor.handle_new_order(ctx)
        except Exception as exc:
            self.hlog.exception("on_order_paid #%s: %s", ctx.order_id, exc)

    @on_message
    async def on_buyer_message(self, ctx: MessageContext) -> None:
        try:
            await self.processor.handle_message(ctx)
        except Exception as exc:
            self.hlog.exception("on_message: %s", exc)

    @on_order_completed
    async def on_completed(self, ctx: OrderContext) -> None:
        self.hlog.info("Заказ #%s завершён на Starvell", ctx.order_id)
