"""
Фоновая автоматизация: заказы, чаты, бамп, автовыдача, ИИ, отзывы.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable

from ai_service import AIService
from config import Settings, load_settings
from database import Database
from handlers import BuiltinHandlers
from plugin_manager import PluginContext
from starvell_api import StarvellAPI

logger = logging.getLogger("starvell.automation")


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

    def _get_settings(self) -> Settings:
        return load_settings()

    def _build_api(self, account) -> StarvellAPI:
        settings = self._get_settings()
        return StarvellAPI(
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
        self._apis.clear()

    async def reload(self) -> None:
        """Перезапускает фоновые задачи (после смены session cookie)."""
        await self.stop()
        self._ai = AIService(load_settings())
        await self.start()

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
            interval = max(5.0, settings.orders_poll_interval)
            try:
                await self._process_orders(account_name, api, settings)
            except Exception as exc:
                logger.exception("orders_loop %s: %s", account_name, exc)
            await asyncio.sleep(interval)

    async def _process_orders(self, account_name: str, api: StarvellAPI, settings: Settings) -> None:
        orders = await api.fetch_orders()
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
        price = order.get("basePrice") or order.get("totalPrice") or 0

        await self.notify(
            f"🛒 <b>Новый заказ #{order_id}</b>\n"
            f"Покупатель: {buyer}\n"
            f"Товар: {product_name}\n"
            f"Сумма: {price} ₽",
            "notify_orders",
            order_id=order_id,
        )
        await self.cardinal.dispatch_plugins("BIND_TO_NEW_ORDER", order)

        # Плагины
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
        delivery_text = settings.delivery_template.format(product=product_name, content=content)
        delivery_text = api.apply_watermark(delivery_text, settings.watermark_on, settings.watermark_text)

        buyer_id = (order.get("user") or {}).get("id")
        chat_id = await api.find_chat_by_buyer(int(buyer_id)) if buyer_id else None
        if chat_id:
            try:
                await api.send_message(chat_id, delivery_text)
                await self.notify(f"✅ Заказ #{order_id} оплачен. Товар выдан успешно.", "notify_delivery")
            except Exception as exc:
                await self.notify(f"❌ Заказ #{order_id}: ошибка выдачи — {exc}", "notify_delivery")
        else:
            await self.notify(f"⚠️ Заказ #{order_id}: товар готов, но чат с покупателем не найден", "notify_delivery")

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

        await self.cardinal.event_manager.dispatch("on_order_completed", {"order": order, "account": account_name})

    async def _chats_loop(self, account_name: str, api: StarvellAPI) -> None:
        """Мониторинг чатов: приветствие и ИИ-ответы."""
        seen: dict[str, set[str]] = {}
        while self._running:
            settings = self._get_settings()
            interval = max(3.0, settings.chat_poll_interval)
            try:
                user_info = await api.fetch_homepage()
                my_id = (user_info.get("user") or {}).get("id")
                await self._process_chats(account_name, api, settings, my_id, seen)
            except Exception as exc:
                logger.warning("chats_loop %s: %s", account_name, exc)
            await asyncio.sleep(interval)

    async def _process_chats(
        self,
        account_name: str,
        api: StarvellAPI,
        settings: Settings,
        my_user_id: int | None,
        seen: dict[str, set[str]],
    ) -> None:
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
            if chat_id not in seen:
                seen[chat_id] = set()

            for msg in messages:
                mid = str(msg.get("id") or msg.get("messageId") or "")
                if not mid or mid in seen[chat_id]:
                    continue
                seen[chat_id].add(mid)

                author_id = msg.get("authorId") or msg.get("author")
                if my_user_id and author_id == my_user_id:
                    continue

                text = str(msg.get("content") or msg.get("text") or "").strip()
                if not text:
                    continue

                last_notified = await self.db.get_last_notified_message(chat_id, account_name)
                if last_notified == mid:
                    continue

                username = ""
                for p in participants:
                    if (p or {}).get("id") == author_id:
                        username = (p or {}).get("username") or ""
                        break

                if not await self.db.is_blacklisted(
                    username=username, starvell_user_id=author_id, check="block_notify"
                ):
                    await self.notify(
                        f"💬 <b>Новое сообщение</b> от {username or 'покупателя'}\n"
                        f"<i>{text[:300]}</i>",
                        "notify_chats",
                        chat_id=chat_id,
                    )
                await self.db.set_last_notified_message(chat_id, mid, account_name)

                handled = await self._handlers.on_chat_message(
                    text=text, chat_id=chat_id, api=api, account_name=account_name,
                    author_id=author_id, username=username, settings=settings,
                )
                if not handled:
                    await self._handlers.on_welcome(
                        chat_id=chat_id, api=api, settings=settings, account_name=account_name,
                    )

                if await self.db.get_feature_flag("ai_replies", settings.ai_replies_enabled):
                    await self._maybe_ai_reply(account_name, api, settings, chat_id, text, messages)

                ctx = PluginContext(api, self.db, settings, account_name)
                ctx.chat_id = chat_id
                ctx.message_author_id = author_id
                await self.cardinal.event_manager.dispatch(
                    "on_message", {"message": text, "chat_id": chat_id, "ctx": ctx}
                )
                await self.cardinal.dispatch_plugins("BIND_TO_NEW_MESSAGE", text, chat_id)

    async def _maybe_welcome(
        self, account_name: str, api: StarvellAPI, settings: Settings, chat_id: str
    ) -> None:
        cooldown = settings.welcome_cooldown_minutes * 60
        last = await self.db.get_chat_last_user_message_at(chat_id, account_name)
        now = int(time.time())
        if last and (now - last) < cooldown:
            return
        welcome = api.apply_watermark(settings.welcome_text, settings.watermark_on, settings.watermark_text)
        try:
            await api.send_message(chat_id, welcome)
            await self.db.set_chat_last_user_message_at(chat_id, now, account_name)
        except Exception as exc:
            logger.warning("welcome failed: %s", exc)

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
