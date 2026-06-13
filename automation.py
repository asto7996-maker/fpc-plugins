"""
Фоновая автоматизация: заказы, чаты, бамп, автовыдача, ИИ, отзывы.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Callable, Awaitable

from ai_service import AIService
from config import Settings, load_settings
from database import Database
from handlers.builtin import BuiltinHandlers
from core.delivery.templates import append_refund_disclaimer, render_delivery_template
from core.plugins.context import (
    BumpContext,
    DeliveryContext,
    MessageContext,
    OrderContext,
    _resolve_order_price,
)
from core.plugins.hooks import STV_BUMP, STV_MESSAGE, STV_ORDER_COMPLETED, STV_ORDER_PAID, STV_ORDER_STATUS, STV_POST_DELIVERY, STV_PRE_DELIVERY
from core.security.payment_guard import PaymentGuard
from plugin_manager import PluginContext
from starvell_api import StarvellAPI

logger = logging.getLogger("starvell.automation")

# Статусы заказа, при которых покупатель уже оплатил — приветствие не нужно
_OPEN_ORDER_STATUSES = frozenset({"CREATED", "PAID", "IN_PROGRESS", "CONFIRMED", "PENDING"})

# Текстовые маркеры системных уведомлений Starvell (не от покупателя)
_PLATFORM_SYSTEM_MARKERS = (
    "вернул деньги покупателю",
    "вернул средства покупателю",
    "покупатель подтвердил",
    "продавец подтвердил",
    "сделка завершена",
    "ожидает подтверждения покупателем",
)


def _is_platform_system_message(msg: dict, text: str, settings: Settings) -> bool:
    """Сообщение платформы / системное — не от покупателя."""
    if not settings.greetings_ignore_system:
        return False
    if msg.get("isSystem") or msg.get("system") or msg.get("isService"):
        return True
    msg_type = str(msg.get("type") or msg.get("messageType") or "").upper()
    if msg_type in ("SYSTEM", "SERVICE", "ORDER", "NOTIFICATION"):
        return True
    if msg.get("orderId") or msg.get("order_id"):
        return True
    author_id = msg.get("authorId") or msg.get("author")
    if author_id is None and text:
        return True
    low = text.lower()
    return any(marker in low for marker in _PLATFORM_SYSTEM_MARKERS)


def _buyer_has_open_order(
    orders: list[dict],
    author_id: int | None,
    username: str,
) -> bool:
    """Покупатель уже оплатил заказ — приветствие не отправляем."""
    uname = (username or "").strip().lower()
    for order in orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("status") or "") not in _OPEN_ORDER_STATUSES:
            continue
        buyer = order.get("user") or {}
        if author_id is not None and buyer.get("id") == author_id:
            return True
        if uname and str(buyer.get("username") or "").lower() == uname:
            return True
    return False


def _chat_has_new_activity(
    chat: dict,
    last_notified_id: str | None,
    my_user_id: int | None,
) -> bool:
    """Пропускаем чат, если новых сообщений покупателя нет."""
    if not last_notified_id:
        return True
    for key in ("unreadCount", "unread", "unreadMessagesCount", "hasUnread"):
        val = chat.get(key)
        if val and int(val) > 0:
            return True
    last_msg = chat.get("lastMessage") or chat.get("last_message")
    if not isinstance(last_msg, dict):
        return True
    mid = _message_id(last_msg)
    if not mid:
        return True
    if mid == last_notified_id:
        return False
    author = last_msg.get("authorId") or last_msg.get("author")
    if my_user_id and author == my_user_id:
        return False
    return True


def _message_id(msg: dict) -> str:
    return str(msg.get("id") or msg.get("messageId") or "")


def _message_ts(msg: dict) -> int:
    for key in ("createdAt", "created_at", "sentAt", "sent_at", "timestamp", "date"):
        val = msg.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            ts = int(val)
            return ts // 1000 if ts > 10_000_000_000 else ts
        if isinstance(val, str) and val.isdigit():
            ts = int(val)
            return ts // 1000 if ts > 10_000_000_000 else ts
    return 0


class AutomationEngine:
    """Движок фоновых задач для одного или нескольких аккаунтов Starvell."""

    def __init__(
        self,
        db: Database,
        cardinal: Any,
        notify_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self.cardinal = cardinal
        self.notify_cb = notify_cb
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._apis: dict[str, StarvellAPI] = {}
        self._ai = AIService(load_settings())
        self._handlers = BuiltinHandlers(cardinal)
        self._payment_guard = PaymentGuard()
        self._open_orders_cache: dict[str, tuple[float, list[dict]]] = {}
        self._user_id_cache: dict[str, tuple[float, int | None]] = {}
        self._OPEN_ORDERS_TTL = 30.0
        self._USER_ID_TTL = 90.0

    async def notify(self, text: str, notify_type: str = "notify_orders", **extra) -> None:
        if self.notify_cb:
            try:
                await self.notify_cb(text, notify_type, **extra)
            except TypeError:
                try:
                    await self.notify_cb(text, notify_type)
                except TypeError:
                    await self.notify_cb(text)
            except Exception as exc:
                logger.warning("notify failed: %s", exc)

    def _plugin_engine(self) -> Any | None:
        return getattr(self.cardinal, "plugin_manager", None)

    async def _emit_starvell(self, event: str, ctx: Any) -> None:
        pe = self._plugin_engine()
        if pe and hasattr(pe, "emit_starvell"):
            await pe.emit_starvell(event, ctx)

    async def _build_order_ctx(self, account_name: str, order: dict) -> OrderContext:
        ctx = OrderContext.from_order(self.cardinal, order, account_name)
        api = self.cardinal.get_api(account_name)
        if api and ctx.buyer_id:
            ctx.chat_id = await api.find_chat_by_buyer(int(ctx.buyer_id))
        return ctx

    def _get_settings(self) -> Settings:
        return load_settings()

    def _build_api(self, account) -> StarvellAPI:
        settings = self._get_settings()
        try:
            from api.starvell_client import StarvellClient

            cls = StarvellClient
        except ImportError:
            cls = StarvellAPI
        return cls(
            session_cookie=account.session_cookie,
            sid_cookie=account.sid_cookie,
            my_games_cookie=account.my_games_cookie,
            delay_seconds=settings.api_delay_seconds,
            max_per_minute=settings.api_max_per_minute,
            account_name=account.name,
        )

    async def start(self) -> None:
        """Запускает все фоновые циклы."""
        if self._running:
            return
        self._running = True
        settings = self._get_settings()
        await self.db.sync_feature_flags(settings)

        accounts = settings.get_active_accounts()
        if not accounts:
            logger.warning("Нет активных аккаунтов Starvell — автоматизация ожидает настройки")
            return

        for account in accounts:
            api = self._build_api(account)
            self._apis[account.name] = api
            self.cardinal.register_api(account.name, api)
            self._tasks.append(asyncio.create_task(self._auth_loop(account.name, api)))
            self._tasks.append(asyncio.create_task(self._orders_loop(account.name, api)))
            self._tasks.append(asyncio.create_task(self._chats_loop(account.name, api)))
            if settings.auto_bump_enabled:
                self._tasks.append(asyncio.create_task(self._bump_loop(account.name, api)))

        logger.info("Автоматизация запущена для %d аккаунт(ов)", len(accounts))

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for api in self._apis.values():
            if hasattr(api, "aclose"):
                try:
                    await api.aclose()
                except Exception:
                    pass
        self._apis.clear()
        self._open_orders_cache.clear()
        self._user_id_cache.clear()

    async def reload(self) -> None:
        """Перезапускает фоновые задачи (после смены session cookie)."""
        await self.stop()
        self._ai = AIService(load_settings())
        await self.start()

    async def _get_cached_user_id(self, account_name: str, api: StarvellAPI) -> int | None:
        cached = self._user_id_cache.get(account_name)
        now = time.time()
        if cached and now - cached[0] < self._USER_ID_TTL:
            return cached[1]
        info = await api.fetch_homepage()
        uid = (info.get("user") or {}).get("id")
        self._user_id_cache[account_name] = (now, uid)
        return uid

    async def _get_cached_open_orders(self, account_name: str, api: StarvellAPI) -> list[dict]:
        cached = self._open_orders_cache.get(account_name)
        now = time.time()
        if cached and now - cached[0] < self._OPEN_ORDERS_TTL:
            return cached[1]
        try:
            orders = await api.fetch_orders()
        except Exception as exc:
            logger.debug("fetch_orders cache: %s", exc)
            orders = cached[1] if cached else []
        self._open_orders_cache[account_name] = (now, orders)
        return orders

    async def _auth_loop(self, account_name: str, api: StarvellAPI) -> None:
        """Периодическая проверка авторизации."""
        while self._running:
            settings = self._get_settings()
            try:
                info = await api.fetch_homepage()
                if not info.get("authorized"):
                    await self.notify(
                        f"❌ [{account_name}] Сессия Starvell недействительна! Обновите cookie в профиле бота.",
                        "notify_auth",
                    )
            except Exception as exc:
                logger.warning("auth_check %s: %s", account_name, exc)
            await asyncio.sleep(300)

    async def _orders_loop(self, account_name: str, api: StarvellAPI) -> None:
        """Мониторинг заказов: автовыдача и авто-отзывы."""
        while self._running:
            settings = self._get_settings()
            interval = max(3.0, settings.orders_poll_interval)
            try:
                await self._process_orders(account_name, api, settings)
            except Exception as exc:
                logger.exception("orders_loop %s: %s", account_name, exc)
            await asyncio.sleep(interval)

    async def _process_orders(self, account_name: str, api: StarvellAPI, settings: Settings) -> None:
        if not await self.db.is_orders_bootstrapped(account_name):
            await self._bootstrap_orders(account_name, api)

        orders = await api.fetch_orders()
        self._open_orders_cache[account_name] = (time.time(), orders)
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("id") or "")
            status = str(order.get("status") or "")
            if not order_id:
                continue

            # Новый оплаченный заказ (CREATED)
            if status == "CREATED":
                if await self.db.is_order_notified(order_id, account_name):
                    continue
                await self._handle_new_order(account_name, api, settings, order)
                await self.db.mark_order_notified(order_id, account_name)

            # Отслеживание смены статуса
            prev = await self.db.get_order_status(order_id, account_name)
            if prev is None:
                await self.db.set_order_status(order_id, status, account_name)
            elif prev != status:
                await self.db.set_order_status(order_id, status, account_name)
                order_ctx = await self._build_order_ctx(account_name, order)
                await self._emit_starvell(STV_ORDER_STATUS, order_ctx)
                if status == "COMPLETED":
                    if await self.db.get_feature_flag("auto_review", settings.auto_review_enabled):
                        await self._handle_completed_order(account_name, api, settings, order)
                    buyer_id = (order.get("user") or {}).get("id")
                    chat_id = await api.find_chat_by_buyer(int(buyer_id)) if buyer_id else None
                    await self._handlers.on_order_confirm(
                        order=order, api=api, settings=settings, chat_id=chat_id,
                    )

    async def _handle_new_order(
        self, account_name: str, api: StarvellAPI, settings: Settings, order: dict
    ) -> None:
        order_id = str(order.get("id"))
        buyer = (order.get("user") or {}).get("username") or "?"
        offer = order.get("offerDetails") or {}
        desc = (offer.get("descriptions") or {}).get("rus") or {}
        product_name = (
            str(desc.get("briefDescription") or "").strip()
            or str(desc.get("description") or "").strip()
            or "товар"
        )
        price = _resolve_order_price(order)

        await self.notify(
            f"🛒 <b>Новый заказ #{order_id}</b>\n"
            f"Покупатель: {buyer}\n"
            f"Товар: {product_name}\n"
            f"Сумма: {price} ₽",
            "notify_orders",
            order_id=order_id,
        )
        await self.cardinal.dispatch_plugins("BIND_TO_NEW_ORDER", order)
        await self.cardinal.events.emit("order:paid", {"order": order, "account": account_name})

        buyer_id = (order.get("user") or {}).get("id")
        if buyer_id:
            try:
                chat_id = await api.find_chat_by_buyer(int(buyer_id))
                if chat_id:
                    await self.db.mark_chat_welcomed(chat_id)
            except Exception:
                pass

        order_ctx = await self._build_order_ctx(account_name, order)
        await self._emit_starvell(STV_ORDER_PAID, order_ctx)

        # Плагины (legacy EventManager)
        ctx = PluginContext(api, self.db, settings, account_name)
        await self.cardinal.event_manager.dispatch("on_order", {"order": order, "ctx": ctx})

        if not await self.db.get_feature_flag("auto_delivery", settings.auto_delivery_enabled):
            return
        if await self.db.is_blacklisted(username=buyer, check="block_delivery"):
            return

        qty = max(1, int(order.get("quantity") or 1))
        codes: list[str] = []
        for _ in range(qty):
            code = await self.db.pop_autodelivery_item(product_name)
            if code:
                codes.append(code)

        if not codes:
            await self.notify(f"⚠️ Заказ #{order_id}: автовыдача — нет товара «{product_name}» на складе", "notify_delivery")
            return

        content = "\n".join(codes)
        delivery_text = render_delivery_template(
            settings.delivery_template,
            username=buyer,
            order_id=order_id,
            product_name=product_name,
            product=product_name,
            content=content,
            price=price,
            quantity=qty,
        )
        delivery_text = append_refund_disclaimer(delivery_text, strict=True)
        delivery_text = api.apply_watermark(delivery_text, settings.watermark_on, settings.watermark_text)

        delivery_ctx = DeliveryContext(
            core=self.cardinal,
            account_name=account_name,
            order=order,
            order_id=order_id,
            status=str(order.get("status") or ""),
            buyer_username=buyer,
            buyer_id=(order.get("user") or {}).get("id"),
            product_name=product_name,
            price=price,
            quantity=qty,
            chat_id=order_ctx.chat_id,
            delivery_text=delivery_text,
            codes=codes,
        )
        await self._emit_starvell(STV_PRE_DELIVERY, delivery_ctx)
        if delivery_ctx.skip_delivery:
            logger.info("Заказ #%s: автовыдача отменена плагином", order_id)
            return

        delivery_text = delivery_ctx.delivery_text or delivery_text
        buyer_id = delivery_ctx.buyer_id
        chat_id = delivery_ctx.chat_id
        if not chat_id and buyer_id:
            chat_id = await api.find_chat_by_buyer(int(buyer_id))
            delivery_ctx.chat_id = chat_id

        if chat_id:
            try:
                await api.send_message(chat_id, delivery_text)
                delivery_ctx.success = True
                await self.notify(f"✅ Заказ #{order_id} оплачен. Товар выдан успешно.", "notify_delivery")
            except Exception as exc:
                delivery_ctx.error = str(exc)
                await self.notify(f"❌ Заказ #{order_id}: ошибка выдачи — {exc}", "notify_delivery")
        else:
            delivery_ctx.error = "chat_not_found"
            await self.notify(f"⚠️ Заказ #{order_id}: товар готов, но чат с покупателем не найден", "notify_delivery")

        await self._emit_starvell(STV_POST_DELIVERY, delivery_ctx)

    async def _handle_completed_order(
        self, account_name: str, api: StarvellAPI, settings: Settings, order: dict
    ) -> None:
        order_id = str(order.get("id"))
        if await self.db.is_order_reviewed(order_id, account_name):
            return

        review_text = await self._handlers.on_order_completed(
            order=order, api=api, settings=settings, account_name=account_name
        )
        if not review_text:
            return

        buyer_id = (order.get("user") or {}).get("id")
        chat_id = await api.find_chat_by_buyer(int(buyer_id)) if buyer_id else None

        sent = False
        # Ответ на отзыв через API
        try:
            result = await api.send_review_reply(order_id, review_text)
            if result.get("success"):
                sent = True
        except Exception:
            pass

        # Fallback — сообщение в чат
        if not sent and chat_id:
            try:
                await api.send_message(chat_id, review_text)
                sent = True
            except Exception as exc:
                logger.warning("review chat send failed: %s", exc)

        if sent:
            await self.db.mark_order_reviewed(order_id, account_name)
            await self.notify(f"⭐ Заказ #{order_id} завершён. Благодарность отправлена.", "notify_orders")

        order_ctx = await self._build_order_ctx(account_name, order)
        await self._emit_starvell(STV_ORDER_COMPLETED, order_ctx)
        await self.cardinal.event_manager.dispatch("on_order_completed", {"order": order, "account": account_name})

    async def _bootstrap_orders(self, account_name: str, api: StarvellAPI) -> None:
        """Помечает существующие заказы как уже обработанные (не уведомлять о старых)."""
        try:
            orders = await api.fetch_orders()
            for order in orders:
                if not isinstance(order, dict):
                    continue
                order_id = str(order.get("id") or "")
                if not order_id:
                    continue
                await self.db.mark_order_notified(order_id, account_name)
                status = str(order.get("status") or "")
                if status:
                    await self.db.set_order_status(order_id, status, account_name)
            await self.db.set_orders_bootstrapped(account_name)
            logger.info("[%s] Baseline заказов: %d шт. (старые не будут обрабатываться)", account_name, len(orders))
        except Exception as exc:
            logger.warning("bootstrap_orders %s: %s", account_name, exc)

    async def _bootstrap_chats(self, account_name: str, api: StarvellAPI, my_user_id: int | None) -> None:
        """Фиксирует текущие сообщения как уже прочитанные — бот реагирует только на новые."""
        try:
            chats = await api.fetch_chats()
            for chat in chats:
                chat_id = str(chat.get("id") or "")
                if not chat_id:
                    continue

                participants = chat.get("participants") or []
                interlocutor_id = None
                for p in participants:
                    pid = (p or {}).get("id")
                    if my_user_id and pid != my_user_id:
                        interlocutor_id = pid
                        break

                messages = await api.fetch_messages(chat_id, limit=30, interlocutor_id=interlocutor_id)
                if not messages:
                    continue

                sorted_msgs = sorted(messages, key=_message_ts)
                latest = sorted_msgs[-1]
                latest_id = _message_id(latest)
                if latest_id:
                    await self.db.set_last_notified_message(chat_id, latest_id, account_name)

                last_buyer_ts = 0
                for msg in reversed(sorted_msgs):
                    author_id = msg.get("authorId") or msg.get("author")
                    if my_user_id and author_id == my_user_id:
                        continue
                    text = str(msg.get("content") or msg.get("text") or "").strip()
                    if not text:
                        continue
                    last_buyer_ts = _message_ts(msg) or int(time.time())
                    break

                if last_buyer_ts:
                    await self.db.set_chat_last_user_message_at(chat_id, last_buyer_ts, account_name)

            await self.db.set_chats_bootstrapped(account_name)
            logger.info("[%s] Baseline чатов: %d шт. (старые сообщения игнорируются)", account_name, len(chats))
        except Exception as exc:
            logger.warning("bootstrap_chats %s: %s", account_name, exc)

    async def _bootstrap_single_chat(
        self,
        account_name: str,
        api: StarvellAPI,
        chat_id: str,
        my_user_id: int | None,
        interlocutor_id: int | None,
        messages: list[dict] | None = None,
    ) -> str | None:
        """Помечает старые сообщения чата как прочитанные. Возвращает anchor id или None."""
        if messages is None:
            messages = await api.fetch_messages(chat_id, limit=30, interlocutor_id=interlocutor_id)
        if not messages:
            return None

        boot_at = await self.db.get_chats_bootstrapped_at(account_name) or 0
        sorted_msgs = sorted(messages, key=_message_ts)
        old_msgs = [
            m for m in sorted_msgs
            if boot_at and _message_ts(m) and _message_ts(m) <= boot_at
        ]

        last_buyer_ts = 0
        for msg in reversed(sorted_msgs):
            author_id = msg.get("authorId") or msg.get("author")
            if my_user_id and author_id == my_user_id:
                continue
            text = str(msg.get("content") or msg.get("text") or "").strip()
            if text:
                last_buyer_ts = _message_ts(msg) or int(time.time())
                break

        if not old_msgs:
            return None

        anchor = _message_id(old_msgs[-1])
        if anchor:
            await self.db.set_last_notified_message(chat_id, anchor, account_name)
        if last_buyer_ts:
            await self.db.set_chat_last_user_message_at(chat_id, last_buyer_ts, account_name)

        logger.debug("[%s] Baseline чата %s (anchor=%s)", account_name, chat_id[:8], anchor)
        return anchor

    async def _chats_loop(self, account_name: str, api: StarvellAPI) -> None:
        """Мониторинг чатов: приветствие и ИИ-ответы."""
        while self._running:
            settings = self._get_settings()
            interval = max(1.5, settings.chat_poll_interval)
            try:
                my_id = await self._get_cached_user_id(account_name, api)
                if not await self.db.is_chats_bootstrapped(account_name):
                    info = await api.fetch_homepage()
                    my_id = (info.get("user") or {}).get("id")
                    self._user_id_cache[account_name] = (time.time(), my_id)
                    await self._bootstrap_chats(account_name, api, my_id)
                await self._process_chats(account_name, api, settings, my_id)
            except Exception as exc:
                logger.warning("chats_loop %s: %s", account_name, exc)
            await asyncio.sleep(interval)

    def _iter_new_buyer_messages(
        self,
        messages: list[dict],
        last_notified_id: str | None,
        my_user_id: int | None,
    ) -> list[dict]:
        """Возвращает только сообщения покупателя, появившиеся после last_notified_id."""
        sorted_msgs = sorted(messages, key=_message_ts)
        result: list[dict] = []
        seen_last = not last_notified_id

        for msg in sorted_msgs:
            mid = _message_id(msg)
            if not mid:
                continue
            if not seen_last:
                if mid == last_notified_id:
                    seen_last = True
                continue

            author_id = msg.get("authorId") or msg.get("author")
            if my_user_id and author_id == my_user_id:
                continue

            text = str(msg.get("content") or msg.get("text") or "").strip()
            if not text:
                continue

            result.append(msg)

        return result

    async def _process_chats(
        self,
        account_name: str,
        api: StarvellAPI,
        settings: Settings,
        my_user_id: int | None,
    ) -> None:
        chats = await api.fetch_chats()
        open_orders = await self._get_cached_open_orders(account_name, api)
        sem = asyncio.Semaphore(6)

        async def _run(chat: dict) -> None:
            async with sem:
                await self._process_single_chat(
                    account_name, api, settings, my_user_id, chat, open_orders,
                )

        await asyncio.gather(*[_run(c) for c in chats], return_exceptions=True)

    async def _process_single_chat(
        self,
        account_name: str,
        api: StarvellAPI,
        settings: Settings,
        my_user_id: int | None,
        chat: dict,
        open_orders: list[dict],
    ) -> None:
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return

        last_notified = await self.db.get_last_notified_message(chat_id, account_name)
        if not _chat_has_new_activity(chat, last_notified, my_user_id):
            return

        participants = chat.get("participants") or []
        interlocutor_id = None
        for p in participants:
            pid = (p or {}).get("id")
            if my_user_id and pid != my_user_id:
                interlocutor_id = pid
                break

        messages = await api.fetch_messages(chat_id, limit=15, interlocutor_id=interlocutor_id)

        if last_notified is None and await self.db.is_chats_bootstrapped(account_name):
            boot_at = await self.db.get_chats_bootstrapped_at(account_name) or 0
            has_old = any(
                boot_at and _message_ts(m) and _message_ts(m) <= boot_at
                for m in messages
            )
            if has_old:
                last_notified = await self._bootstrap_single_chat(
                    account_name, api, chat_id, my_user_id, interlocutor_id, messages
                )
                if last_notified and not self._iter_new_buyer_messages(messages, last_notified, my_user_id):
                    return

        for msg in self._iter_new_buyer_messages(messages, last_notified, my_user_id):
            mid = _message_id(msg)
            author_id = msg.get("authorId") or msg.get("author")

            username = ""
            for p in participants:
                if (p or {}).get("id") == author_id:
                    username = (p or {}).get("username") or ""
                    break

            text = str(msg.get("content") or msg.get("text") or "").strip()
            if _is_platform_system_message(msg, text, settings):
                await self.db.set_last_notified_message(chat_id, mid, account_name)
                continue

            guard = self._payment_guard.inspect(text)
            if guard.is_suspicious:
                await self.notify(
                    self._payment_guard.format_admin_alert(username, chat_id, text),
                    "notify_chats",
                    chat_id=chat_id,
                )
                await self.db.set_last_notified_message(chat_id, mid, account_name)
                continue

            prev_buyer_ts = await self.db.get_chat_last_user_message_at(chat_id, account_name)
            msg_ts = _message_ts(msg) or int(time.time())
            await self.db.set_chat_last_user_message_at(chat_id, msg_ts, account_name)

            if not await self.db.is_blacklisted(
                username=username, starvell_user_id=author_id, check="block_notify"
            ):
                await self.notify(
                    f"💬 <b>Новое сообщение</b> от {username or 'покупателя'}\n"
                    f"<i>{str(msg.get('content') or msg.get('text') or '')[:300]}</i>",
                    "notify_chats",
                    chat_id=chat_id,
                )

            await self.db.set_last_notified_message(chat_id, mid, account_name)

            handled = await self._handlers.on_chat_message(
                text=text, chat_id=chat_id, api=api, account_name=account_name,
                author_id=author_id, username=username, settings=settings,
            )
            if not handled:
                has_open_order = _buyer_has_open_order(open_orders, author_id, username)
                await self._handlers.on_welcome(
                    chat_id=chat_id,
                    api=api,
                    settings=settings,
                    account_name=account_name,
                    previous_buyer_message_at=prev_buyer_ts,
                    has_open_order=has_open_order,
                )

            if await self.db.get_feature_flag("ai_replies", settings.ai_replies_enabled):
                await self._maybe_ai_reply(account_name, api, settings, chat_id, text, messages)

            msg_ctx = MessageContext(
                core=self.cardinal,
                account_name=account_name,
                chat_id=chat_id,
                text=text,
                author_id=author_id,
                username=username,
                message_id=mid or "",
                raw_message=msg,
            )
            await self._emit_starvell(STV_MESSAGE, msg_ctx)

            ctx = PluginContext(api, self.db, settings, account_name)
            ctx.chat_id = chat_id
            ctx.message_author_id = author_id
            await self.cardinal.event_manager.dispatch(
                "on_message", {"message": text, "chat_id": chat_id, "ctx": ctx}
            )
            await self.cardinal.dispatch_plugins("BIND_TO_NEW_MESSAGE", text, chat_id)

    async def _maybe_ai_reply(
        self,
        account_name: str,
        api: StarvellAPI,
        settings: Settings,
        chat_id: str,
        buyer_message: str,
        history: list[dict],
    ) -> None:
        # Кулдаун 30 сек между ИИ-ответами в одном чате
        last_ai = await self.db.get_ai_cooldown(chat_id, account_name)
        if int(time.time()) - last_ai < 30:
            return

        self._ai.settings = settings
        reply = await self._ai.generate_reply(buyer_message, history)
        if not reply:
            blocked = self._ai.check_blacklist(buyer_message)
            if blocked and settings.ai_blacklist_alert:
                await self.notify(
                    f"⚡️ <b>ИИ отключён</b> — чёрный список слов\n"
                    f"Слово: <code>{blocked}</code>\n"
                    f"Чат: <code>{chat_id[:12]}</code>\n"
                    f"<i>Требуется живой оператор.</i>",
                    "notify_chats",
                    chat_id=chat_id,
                )
            return
        reply = api.apply_watermark(reply, settings.watermark_on, settings.watermark_text)
        try:
            await api.send_message(chat_id, reply)
            await self.db.set_ai_cooldown(chat_id, account_name)
            await self.notify(f"🤖 Gemini ответил в чате {chat_id[:8]}…", "notify_chats")
        except Exception as exc:
            logger.warning("ai reply failed: %s", exc)

    async def _bump_loop(self, account_name: str, api: StarvellAPI) -> None:
        """Циклическое поднятие лотов."""
        while self._running:
            settings = self._get_settings()
            if not await self.db.get_feature_flag("auto_bump", settings.auto_bump_enabled):
                await asyncio.sleep(60)
                continue
            interval = max(300.0, settings.bump_interval)
            jitter = random.randint(settings.bump_jitter_min, settings.bump_jitter_max)
            interval = max(60.0, interval + jitter)
            try:
                await self._do_bump(account_name, api, settings)
            except Exception as exc:
                logger.warning("bump_loop %s: %s", account_name, exc)
            await asyncio.sleep(interval)

    async def _do_bump(self, account_name: str, api: StarvellAPI, settings: Settings) -> None:
        info = await api.fetch_homepage()
        user = info.get("user") or {}
        user_id = user.get("id")
        if not user_id:
            return

        lots = await api.fetch_user_lots(int(user_id))
        game_categories: dict[int, set[int]] = {}
        referer = None
        for lot in lots:
            gid = lot.get("game_id")
            cid = lot.get("category_id")
            if isinstance(gid, int) and isinstance(cid, int):
                game_categories.setdefault(gid, set()).add(cid)
            if not referer and lot.get("category_url"):
                referer = lot["category_url"]

        bumped = 0
        for game_id, cat_ids in game_categories.items():
            result = await api.bump_offers(game_id, list(cat_ids), referer=referer)
            if result.get("success"):
                bumped += len(cat_ids)

        if bumped and settings.notify_bump:
            await self.notify(f"📈 [{account_name}] Лоты подняты ({bumped} категорий)", "notify_bump")

        bump_ctx = BumpContext(
            core=self.cardinal,
            account_name=account_name,
            categories_bumped=bumped,
        )
        await self._emit_starvell(STV_BUMP, bump_ctx)

    async def get_status(self) -> dict[str, Any]:
        """Сводка для Telegram-меню."""
        settings = self._get_settings()
        result: dict[str, Any] = {"accounts": []}
        for account in settings.get_active_accounts():
            api = self._apis.get(account.name) or self._build_api(account)
            entry: dict[str, Any] = {"name": account.name}
            try:
                info = await api.fetch_homepage()
                user = info.get("user") or {}
                entry["authorized"] = info.get("authorized", False)
                entry["username"] = user.get("username")
                entry["balance"] = user.get("balance")
                orders = await api.fetch_orders()
                active = [o for o in orders if str(o.get("status")) in ("CREATED", "IN_PROGRESS", "PAID")]
                entry["active_orders"] = len(active)
                entry["total_orders"] = len(orders)
            except Exception as exc:
                entry["error"] = str(exc)
            result["accounts"].append(entry)
        return result
