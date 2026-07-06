"""
Gemini Review Reply — ответы на отзывы через Google Gemini (Starvell Cardinal).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from starvell_sdk import OrderContext, StarvellPlugin, on_order_completed

NAME = "Gemini Review Reply"
VERSION = "2.0.0"
DESCRIPTION = "ИИ-ответы на отзывы покупателей (Gemini + proxy)"
CREDITS = "Starvell Cardinal"
UUID = "c4e8b2f1-9a3d-4e7b-8c6f-2d1a5e9b0c3f"
SETTINGS_PAGE = True

DEFAULT_SYSTEM_PROMPT = """Ты — дружелюбный продавец на Starvell.
Напиши ответ на отзыв покупателя на русском языке.

Правила:
- Не обращайся к покупателю по имени
- Упомяни товар: {product_name}
- Тёплый тон, умеренно эмодзи
- Для 1★ — вежливо укажи, что отзыв необоснован и будет оспорен
- До 900 символов, не более 8 строк
- Каждый ответ уникален"""

logger = logging.getLogger("starvell.plugin.gemini_review")


class Plugin(StarvellPlugin):
    def on_load(self) -> None:
        self.log("Gemini Review Reply загружен")

    def settings_page_size(self) -> int:
        return 6

    def get_settings_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "enabled", "label": "Включить плагин", "type": "bool", "default": True},
            {"key": "gemini_api_key", "label": "Gemini API Key", "type": "text", "default": ""},
            {"key": "gemini_proxy", "label": "Proxy (http://user:pass@host:port)", "type": "text", "default": ""},
            {"key": "gemini_model", "label": "Модель Gemini", "type": "text", "default": "gemini-2.0-flash"},
            {"key": "system_prompt", "label": "Системный промпт", "type": "multiline", "default": DEFAULT_SYSTEM_PROMPT},
            {"key": "temperature", "label": "Temperature", "type": "text", "default": "0.95"},
            {"key": "send_chat_message", "label": "Дублировать в чат", "type": "bool", "default": True},
        ]

    async def on_setting_change(self, key: str, value: Any) -> None:
        if key == "gemini_api_key" and value:
            self.log("Gemini API key обновлён")

    @on_order_completed
    async def on_review(self, ctx: OrderContext) -> None:
        if not await self.get_cfg("enabled", True):
            return
        order = ctx.order or {}
        order_id = str(ctx.order_id or order.get("id") or "")
        if not order_id:
            return
        if await self.db.is_order_reviewed(order_id, ctx.account_name):
            return

        review = order.get("review") or {}
        stars = str(review.get("stars") or review.get("rating") or "5")
        review_text = str(review.get("text") or review.get("comment") or "").strip()
        product = ctx.product_name or "товар"
        order_dt = datetime.now().strftime("%d.%m.%Y %H:%M")

        system_prompt = str(await self.get_cfg("system_prompt", DEFAULT_SYSTEM_PROMPT))
        system_prompt = system_prompt.format(
            product_name=product,
            order_datetime=order_dt,
        )

        user_prompt = (
            f"Оценка: {stars}★\n"
            f"Товар: {product}\n"
            f"Отзыв покупателя: {review_text or '(без текста)'}\n\n"
            "Напиши ответ продавца."
        )

        reply = await self._generate(system_prompt, user_prompt)
        if not reply:
            self.log("Gemini не вернул текст для #%s", order_id, level="warning")
            return

        api = ctx.api()
        if api:
            try:
                result = await api.send_review_reply(order_id, reply)
                if result.get("success"):
                    await self.db.mark_order_reviewed(order_id, ctx.account_name)
                    await ctx.notify(f"⭐ Gemini Review: ответ на #{order_id}", "notify_orders")
                    if await self.get_cfg("send_chat_message", True) and ctx.chat_id:
                        await ctx.send_to_buyer(reply)
                    return
            except Exception as exc:
                self.log("send_review_reply #%s: %s", order_id, exc, level="warning")

        if ctx.chat_id:
            sent = await ctx.send_to_buyer(reply)
            if sent:
                await self.db.mark_order_reviewed(order_id, ctx.account_name)
                await ctx.notify(f"⭐ Gemini Review (чат): #{order_id}", "notify_orders")

    async def _generate(self, system_prompt: str, user_prompt: str) -> str | None:
        api_key = str(await self.get_cfg("gemini_api_key", "")).strip()
        if not api_key:
            api_key = (self.settings.gemini_api_key or "").strip()
        if not api_key:
            return None

        model = str(await self.get_cfg("gemini_model", "gemini-2.0-flash")).strip()
        proxy = str(await self.get_cfg("gemini_proxy", "")).strip()
        if not proxy:
            proxy = (getattr(self.settings, "gemini_proxy", "") or "").strip()

        try:
            temperature = float(await self.get_cfg("temperature", "0.95"))
        except (TypeError, ValueError):
            temperature = 0.95

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 900},
        }
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        client_kwargs: dict[str, Any] = {"timeout": 60.0}
        if proxy:
            client_kwargs["proxy"] = proxy

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    self.log("Gemini HTTP %s: %s", resp.status_code, resp.text[:200], level="warning")
                    return None
                data = resp.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    return None
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
                return text[:900] if text else None
        except Exception as exc:
            self.log("Gemini error: %s", exc, level="warning")
            return None
