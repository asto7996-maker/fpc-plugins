"""
VexBoost AutoSMM — нативный плагин Starvell Cardinal.

Лот на Starvell должен содержать в описании:
  ID: 1234
  #Quan: 1   (опционально, множитель количества)

Покупатель отправляет ссылку → подтверждает + → заказ уходит в VexBoost API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from aiogram.types import InlineKeyboardMarkup

from config import BASE_DIR
from starvell_sdk import MessageContext, OrderContext, StarvellPlugin, on_message, on_order_paid

NAME = "VexBoost AutoSMM"
VERSION = "3.0.0"
DESCRIPTION = "Автонакрутка SMM через VexBoost для Starvell"
CREDITS = "Starvell Cardinal"
UUID = "a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e"
SETTINGS_PAGE = True

TELEGRAM_COMMANDS = [
    {"command": "vexboost", "description": "панель VexBoost AutoSMM"},
    {"command": "vb_stats", "description": "статистика VexBoost AutoSMM"},
    {"command": "vb_balance", "description": "баланс VexBoost AutoSMM"},
]

SERVICE_ID_RE = re.compile(r"ID:\s*(\d+)", re.IGNORECASE)
QUAN_RE = re.compile(r"#Quan:\s*(\d+)", re.IGNORECASE)
LINK_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
CONFIRM_RE = re.compile(r"^[+\-✅❌]$|^(да|нет|yes|no)$", re.IGNORECASE)

logger = logging.getLogger("starvell.plugin.vexboost")


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


class VexBoostAPI:
    """Стандартный SMM API v2 (action=add/status/balance/refill/cancel)."""

    def __init__(self, api_url: str, api_key: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {**payload, "key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(self.api_url, data=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("VexBoost API: %s", exc)
            return {"error": str(exc)}
        if isinstance(data, dict) and data.get("error"):
            return {"error": data["error"]}
        return data if isinstance(data, dict) else {"raw": data}

    async def balance(self) -> tuple[float | None, str, str]:
        data = await self._request({"action": "balance"})
        if "error" in data:
            return None, "", str(data["error"])
        raw = str(data.get("balance", "0"))
        m = re.search(r"[\d.]+", raw)
        if not m:
            return None, "", "Неверный ответ баланса"
        return float(m.group()), data.get("currency", "RUB"), ""

    async def create_order(self, service_id: int, link: str, quantity: int) -> int | str:
        data = await self._request({
            "action": "add",
            "service": service_id,
            "link": link,
            "quantity": quantity,
        })
        if "order" in data:
            return int(data["order"])
        return data.get("error", "Неизвестная ошибка")

    async def status(self, order_id: int) -> dict[str, Any] | None:
        data = await self._request({"action": "status", "order": order_id})
        if "error" in data:
            return None
        return data

    async def refill(self, order_id: int) -> bool:
        data = await self._request({"action": "refill", "order": order_id})
        return "refill" in data

    async def cancel(self, order_id: int) -> bool:
        data = await self._request({"action": "cancel", "order": order_id})
        return bool(data.get("cancel"))


class Plugin(StarvellPlugin):
    """VexBoost AutoSMM для Starvell."""

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

    async def on_startup(self) -> None:
        self._status_task = asyncio.create_task(self._status_loop())
        self.log("VexBoost AutoSMM v%s запущен", VERSION)

    async def on_shutdown(self) -> None:
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass

    def get_settings_schema(self) -> list[dict]:
        return [
            {"key": "enabled", "label": "Авто-SMM", "type": "bool", "default": True},
            {"key": "api_url", "label": "API URL", "type": "text", "default": "https://vexboost.ru/api/v2"},
            {"key": "api_key", "label": "API Key", "type": "text", "default": ""},
            {"key": "status_interval", "label": "Интервал проверки (сек)", "type": "int", "default": 60, "min": 30, "max": 600},
            {"key": "allow_private_tg", "label": "Приватные TG-ссылки", "type": "bool", "default": False},
            {"key": "notify_admin", "label": "Уведомления в TG", "type": "bool", "default": True},
        ]

    async def _api(self) -> VexBoostAPI | None:
        key = str(await self.get_cfg("api_key", "")).strip()
        if not key:
            return None
        url = str(await self.get_cfg("api_url", "https://vexboost.ru/api/v2"))
        return VexBoostAPI(url, key)

    async def _enabled(self) -> bool:
        return bool(await self.get_cfg("enabled", True))

    async def _msg(self, key: str, default: str, **kwargs) -> str:
        tpl = await self.get_cfg(key, default)
        try:
            return str(tpl).format(**kwargs)
        except Exception:
            return str(tpl)

    # ── Заказы Starvell ───────────────────────────────────────────────────

    @on_order_paid
    async def on_paid(self, ctx: OrderContext) -> None:
        if not await self._enabled():
            return
        api = await self._api()
        if not api:
            self.log("API key не задан", level="warning")
            return

        desc = _order_description(ctx.order)
        m = SERVICE_ID_RE.search(desc)
        if not m:
            return

        service_id = int(m.group(1))
        mult = 1
        qm = QUAN_RE.search(desc)
        if qm:
            mult = max(1, int(qm.group(1)))
        quantity = ctx.quantity * mult

        order_id = ctx.order_id
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
            "price": ctx.price,
            "created_at": int(time.time()),
        }
        if not entry["chat_id"] and ctx.buyer_id:
            starvell_api = ctx.api()
            if starvell_api:
                entry["chat_id"] = await starvell_api.find_chat_by_buyer(int(ctx.buyer_id)) or ""

        waiting.append(entry)
        _save_json("waiting.json", waiting)

        welcome = await self._msg(
            "welcome_message",
            "👋 Спасибо за заказ!\nОтправьте ссылку на аккаунт или пост для накрутки.",
            order_id=order_id,
        )
        if entry["chat_id"]:
            await ctx.send_to_buyer(welcome)
        else:
            await ctx.notify(f"⚠️ VexBoost: заказ #{order_id} — чат не найден", "notify_orders")

        if await self.get_cfg("notify_admin", True):
            await ctx.notify(
                f"📥 <b>VexBoost</b> заказ #{order_id}\n"
                f"Service ID: <code>{service_id}</code>\n"
                f"Кол-во: {quantity}\n"
                f"Покупатель: {ctx.buyer_username}",
                "notify_orders",
                order_id=order_id,
            )
        self.log("Ожидание ссылки #%s service=%s qty=%s", order_id, service_id, quantity)

    # ── Сообщения покупателя ──────────────────────────────────────────────

    @on_message
    async def on_buyer_message(self, ctx: MessageContext) -> None:
        if not await self._enabled():
            return
        text = (ctx.text or "").strip()
        if not text:
            return

        low = text.lower()
        if low.startswith("#статус"):
            await self._cmd_status(ctx, text)
            return
        if low.startswith("#рефилл") or low.startswith("#refill"):
            await self._cmd_refill(ctx, text)
            return

        pending = _load_json("pending.json", {})
        pkey = f"{ctx.chat_id}:{ctx.username}"
        if pkey in pending or ctx.username in {p.get("buyer") for p in pending.values()}:
            await self._handle_pending(ctx, text)
            return

        waiting = _load_json("waiting.json", [])
        order = next((o for o in waiting if o.get("buyer") == ctx.username), None)
        if not order:
            order = next((o for o in waiting if str(o.get("chat_id")) == str(ctx.chat_id)), None)
        if not order:
            return

        links = _extract_links(text)
        if not links:
            return

        link = links[0]
        if not await self.get_cfg("allow_private_tg", False) and _is_private_tg(link):
            await ctx.reply(
                "❌ Закрытые Telegram-каналы не поддерживаются.\n"
                "Используйте публичную ссылку: https://t.me/your_channel"
            )
            return

        order["link"] = link
        order["chat_id"] = ctx.chat_id
        pending[pkey] = order
        _save_json("pending.json", pending)

        confirm = await self._msg(
            "confirmation_message",
            "📋 Проверьте заказ:\n\n"
            "🛒 {product}\n🔢 Количество: {qty} шт.\n🔗 {link}\n\n"
            "✅ + подтвердить\n❌ - отменить",
            product=order.get("product", "товар"),
            qty=order.get("quantity", 1),
            link=link.replace("https://", "").replace("http://", ""),
        )
        await ctx.reply(confirm)

    async def _handle_pending(self, ctx: MessageContext, text: str) -> None:
        pending = _load_json("pending.json", {})
        pkey = f"{ctx.chat_id}:{ctx.username}"
        order = pending.get(pkey)
        if not order:
            for k, v in list(pending.items()):
                if v.get("buyer") == ctx.username:
                    pkey, order = k, v
                    break
        if not order:
            return

        action = text.strip()
        if action in ("+", "✅", "да", "yes"):
            pending.pop(pkey, None)
            _save_json("pending.json", pending)
            await self._submit_order(ctx, order)
            return

        if action in ("-", "❌", "нет", "no"):
            pending.pop(pkey, None)
            _save_json("pending.json", pending)
            self._remove_waiting(order.get("order_id"))
            await ctx.reply(await self._msg("cancel_message", "❌ Заказ отменён."))
            return

        links = _extract_links(text)
        if links:
            order["link"] = links[0]
            pending[pkey] = order
            _save_json("pending.json", pending)
            await ctx.reply("🔗 Ссылка обновлена. Отправьте + для подтверждения.")
            return

        await ctx.reply("Отправьте + для подтверждения, - для отмены или новую ссылку.")

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

        await ctx.reply(await self._msg("creating_message", "⏳ Создаю заказ, подождите..."))

        result = await api.create_order(
            int(order["service_id"]),
            str(order["link"]),
            int(order["quantity"]),
        )

        if isinstance(result, int):
            smm_id = result
            submitted[oid] = smm_id
            _save_json("submitted.json", submitted)

            active = _load_json("active.json", {})
            active[str(smm_id)] = {
                **order,
                "smm_id": smm_id,
                "status": "Pending",
                "created_at": int(time.time()),
            }
            _save_json("active.json", active)
            self._remove_waiting(oid)

            stats = _load_json("stats.json", {"created": 0, "completed": 0, "failed": 0})
            stats["created"] = int(stats.get("created", 0)) + 1
            _save_json("stats.json", stats)

            msg = await self._msg(
                "created_message",
                "📊 Заказ создан!\n🆔 SMM ID: {smm_id}\n\n"
                "#статус {smm_id}\n#рефилл {smm_id}",
                smm_id=smm_id,
            )
            await ctx.reply(msg)

            if await self.get_cfg("notify_admin", True):
                await ctx.notify(
                    f"✅ VexBoost #{smm_id} создан (Starvell #{oid})",
                    "notify_orders",
                    order_id=oid,
                )
        else:
            await ctx.reply(f"❌ Ошибка: {result}")
            stats = _load_json("stats.json", {"created": 0, "completed": 0, "failed": 0})
            stats["failed"] = int(stats.get("failed", 0)) + 1
            _save_json("stats.json", stats)

        self._locks.discard(oid)

    def _remove_waiting(self, order_id: str | None) -> None:
        if not order_id:
            return
        waiting = _load_json("waiting.json", [])
        waiting = [o for o in waiting if str(o.get("order_id")) != str(order_id)]
        _save_json("waiting.json", waiting)

    async def _cmd_status(self, ctx: MessageContext, text: str) -> None:
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await ctx.reply("Использование: #статус ID")
            return
        api = await self._api()
        if not api:
            await ctx.reply("❌ API не настроен")
            return
        data = await api.status(int(parts[1]))
        if not data:
            await ctx.reply("🔴 Не удалось получить статус")
            return
        await ctx.reply(
            f"📈 Статус #{parts[1]}\n"
            f"Статус: {data.get('status', '?')}\n"
            f"Было: {data.get('start_count', '—')}\n"
            f"Остаток: {data.get('remains', '—')}"
        )

    async def _cmd_refill(self, ctx: MessageContext, text: str) -> None:
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await ctx.reply("Использование: #рефилл ID")
            return
        api = await self._api()
        if not api:
            await ctx.reply("❌ API не настроен")
            return
        ok = await api.refill(int(parts[1]))
        await ctx.reply("✅ Рефилл отправлен" if ok else "🔴 Рефилл недоступен")

    # ── Фоновая проверка статусов ─────────────────────────────────────────

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

        done_msg = await self.get_cfg(
            "completed_message",
            "✅ Заказ #{order_id} выполнен! Спасибо за покупку! 🙏",
        )

        for smm_id, info in list(active.items()):
            data = await api.status(int(smm_id))
            if not data:
                continue
            status = str(data.get("status", ""))
            info["status"] = status
            active[smm_id] = info

            if status.lower() not in ("completed", "canceled", "cancelled", "partial"):
                continue

            chat_id = str(info.get("chat_id") or "")
            starvell_id = str(info.get("order_id", ""))
            starvell_api = self.core.get_api()

            if status.lower() == "completed" and chat_id and starvell_api:
                try:
                    await starvell_api.send_message(
                        chat_id,
                        str(done_msg).format(order_id=starvell_id, smm_id=smm_id),
                    )
                except Exception as exc:
                    self.log("notify complete: %s", exc, level="warning")

                stats = _load_json("stats.json", {"created": 0, "completed": 0, "failed": 0})
                stats["completed"] = int(stats.get("completed", 0)) + 1
                _save_json("stats.json", stats)

                if await self.get_cfg("notify_admin", True):
                    await self.core.notify(
                        f"✅ VexBoost #{smm_id} завершён (Starvell #{starvell_id})",
                        "notify_orders",
                    )
                del active[smm_id]

            elif status.lower() in ("canceled", "cancelled"):
                if chat_id and starvell_api:
                    try:
                        await starvell_api.send_message(
                            chat_id,
                            f"❌ SMM-заказ #{smm_id} отменён. Обратитесь к продавцу.",
                        )
                    except Exception:
                        pass
                del active[smm_id]

        _save_json("active.json", active)

    # ── Telegram панель (FPC) ─────────────────────────────────────────────

    async def render_plugin_card_extras(self) -> str:
        stats = _load_json("stats.json", {})
        active = _load_json("active.json", {})
        waiting = _load_json("waiting.json", [])
        api = await self._api()
        bal_line = "API: ❌ ключ не задан"
        if api:
            bal, cur, err = await api.balance()
            if bal is not None:
                bal_line = f"💰 Баланс: <b>{bal:.2f}</b> {cur}"
            else:
                bal_line = f"💰 Баланс: ⚠️ {err}"
        return (
            f"{bal_line}\n"
            f"📋 Активных SMM: <b>{len(active)}</b>\n"
            f"⏳ Ожидают ссылку: <b>{len(waiting)}</b>\n"
            f"📊 Создано/готово: {stats.get('created', 0)}/{stats.get('completed', 0)}"
        )

    async def render_plugin_panel(self) -> tuple[str, InlineKeyboardMarkup]:
        stats = _load_json("stats.json", {"created": 0, "completed": 0, "failed": 0})
        active = _load_json("active.json", {})
        waiting = _load_json("waiting.json", [])
        enabled = await self._enabled()
        api = await self._api()

        bal_txt = "—"
        if api:
            bal, cur, err = await api.balance()
            bal_txt = f"{bal:.2f} {cur}" if bal is not None else f"ошибка: {err}"

        text = (
            f"📊 <b>{NAME}</b> v{VERSION}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{'🟢' if enabled else '🔴'} Плагин: {'включён' if enabled else 'выключен'}\n"
            f"💰 Баланс VexBoost: <code>{bal_txt}</code>\n"
            f"📋 Активные SMM: <b>{len(active)}</b>\n"
            f"⏳ Ожидают ссылку: <b>{len(waiting)}</b>\n"
            f"✅ Создано: {stats.get('created', 0)} · Готово: {stats.get('completed', 0)} · Ошибок: {stats.get('failed', 0)}\n\n"
            "<i>Лот должен содержать <code>ID: 1234</code> в описании.</i>"
        )
        rows = [
            [self.panel_btn("⚙️ Настройки", self.UUID, "settings")],
            [self.panel_btn("📋 Активные заказы", self.UUID, "active")],
            [self.panel_btn("⏳ Ожидают ссылку", self.UUID, "waiting")],
            [self.panel_btn("💰 Баланс", self.UUID, "balance")],
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
                await call.answer("API key не задан", show_alert=True)
                return True
            bal, cur, err = await api.balance()
            if bal is not None:
                await call.answer(f"Баланс: {bal:.2f} {cur}", show_alert=True)
            else:
                await call.answer(f"Ошибка: {err}", show_alert=True)
            return True
        if action == "active":
            active = _load_json("active.json", {})
            lines = [f"<b>Активные SMM ({len(active)})</b>\n"]
            if not active:
                lines.append("<i>Пусто</i>")
            else:
                for sid, info in list(active.items())[:15]:
                    lines.append(
                        f"• #{sid} → Starvell #{info.get('order_id')} "
                        f"({info.get('status', '?')})"
                    )
            await call.message.answer("\n".join(lines), parse_mode="HTML")
            await call.answer()
            return True
        if action == "waiting":
            waiting = _load_json("waiting.json", [])
            lines = [f"<b>Ожидают ссылку ({len(waiting)})</b>\n"]
            if not waiting:
                lines.append("<i>Пусто</i>")
            else:
                for o in waiting[:15]:
                    lines.append(
                        f"• #{o.get('order_id')} — {o.get('buyer')} "
                        f"(service {o.get('service_id')}, qty {o.get('quantity')})"
                    )
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
                await call.message.answer("❌ API key не задан в настройках плагина")
                return True
            bal, cur, err = await api.balance()
            if bal is not None:
                await call.message.answer(f"💰 Баланс VexBoost: <b>{bal:.2f}</b> {cur}", parse_mode="HTML")
            else:
                await call.message.answer(f"❌ {err}")
            return True
        if command == "vb_stats":
            stats = _load_json("stats.json", {})
            active = _load_json("active.json", {})
            waiting = _load_json("waiting.json", [])
            await call.message.answer(
                f"📊 <b>Статистика VexBoost</b>\n"
                f"Создано: {stats.get('created', 0)}\n"
                f"Завершено: {stats.get('completed', 0)}\n"
                f"Ошибок: {stats.get('failed', 0)}\n"
                f"Активных: {len(active)}\n"
                f"Ожидают ссылку: {len(waiting)}",
                parse_mode="HTML",
            )
            return True
        return False
