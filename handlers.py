"""
Встроенные обработчики событий (аналог handlers.py в FunPay Cardinal).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ai_service import AIService
from config import Settings, load_settings

logger = logging.getLogger("starvell.handlers")


def format_vars(text: str, **kwargs: Any) -> str:
    """Подстановка переменных $username, $order_id, $product и т.д."""
    for key, val in kwargs.items():
        text = text.replace(f"${key}", str(val))
        text = text.replace(f"{{{key}}}", str(val))
    return text


class BuiltinHandlers:
    """Обработчики автоматизации Starvell."""

    def __init__(self, cardinal: Any) -> None:
        self.cardinal = cardinal
        self._ai = AIService(load_settings())

    @property
    def db(self):
        return self.cardinal.db

    async def on_chat_message(
        self,
        *,
        text: str,
        chat_id: str,
        api: Any,
        account_name: str,
        author_id: int | None,
        username: str,
        settings: Settings,
    ) -> bool:
        """Возвращает True если сообщение обработано автоответчиком."""
        if await self.db.is_blacklisted(username=username, starvell_user_id=author_id, check="block_response"):
            return False

        if not await self.db.get_feature_flag("auto_response", settings.auto_response_enabled):
            return False

        ar = await self.db.find_ar_response(text)
        if not ar:
            return False

        reply = format_vars(ar["response"], username=username, chat_id=chat_id)
        reply = api.apply_watermark(reply, settings.watermark_on, settings.watermark_text)
        try:
            await api.send_message(chat_id, reply)
            if ar.get("notify"):
                await self.cardinal.notify(
                    f"📨 Автоответ «{ar['command']}» → {username or chat_id[:8]}",
                    "notify_chats",
                )
            return True
        except Exception as exc:
            logger.warning("auto_response failed: %s", exc)
        return False

    async def on_welcome(
        self,
        *,
        chat_id: str,
        api: Any,
        settings: Settings,
        account_name: str,
        previous_buyer_message_at: int | None = None,
    ) -> None:
        if not await self.db.get_feature_flag("auto_welcome", settings.auto_welcome_enabled):
            return

        inactivity_sec = max(1, settings.welcome_inactivity_days) * 86400
        last_welcome_at = await self.db.get_last_welcome_at(chat_id)
        now = int(time.time())

        # Новый чат — покупатель пишет впервые с момента установки бота
        is_new_chat = previous_buyer_message_at is None and last_welcome_at is None

        # Покупатель вернулся после паузы (минимум N дней без сообщений)
        returned_after_pause = (
            previous_buyer_message_at is not None
            and (now - previous_buyer_message_at) >= inactivity_sec
        )

        if settings.greetings_only_new_chats:
            if not is_new_chat and not returned_after_pause:
                return
        elif last_welcome_at and (now - last_welcome_at) < inactivity_sec:
            return

        text = api.apply_watermark(settings.welcome_text, settings.watermark_on, settings.watermark_text)
        try:
            await api.send_message(chat_id, text)
            await self.db.mark_chat_welcomed(chat_id)
            logger.info(
                "welcome sent chat=%s new=%s after_pause=%s",
                chat_id[:8],
                is_new_chat,
                returned_after_pause,
            )
        except Exception as exc:
            logger.warning("welcome failed: %s", exc)

    async def on_order_completed(
        self, *, order: dict, api: Any, settings: Settings, account_name: str
    ) -> str | None:
        """Благодарность / ответ на отзыв после COMPLETED."""
        if not await self.db.get_feature_flag("auto_review", settings.auto_review_enabled):
            return None

        order_id = str(order.get("id"))
        review = order.get("review") or {}
        stars = str(review.get("stars") or review.get("rating") or "5")

        if settings.review_use_gemini and settings.gemini_api_key:
            self._ai.settings = settings
            text = await self._ai.generate_review_text(order)
        else:
            text = settings.review_replies.get(stars) or settings.review_template

        text = format_vars(text, order_id=order_id, product=self._product_name(order))
        return api.apply_watermark(text, settings.watermark_on, settings.watermark_text)

    async def on_order_confirm(
        self, *, order: dict, api: Any, settings: Settings, chat_id: str | None
    ) -> None:
        if not await self.db.get_feature_flag("order_confirm", settings.order_confirm_enabled):
            return
        if not chat_id:
            return
        text = format_vars(
            settings.order_confirm_text,
            order_id=order.get("id"),
            product=self._product_name(order),
        )
        text = api.apply_watermark(text, settings.watermark_on, settings.watermark_text)
        try:
            await api.send_message(chat_id, text)
        except Exception as exc:
            logger.warning("order_confirm failed: %s", exc)

    @staticmethod
    def _product_name(order: dict) -> str:
        offer = order.get("offerDetails") or {}
        desc = (offer.get("descriptions") or {}).get("rus") or {}
        return (
            str(desc.get("briefDescription") or "").strip()
            or str(desc.get("description") or "").strip()
            or "товар"
        )
